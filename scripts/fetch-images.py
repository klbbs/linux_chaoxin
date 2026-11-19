#!/usr/bin/python3
"""Resume official Waydroid image downloads in an ordinary user's cache only."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
import configparser
import fcntl
import hashlib
import http.client
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

CHUNK_SIZE = 8 * 1024 * 1024
MAX_WORKERS = 8
TIMEOUT = 30
RETRIES = 3


def validate_url(url, stage='image', allow_loopback=False):
    parsed = urllib.parse.urlsplit(url)
    host = (parsed.hostname or '').lower()
    if parsed.username or parsed.password or parsed.fragment:
        raise ValueError('Download URL contains unsupported credentials or fragment')
    if allow_loopback and host in ('localhost', '127.0.0.1', '::1') and parsed.scheme == 'http':
        return
    if parsed.scheme != 'https' or parsed.port not in (None, 443):
        raise ValueError('Only HTTPS official servers are permitted')
    if stage == 'metadata':
        accepted = host == 'ota.waydro.id'
    else:
        accepted = host == 'sourceforge.net' or host.endswith('.sourceforge.net')
    if not accepted:
        raise ValueError(f'Unexpected {stage} server: {host}')


class CheckedRedirect(urllib.request.HTTPRedirectHandler):
    def __init__(self, stage, allow_loopback):
        self.stage = stage
        self.allow_loopback = allow_loopback

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        validate_url(newurl, self.stage, self.allow_loopback)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def open_url(url, *, stage='image', headers=None, allow_loopback=False):
    validate_url(url, stage, allow_loopback)
    opener = urllib.request.build_opener(CheckedRedirect(stage, allow_loopback))
    request = urllib.request.Request(url, headers={'User-Agent': 'ChaoxingLinux-ImagePrefetch/0.1', **(headers or {})})
    response = opener.open(request, timeout=TIMEOUT)
    validate_url(response.geturl(), stage, allow_loopback)
    return response


def atomic_write(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix='.' + path.name + '-', dir=path.parent)
    try:
        with os.fdopen(descriptor, 'wb') as output:
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def file_hash(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as source:
        for block in iter(lambda: source.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def official_channels(config_path=Path('/var/lib/waydroid/waydroid.cfg')):
    config = configparser.ConfigParser(interpolation=None)
    with Path(config_path).open(encoding='utf-8') as stream:
        config.read_file(stream)
    channels = {kind: config.get('waydroid', kind + '_ota') for kind in ('system', 'vendor')}
    for url in channels.values():
        validate_url(url, 'metadata')
    return channels


def fetch_metadata(url, allow_loopback=False):
    last = None
    for attempt in range(RETRIES):
        try:
            with open_url(url, stage='metadata', allow_loopback=allow_loopback) as response:
                raw = response.read(4 * 1024 * 1024 + 1)
            if len(raw) > 4 * 1024 * 1024:
                raise ValueError('OTA metadata exceeds limit')
            entries = json.loads(raw)['response']
            if not isinstance(entries, list) or not entries:
                raise ValueError('Official OTA channel has no current images')
            entry = max(entries, key=lambda item: int(item.get('datetime', 0)))
            size = int(entry['size'])
            digest = str(entry['id']).lower()
            filename = entry['filename']
            if size <= 0 or size > 8 * 1024 ** 3 or not re.fullmatch(r'[0-9a-f]{64}', digest):
                raise ValueError('OTA image size or SHA256 is invalid')
            if not isinstance(filename, str) or not re.fullmatch(r'[A-Za-z0-9_.+\-]+\.zip', filename):
                raise ValueError('OTA image filename is unsafe')
            validate_url(entry['url'], 'image', allow_loopback)
            return {'filename': filename, 'size': size, 'sha256': digest,
                    'url': entry['url'], 'metadata_url': url,
                    'datetime': int(entry.get('datetime', 0))}
        except (OSError, ValueError, KeyError, TypeError, urllib.error.URLError, http.client.HTTPException) as exc:
            last = exc
            if attempt + 1 < RETRIES:
                time.sleep(min(2 ** attempt, 4))
    raise ValueError(f'Cannot read official OTA metadata after {RETRIES} attempts: {last}')


class Downloader:
    def __init__(self, cache, *, workers=MAX_WORKERS, chunk_size=CHUNK_SIZE,
                 retries=RETRIES, allow_loopback=False, progress=print):
        if not 1 <= workers <= MAX_WORKERS or chunk_size <= 0 or not 1 <= retries <= 5:
            raise ValueError('Invalid bounded download settings')
        self.cache = Path(cache)
        self.workers = workers
        self.chunk_size = chunk_size
        self.retries = retries
        self.allow_loopback = allow_loopback
        self.progress = progress

    def chunk(self, image, index):
        first = index * self.chunk_size
        last = min(first + self.chunk_size, image['size']) - 1
        expected = last - first + 1
        directory = self.cache / 'chunks' / image['sha256'] / str(self.chunk_size)
        path = directory / f'{index:06d}.part'
        record = directory / f'{index:06d}.json'
        if path.is_file() and record.is_file():
            try:
                saved = json.loads(record.read_text())
                if (saved['start'] == first and saved['end'] == last and path.stat().st_size == expected
                        and saved['sha256'] == file_hash(path)):
                    return path, True
            except (OSError, ValueError, KeyError):
                pass
        error = None
        for attempt in range(self.retries):
            try:
                with open_url(image['url'], headers={'Range': f'bytes={first}-{last}',
                                                     'Accept-Encoding': 'identity'},
                              allow_loopback=self.allow_loopback) as response:
                    if response.status != 206:
                        raise ValueError(f'Server does not support validated ranges (HTTP {response.status}); no chunks were concatenated')
                    actual = response.headers.get('Content-Range', '')
                    match = re.fullmatch(r'bytes (\d+)-(\d+)/(\d+)', actual)
                    if not match or tuple(map(int, match.groups())) != (first, last, image['size']):
                        raise ValueError(f'Unexpected Content-Range: {actual!r}')
                    length = response.headers.get('Content-Length')
                    if length is not None and int(length) != expected:
                        raise ValueError('Range Content-Length mismatch')
                    encoding = response.headers.get('Content-Encoding', 'identity')
                    if encoding.lower() != 'identity':
                        raise ValueError('Compressed HTTP ranges are unsupported')
                    data = response.read(expected + 1)
                    if len(data) != expected:
                        raise ValueError(f'Range length mismatch: got {len(data)}, expected {expected}')
                atomic_write(path, data)
                atomic_write(record, json.dumps({'start': first, 'end': last, 'sha256': hashlib.sha256(data).hexdigest()}).encode())
                return path, False
            except (OSError, ValueError, urllib.error.URLError, http.client.HTTPException) as exc:
                error = exc
                if attempt + 1 < self.retries:
                    time.sleep(min(2 ** attempt, 4))
        raise ValueError(f'Chunk {index} failed after {self.retries} attempts: {error}')

    def download(self, image):
        validate_url(image['url'], 'image', self.allow_loopback)
        target = self.cache / image['filename']
        if target.is_file() and target.stat().st_size == image['size'] and file_hash(target) == image['sha256']:
            self.progress(f"[verified cache] {image['filename']}")
            return target
        self.cache.mkdir(parents=True, exist_ok=True)
        count = (image['size'] + self.chunk_size - 1) // self.chunk_size
        completed = {}
        with ThreadPoolExecutor(max_workers=self.workers) as pool:
            futures = {pool.submit(self.chunk, image, index): index for index in range(count)}
            try:
                for future in as_completed(futures):
                    index = futures[future]
                    path, reused = future.result()
                    completed[index] = path
                    self.progress(f"[{image['filename']}] {len(completed)}/{count} chunks {'resumed' if reused else 'downloaded'}")
            except BaseException:
                for future in futures:
                    future.cancel()
                raise
        temporary = self.cache / (image['filename'] + '.merging')
        digest = hashlib.sha256()
        total = 0
        with temporary.open('wb') as merged:
            for index in range(count):
                with completed[index].open('rb') as part:
                    for block in iter(lambda: part.read(1024 * 1024), b''):
                        merged.write(block)
                        digest.update(block)
                        total += len(block)
            merged.flush()
            os.fsync(merged.fileno())
        actual_hash = digest.hexdigest()
        if total != image['size'] or actual_hash != image['sha256']:
            diagnostic = temporary.with_name(temporary.name + '.invalid')
            os.replace(temporary, diagnostic)
            atomic_write(diagnostic.with_suffix(diagnostic.suffix + '.json'),
                         json.dumps({'expected_size': image['size'], 'actual_size': total,
                                     'expected_sha256': image['sha256'], 'actual_sha256': actual_hash,
                                     'source_url': image['url']}, indent=2).encode())
            raise ValueError(f'Image SHA256/length mismatch; diagnostics retained at {diagnostic}; nothing installed')
        os.replace(temporary, target)
        self.progress(f"[SHA256 verified] {target} {actual_hash}")
        return target


def main():
    if os.geteuid() == 0:
        print('Run this prefetcher as an ordinary user, without sudo.', file=sys.stderr)
        return 1
    cache = Path.home() / '.cache/chaoxing-linux/images'
    cache.mkdir(parents=True, exist_ok=True)
    with (cache / '.prefetch.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print('Another image prefetch is already running.', file=sys.stderr)
            return 1
        manifest = {'status': 'fetching_metadata', 'images': {}}
        try:
            channels = official_channels()
            for kind, url in channels.items():
                image = fetch_metadata(url)
                image['path'] = str(cache / image['filename'])
                image['verified'] = False
                manifest['images'][kind] = image
                print(f"[official metadata] {kind} {image['filename']} bytes={image['size']} sha256={image['sha256']}", flush=True)
            manifest['status'] = 'downloading'
            atomic_write(cache / 'manifest.json', json.dumps(manifest, indent=2).encode())
            downloader = Downloader(cache, progress=lambda message: print(message, flush=True))
            for kind, image in manifest['images'].items():
                downloader.download(image)
                image['verified'] = True
                atomic_write(cache / 'manifest.json', json.dumps(manifest, indent=2).encode())
            manifest['status'] = 'complete'
            atomic_write(cache / 'manifest.json', json.dumps(manifest, indent=2).encode())
            print('[complete] Both images are verified in the user cache; no system files changed.', flush=True)
            return 0
        except (OSError, ValueError, KeyError, configparser.Error) as exc:
            manifest['status'] = 'failed'
            manifest['error'] = str(exc)
            atomic_write(cache / 'manifest.json', json.dumps(manifest, indent=2).encode())
            print(f'[failed] {exc}', file=sys.stderr, flush=True)
            return 1


if __name__ == '__main__':
    sys.exit(main())
