"""Discover only local Chaoxing sessions, without modifying or validating their sources."""
from __future__ import annotations

import configparser
from dataclasses import dataclass, field
import os
from pathlib import Path
import re
import sqlite3
import stat
import tempfile
import time

MAX_COOKIE_FILE = 64 * 1024
MAX_DATABASE = 128 * 1024 * 1024
_DOMAIN = re.compile(r'(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)*chaoxing\.com\Z')
_NAME = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+\Z")


@dataclass(frozen=True)
class SessionCandidate:
    source: str
    cookies: str = field(repr=False)


def _header(pairs):
    """Validate the simple name=value format understood by the isolated backend."""
    selected = {}
    for name, value in pairs:
        if (not isinstance(name, str) or not _NAME.fullmatch(name)
                or not isinstance(value, str) or len(value) > 8192
                or ';' in value or any(ord(c) < 32 or ord(c) > 126 for c in value)):
            continue
        if name not in selected:
            selected[name] = value
    if not any(selected.get(key, '').strip() for key in ('_uid', 'UID')):
        return None
    result = '; '.join(f'{name}={value}' for name, value in sorted(selected.items()))
    return result if len(result) <= MAX_COOKIE_FILE else None


def _profiles(root):
    """Prefer declared defaults, retain each profile as a separate candidate."""
    ini = root / 'profiles.ini'
    profiles = []
    if ini.is_file():
        parser = configparser.ConfigParser(interpolation=None)
        try:
            if ini.stat().st_size > MAX_COOKIE_FILE:
                return []
            parser.read_string(ini.read_text(encoding='utf-8-sig'))
        except (OSError, UnicodeError, configparser.Error):
            return []
        defaults = {section.get('Default', '') for name, section in parser.items()
                    if name.lower().startswith('install') and section.get('Default')}
        for name, section in parser.items():
            if not name.lower().startswith('profile') or not section.get('Path'):
                continue
            path = Path(section['Path']).expanduser()
            if section.get('IsRelative', '1') != '0':
                path = root / path
            elif not path.is_absolute():
                continue
            preferred = section.get('Default') == '1' or section['Path'] in defaults
            profiles.append((not preferred, name, path))
        # Firefox can keep an installation default not duplicated by ProfileN.
        for value in sorted(defaults):
            path = Path(value).expanduser()
            profiles.append((False, '', path if path.is_absolute() else root / path))
    else:
        # A narrowly scoped fallback for older/portable Firefox profile layouts.
        if (root / 'cookies.sqlite').is_file():
            profiles.append((False, '', root))
        try:
            profiles.extend((True, item.name, item) for item in root.iterdir()
                            if item.is_dir() and (item / 'cookies.sqlite').is_file())
        except OSError:
            pass
    result, seen = [], set()
    for _, _, path in sorted(profiles, key=lambda item: (item[0], item[1], str(item[2]))):
        try:
            resolved = path.resolve()
        except (OSError, RuntimeError):
            continue
        if resolved not in seen:
            seen.add(resolved)
            result.append(resolved)
    return result


def _fingerprint(path):
    try:
        info = path.stat()
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_DATABASE:
        raise OSError('Unavailable session source')
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def _copy_private(source, target):
    # O_NOATIME is available for the user's own Linux files; fall back for other
    # filesystems. Neither path opens the source for writing or checkpointing.
    flags = os.O_RDONLY | getattr(os, 'O_CLOEXEC', 0) | getattr(os, 'O_NONBLOCK', 0)
    try:
        descriptor = os.open(source, flags | getattr(os, 'O_NOATIME', 0))
    except PermissionError:
        descriptor = os.open(source, flags)
    with os.fdopen(descriptor, 'rb') as incoming:
        if not stat.S_ISREG(os.fstat(incoming.fileno()).st_mode):
            raise OSError('Unavailable session source')
        descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, 'wb') as outgoing:
            remaining = MAX_DATABASE
            while block := incoming.read(min(65536, remaining + 1)):
                remaining -= len(block)
                if remaining < 0:
                    raise OSError('Session source too large')
                outgoing.write(block)


def _read_firefox(database):
    wal = database.with_name(database.name + '-wal')
    # Do not snapshot a rollback transaction: without its journal, copied pages
    # could represent uncommitted data. Firefox normally uses WAL instead.
    try:
        if database.with_name(database.name + '-journal').exists():
            return None
    except OSError:
        return None
    # Never open the original with SQLite: even mode=ro can create/change an SHM
    # index next to a live WAL database. Snapshot only DB/WAL into a private temp
    # directory and let SQLite construct any required SHM there.
    for _ in range(3):
        try:
            before = (_fingerprint(database), _fingerprint(wal))
            if before[0] is None:
                return None
            with tempfile.TemporaryDirectory(prefix='chaoxing-firefox-session-') as temporary:
                snapshot = Path(temporary) / 'cookies.sqlite'
                _copy_private(database, snapshot)
                if before[1] is not None:
                    _copy_private(wal, snapshot.with_name(snapshot.name + '-wal'))
                after = (_fingerprint(database), _fingerprint(wal))
                if before != after:
                    continue
                connection = sqlite3.connect(snapshot.as_uri() + '?mode=ro', uri=True, timeout=0.1)
                try:
                    connection.execute('PRAGMA query_only=ON')
                    columns = {row[1] for row in connection.execute('PRAGMA table_info(moz_cookies)')}
                    if not {'name', 'value', 'host', 'path', 'expiry'} <= columns:
                        return None
                    attributes = "COALESCE(originAttributes, '')" if 'originAttributes' in columns else "''"
                    rows = connection.execute(
                        'SELECT name,value,host,path,expiry FROM moz_cookies '
                        "WHERE (lower(host) IN ('chaoxing.com','.chaoxing.com') OR lower(host) LIKE '%.chaoxing.com') "
                        f"AND {attributes}='' AND (expiry=0 OR expiry>?)", (int(time.time()),)).fetchall()
                finally:
                    connection.close()
            ordered = []
            for name, value, host, path, expiry in rows:
                if not all(isinstance(item, str) for item in (name, value, host)):
                    continue
                domain = host[1:] if host.startswith('.') else host
                domain = domain.lower()
                if not _DOMAIN.fullmatch(domain):
                    continue
                path = path if isinstance(path, str) else '/'
                # Parent-domain cookies win before subdomain cookies. Within a
                # domain, prefer root paths and domain cookies, then stable text.
                rank = (domain.count('.'), domain, path != '/', len(path), path,
                        not host.startswith('.'), name, value)
                ordered.append((rank, name, value))
            ordered.sort(key=lambda item: item[0])
            return _header((name, value) for _, name, value in ordered)
        except (OSError, sqlite3.Error, ValueError, TypeError):
            # Locked, incomplete or corrupt sources are simply not candidates;
            # source paths and cookie/database contents never enter an error.
            return None
    return None


def _read_backend(path):
    try:
        if not path.is_file() or path.stat().st_size > MAX_COOKIE_FILE:
            return None
        value = path.read_text(encoding='utf-8').strip()
        pairs = []
        for item in value.split(';'):
            if not item.strip():
                continue
            if '=' not in item:
                return None
            name, content = item.strip().split('=', 1)
            pairs.append((name.strip(), content))
        return _header(pairs)
    except (OSError, UnicodeError, ValueError):
        return None


def discover_sessions(*, home: Path | None = None, backend_root: Path | None = None) -> list[SessionCandidate]:
    """Return local candidates only; callers must validate before replacing a session.

    Sources are ordered standard Firefox, Snap Firefox, Flatpak Firefox, then the
    explicitly supplied backend. Cookies are never merged across profiles/sources.
    No password database, unrelated browser, phone, or network is accessed.
    """
    home = Path.home() if home is None else Path(home)
    roots = ((home / '.mozilla/firefox', 'Firefox 标准'),
             (home / 'snap/firefox/common/.mozilla/firefox', 'Firefox Snap'),
             (home / '.var/app/org.mozilla.firefox/.mozilla/firefox', 'Firefox Flatpak'))
    candidates, visited = [], set()
    for root, label in roots:
        try:
            profiles = _profiles(root)
        except (OSError, ValueError, RuntimeError):
            continue
        for index, profile in enumerate(profiles, 1):
            database = profile / 'cookies.sqlite'
            if database in visited:
                continue
            visited.add(database)
            cookies = _read_firefox(database)
            if cookies:
                candidates.append(SessionCandidate(f'{label} · 配置 {index}', cookies))
    if backend_root is not None:
        cookies = _read_backend(Path(backend_root) / 'cookies.txt')
        if cookies:
            candidates.append(SessionCandidate('原命令行项目', cookies))
    return candidates
