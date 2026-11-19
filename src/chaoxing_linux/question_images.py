"""Bounded assignment image downloads and verification of private cached image files."""
from __future__ import annotations

import base64
import hashlib
import os
from pathlib import Path
import re
import secrets
import stat
import struct
import time
from urllib.parse import unquote_to_bytes, urljoin, urlsplit


MAX_IMAGE_BYTES = 8 * 1024 * 1024
MAX_QUESTION_BYTES = 24 * 1024 * 1024
MAX_QUESTION_IMAGES = 16
MIME_EXTENSIONS = {'image/png': 'png', 'image/jpeg': 'jpg', 'image/gif': 'gif', 'image/webp': 'webp'}


def image_type(data: bytes) -> str:
    if (len(data) >= 24 and data.startswith(b'\x89PNG\r\n\x1a\n') and data[12:16] == b'IHDR'
            and all(struct.unpack('>II', data[16:24]))):
        return 'image/png'
    if len(data) >= 12 and data.startswith(b'\xff\xd8\xff') and data.endswith(b'\xff\xd9'):
        return 'image/jpeg'
    if len(data) >= 13 and data[:6] in (b'GIF87a', b'GIF89a') and all(struct.unpack('<HH', data[6:10])):
        return 'image/gif'
    if (len(data) >= 20 and data[:4] == b'RIFF' and data[8:12] == b'WEBP'
            and data[12:16] in (b'VP8 ', b'VP8L', b'VP8X') and struct.unpack('<I', data[4:8])[0] + 8 == len(data)):
        return 'image/webp'
    raise ValueError('图片不是可识别的 PNG、JPEG、GIF 或 WebP 内容')


def safe_image_url(url: str) -> str:
    try:
        parsed = urlsplit(url)
        host = parsed.hostname or ''
        if (parsed.scheme != 'https' or not (host == 'chaoxing.com' or host.endswith('.chaoxing.com'))
                or parsed.username or parsed.password or parsed.port not in (None, 443)):
            raise ValueError()
    except (TypeError, ValueError):
        raise ValueError('图片地址不是受支持的学习通 HTTPS 来源') from None
    return url


def _cache(data_dir, *, create=False):
    if not data_dir:
        raise ValueError('缺少图片数据目录')
    base = Path(data_dir).expanduser().resolve()
    if not base.is_dir():
        raise ValueError('图片数据目录不存在')
    cache = base / 'assignment-images'
    if create:
        try:
            cache.mkdir(mode=0o700)
        except FileExistsError:
            pass
    try:
        fd = os.open(cache, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError:
        raise ValueError('图片缓存目录不存在或不安全') from None
    info = os.fstat(fd)
    if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
        os.close(fd)
        raise ValueError('图片缓存目录必须由当前用户持有且权限为 0700')
    return cache, fd


def _read_file(fd, name):
    try:
        handle = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
    except OSError:
        raise ValueError('缓存图片缺失或不是安全文件') from None
    try:
        info = os.fstat(handle)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1
                or stat.S_IMODE(info.st_mode) != 0o600 or not 0 < info.st_size <= MAX_IMAGE_BYTES):
            raise ValueError('缓存图片类型、权限或大小不符合要求')
        chunks, size = [], 0
        while True:
            chunk = os.read(handle, min(65536, MAX_IMAGE_BYTES + 1 - size))
            if not chunk:
                break
            size += len(chunk)
            if size > MAX_IMAGE_BYTES:
                raise ValueError('图片超过 8 MiB 限制')
            chunks.append(chunk)
        return b''.join(chunks)
    finally:
        os.close(handle)


def normalize_images(images, *, data_dir) -> list:
    """Validate every ready image; preserve metadata and fail closed on missing/changed files."""
    if not isinstance(images, list) or len(images) > MAX_QUESTION_IMAGES:
        raise ValueError('题目图片列表不合法或超过 16 张')
    if not images:
        return []
    cache, fd = _cache(data_dir)
    normalized, total = [], 0
    try:
        for image in images:
            if not isinstance(image, dict) or image.get('status') != 'ready':
                raise ValueError('题目中有未成功读取的图片')
            local = image.get('local_path')
            digest = image.get('sha256')
            mime = image.get('mime')
            if not isinstance(local, str) or not isinstance(digest, str) or not re.fullmatch('[a-f0-9]{64}', digest):
                raise ValueError('图片缺少本地路径或真实 hash')
            path = Path(local)
            if not path.is_absolute() or path.parent != cache or mime not in MIME_EXTENSIONS:
                raise ValueError('图片路径越出私有缓存目录或类型不合法')
            if path.name != digest + '.' + MIME_EXTENSIONS[mime]:
                raise ValueError('图片文件名与 hash 不匹配')
            data = _read_file(fd, path.name)
            if image_type(data) != mime or hashlib.sha256(data).hexdigest() != digest:
                raise ValueError('缓存图片的类型或内容已改变，请重新读取题目')
            total += len(data)
            if total > MAX_QUESTION_BYTES:
                raise ValueError('题目图片总大小超过 24 MiB')
            normalized.append(dict(image, local_path=str(cache / path.name)))
    finally:
        os.close(fd)
    return normalized


def _inline_bytes(source):
    encoded_limit = MAX_IMAGE_BYTES * 4 // 3 + 1024 if ';base64,' in source[:100] else MAX_IMAGE_BYTES * 3 + 1024
    if len(source) > encoded_limit:
        raise ValueError('内联图片超过 8 MiB 限制')
    match = re.fullmatch(r'data:(image/(?:png|jpeg|gif|webp))(;base64)?,(.*)', source, re.I | re.S)
    if not match:
        raise ValueError('不支持该内联图片格式')
    try:
        data = base64.b64decode(re.sub(r'\s', '', match[3]), validate=True) if match[2] else unquote_to_bytes(match[3])
    except (ValueError, TypeError):
        raise ValueError('内联图片编码无效') from None
    if len(data) > MAX_IMAGE_BYTES:
        raise ValueError('内联图片超过 8 MiB 限制')
    if image_type(data) != match[1].lower():
        raise ValueError('内联图片声明类型与真实内容不符')
    return data


def _download(source, session):
    url = safe_image_url(source)
    deadline = time.monotonic() + 25
    for _ in range(6):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ValueError('图片下载超时')
        response = session.get(safe_image_url(url), stream=True, allow_redirects=False,
                               timeout=(min(5, remaining), min(10, remaining)), headers={'Accept': 'image/*'})
        try:
            if response.status_code in (301, 302, 303, 307, 308):
                location = response.headers.get('Location')
                if not location:
                    raise ValueError('图片重定向没有地址')
                url = safe_image_url(urljoin(url, location))
                continue
            if response.status_code != 200:
                raise ValueError(f'图片请求返回 HTTP {response.status_code}')
            length = response.headers.get('Content-Length', '')
            if str(length).isdigit() and int(length) > MAX_IMAGE_BYTES:
                raise ValueError('图片超过 8 MiB 限制')
            chunks, size = [], 0
            for chunk in response.iter_content(chunk_size=65536):
                if time.monotonic() > deadline:
                    raise ValueError('图片下载超时')
                if not chunk:
                    continue
                size += len(chunk)
                if size > MAX_IMAGE_BYTES:
                    raise ValueError('图片超过 8 MiB 限制')
                chunks.append(chunk)
            return b''.join(chunks)
        finally:
            response.close()
    raise ValueError('图片重定向次数超限')


def materialize_images(images, *, data_dir, session) -> list:
    """Download each DOM image without dropping failed/excess entries from the result."""
    result, total = [], 0
    try:
        cache, fd = _cache(data_dir, create=True)
    except (ValueError, OSError):
        failures = []
        for original in images:
            image = dict(original, status='error', mime='', local_path='', sha256='', error='图片缓存目录不可用或权限不安全')
            source = str(image.get('source_url') or '')
            if source.startswith('data:'):
                image['source_url'] = 'data:image;sha256=' + hashlib.sha256(source.encode()).hexdigest()
            failures.append(image)
        return failures
    try:
        for index, original in enumerate(images):
            image = dict(original, status='error', mime='', local_path='', sha256='')
            source = str(original.get('source_url') or '')
            if source.startswith('data:'):
                image['source_url'] = 'data:image;sha256=' + hashlib.sha256(source.encode()).hexdigest()
            try:
                if index >= MAX_QUESTION_IMAGES:
                    raise ValueError('每题最多支持 16 张图片')
                data = _inline_bytes(source) if source.startswith('data:') else _download(source, session)
                if total + len(data) > MAX_QUESTION_BYTES:
                    raise ValueError('每题图片总大小超过 24 MiB')
                mime = image_type(data)
                digest = hashlib.sha256(data).hexdigest()
                name = digest + '.' + MIME_EXTENSIONS[mime]
                temporary = '.image-' + secrets.token_hex(12)
                handle = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=fd)
                try:
                    with os.fdopen(handle, 'wb') as output:
                        output.write(data)
                        output.flush()
                        os.fsync(output.fileno())
                        os.fchmod(output.fileno(), 0o600)
                    os.replace(temporary, name, src_dir_fd=fd, dst_dir_fd=fd)
                finally:
                    try:
                        os.unlink(temporary, dir_fd=fd)
                    except FileNotFoundError:
                        pass
                image.update(status='ready', mime=mime, local_path=str(cache / name), sha256=digest)
                image.pop('error', None)
                if source.startswith('data:'):
                    image['source_url'] = 'data:' + mime + ';sha256=' + digest
                total += len(data)
            except ValueError as exc:
                image['error'] = str(exc)
            except Exception as exc:
                image['error'] = f'图片读取失败（{type(exc).__name__}）'
            result.append(image)
    finally:
        os.close(fd)
    return result


def image_status(images) -> dict:
    count, ready = len(images), sum(image.get('status') == 'ready' for image in images)
    return {'state': 'none' if not count else 'ready' if ready == count else 'partial' if ready else 'failed',
            'count': count, 'ready': ready}


def dom_image_source(node, page_url):
    for attribute in ('data-src', 'data-original', 'data-lazy-src', 'data-url', 'lazy-src'):
        if str(node.get(attribute, '')).strip():
            return urljoin(page_url, str(node[attribute]).strip())
    srcset = str(node.get('data-srcset') or node.get('srcset') or '')
    picture = node.find_parent('picture')
    if not srcset and picture:
        source = picture.find('source', attrs={'srcset': True})
        srcset = source['srcset'] if source else ''
    if srcset:
        if srcset.startswith('data:'):
            return srcset.split()[0]
        choices = []
        for item in srcset.split(','):
            parts = item.strip().split()
            if parts:
                weight = re.match(r'(\d+(?:\.\d+)?)[wx]', parts[1]) if len(parts) > 1 else None
                choices.append((float(weight[1]) if weight else 1, parts[0]))
        if choices:
            return urljoin(page_url, max(choices)[1])
    return urljoin(page_url, str(node.get('src') or '').strip()) if node.get('src') else ''
