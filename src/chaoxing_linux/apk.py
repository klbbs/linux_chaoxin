"""Read APK metadata without extracting, executing, or authenticating the APK."""
from __future__ import annotations

import hashlib
import os
from pathlib import PurePosixPath
import re
import stat
import struct
import xml.etree.ElementTree as ET
import zipfile
import zlib

ANDROID_NS = 'http://schemas.android.com/apk/res/android'
PACKAGE = 'com.chaoxing.mobile'
MAX_MANIFEST = 16 * 1024 * 1024
NO_INDEX = 0xFFFFFFFF


def _chunk(data: bytes, offset: int, end: int) -> tuple[int, int, int]:
    if offset < 0 or offset + 8 > end:
        raise ValueError('Android XML 块头已截断。')
    kind, header_size, size = struct.unpack_from('<HHI', data, offset)
    if header_size < 8 or size < header_size or offset + size > end:
        raise ValueError('Android XML 块长度无效。')
    return kind, header_size, size


def _string_pool(data: bytes, start: int, header_size: int, size: int) -> list[str]:
    if header_size < 28:
        raise ValueError('Android XML 字符串池头无效。')
    count, style_count, flags, strings_start, styles_start = struct.unpack_from('<5I', data, start + 8)
    if count > 200_000 or style_count > 200_000:
        raise ValueError('Android XML 字符串池条目超过安全上限。')
    offsets_end = header_size + 4 * (count + style_count)
    if offsets_end > size or strings_start < offsets_end or strings_start > size:
        raise ValueError('Android XML 字符串池偏移无效。')
    if styles_start and not strings_start <= styles_start <= size:
        raise ValueError('Android XML 样式池偏移无效。')
    strings_end = start + (styles_start or size)
    utf8 = bool(flags & 0x100)

    def read_length(pos: int, wide: bool) -> tuple[int, int]:
        width = 2 if wide else 1
        if pos + width > strings_end:
            raise ValueError('Android XML 字符串长度已截断。')
        first = int.from_bytes(data[pos:pos + width], 'little')
        pos += width
        mask = 0x8000 if wide else 0x80
        if first & mask:
            if pos + width > strings_end:
                raise ValueError('Android XML 字符串长度已截断。')
            second = int.from_bytes(data[pos:pos + width], 'little')
            return ((first & (mask - 1)) << (16 if wide else 8)) | second, pos + width
        return first, pos

    strings = []
    decoded_bytes = 0
    for index in range(count):
        relative = struct.unpack_from('<I', data, start + header_size + 4 * index)[0]
        pos = start + strings_start + relative
        if not start + strings_start <= pos < strings_end:
            raise ValueError('Android XML 字符串偏移越界。')
        characters, pos = read_length(pos, not utf8)
        if utf8:
            length, pos = read_length(pos, False)
            terminator = b'\x00'
            encoding = 'utf-8'
        else:
            length = characters * 2
            terminator = b'\x00\x00'
            encoding = 'utf-16-le'
        if pos + length + len(terminator) > strings_end:
            raise ValueError('Android XML 字符串已截断。')
        if data[pos + length:pos + length + len(terminator)] != terminator:
            raise ValueError('Android XML 字符串缺少结束标记。')
        # Offsets may alias the same long string: cap aggregate work/allocation too.
        decoded_bytes += length
        if decoded_bytes > MAX_MANIFEST * 4:
            raise ValueError('Android XML 字符串展开超过安全上限。')
        try:
            decoded = data[pos:pos + length].decode(encoding, errors='strict')
        except UnicodeError as exc:
            raise ValueError('Android XML 字符串编码损坏。') from exc
        if len(decoded.encode('utf-16-le')) // 2 != characters:
            raise ValueError('Android XML 字符串字符数不一致。')
        strings.append(decoded)
    return strings


def _binary_manifest(data: bytes) -> dict:
    root_kind, root_header, total = _chunk(data, 0, len(data))
    if root_kind != 3 or total != len(data):
        raise ValueError('Android 二进制 XML 根块无效。')
    strings = None
    stack = []
    namespaces = []
    manifest = None
    root_finished = False

    def string(index: int, optional: bool = False):
        if optional and index == NO_INDEX:
            return None
        if strings is None or index >= len(strings):
            raise ValueError('Android XML 字符串索引越界。')
        return strings[index]

    position = root_header
    while position < total:
        kind, header_size, size = _chunk(data, position, total)
        end = position + size
        if kind == 1:
            if strings is not None or stack or manifest is not None:
                raise ValueError('Android XML 字符串池重复或顺序错误。')
            strings = _string_pool(data, position, header_size, size)
        elif kind in (0x100, 0x101, 0x102, 0x103, 0x104):
            if strings is None or header_size < 16:
                raise ValueError('Android XML 节点头无效。')
            extension = position + header_size
            if kind == 0x102:
                if root_finished or extension + 20 > end:
                    raise ValueError('Android XML 元素无效。')
                namespace, name, attr_start, attr_size, count = struct.unpack_from('<IIHHH', data, extension)
                node = (string(namespace, True), string(name))
                if attr_start < 20 or attr_size < 20 or extension + attr_start + count * attr_size > end:
                    raise ValueError('Android XML 属性范围越界。')
                attributes = {}
                for index in range(count):
                    attr = extension + attr_start + index * attr_size
                    ns, attr_name, raw = struct.unpack_from('<III', data, attr)
                    value_size, reserved, value_type, value = struct.unpack_from('<HBBI', data, attr + 12)
                    if value_size != 8 or reserved:
                        raise ValueError('Android XML 属性值格式无效。')
                    key = (string(ns, True), string(attr_name))
                    raw_value = string(raw, True)
                    typed_value = string(value) if value_type == 3 else value
                    if key in attributes:
                        raise ValueError('Android XML 属性重复。')
                    attributes[key] = raw_value if raw_value is not None else typed_value
                if not stack:
                    if manifest is not None or node != (None, 'manifest'):
                        raise ValueError('AndroidManifest.xml 缺少 manifest 根元素。')
                    manifest = attributes
                stack.append(node)
            elif kind == 0x103:
                if extension + 8 > end:
                    raise ValueError('Android XML 元素结束节点已截断。')
                ns, name = struct.unpack_from('<II', data, extension)
                if not stack or stack.pop() != (string(ns, True), string(name)):
                    raise ValueError('Android XML 元素未正确闭合。')
                if not stack:
                    root_finished = True
            elif kind in (0x100, 0x101):
                if extension + 8 > end:
                    raise ValueError('Android XML 命名空间节点已截断。')
                prefix, uri = struct.unpack_from('<II', data, extension)
                namespace = (string(prefix, True), string(uri))
                if kind == 0x100:
                    if root_finished:
                        raise ValueError('Android XML 命名空间顺序错误。')
                    namespaces.append(namespace)
                elif not namespaces or namespaces.pop() != namespace:
                    raise ValueError('Android XML 命名空间未正确闭合。')
            else:
                if extension + 12 > end or not stack:
                    raise ValueError('Android XML 文本节点无效。')
                string(struct.unpack_from('<I', data, extension)[0])
        elif kind == 0x180:
            if strings is None or manifest is not None or header_size != 8 or (size - 8) % 4:
                raise ValueError('Android XML 资源映射无效。')
        else:
            raise ValueError('不支持或损坏的 Android XML 块。')
        position = end
    if manifest is None or stack or namespaces or not root_finished:
        raise ValueError('AndroidManifest.xml 缺失或未闭合。')
    return manifest


def _manifest_metadata(data: bytes) -> tuple[str, str, int]:
    if not data:
        raise ValueError('AndroidManifest.xml 为空。')
    if data[:2] == b'\x03\x00':
        attributes = _binary_manifest(data)
        package = attributes.get((None, 'package'), '')
        version_name = attributes.get((ANDROID_NS, 'versionName'), '')
        version_code = attributes.get((ANDROID_NS, 'versionCode'), 0)
    else:
        if re.search(br'<!\s*(?:DOCTYPE|ENTITY)\b', data, flags=re.IGNORECASE):
            raise ValueError('AndroidManifest.xml 不允许 DTD 或实体声明。')
        try:
            root = ET.fromstring(data)
        except (ET.ParseError, ValueError) as exc:
            raise ValueError('AndroidManifest.xml 文本 XML 损坏。') from exc
        if root.tag != 'manifest':
            raise ValueError('AndroidManifest.xml 缺少 manifest 根元素。')
        package = root.attrib.get('package', '')
        version_name = root.attrib.get(f'{{{ANDROID_NS}}}versionName', '')
        version_code = root.attrib.get(f'{{{ANDROID_NS}}}versionCode', '0')
    if not isinstance(package, str) or package != PACKAGE:
        raise ValueError('此 APK 的包名不是 com.chaoxing.mobile，不能作为学习通安装。')
    try:
        code = version_code if isinstance(version_code, int) else int(version_code, 16 if str(version_code).startswith('0x') else 10)
    except (TypeError, ValueError) as exc:
        raise ValueError('APK 版本号无效。') from exc
    if code < 0 or code > 0xFFFFFFFF:
        raise ValueError('APK 版本号超出范围。')
    return package, str(version_name), code


def inspect_apk(path: os.PathLike | str) -> dict:
    """Inspect structure/package/ABIs only; this does NOT verify publisher signatures."""
    try:
        with open(path, 'rb') as source:
            byte_count = os.fstat(source.fileno()).st_size
            if not byte_count or not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
                raise ValueError('APK 文件为空或不是普通文件。')
            digest = hashlib.sha256()
            for block in iter(lambda: source.read(1024 * 1024), b''):
                digest.update(block)
            source.seek(0)
            with zipfile.ZipFile(source) as archive:
                entries = archive.infolist()
                if not entries:
                    raise ValueError('APK 压缩包为空。')
                seen = set()
                abis = set()
                dex_count = 0
                manifest_entry = None
                for entry in entries:
                    name = entry.filename
                    components = name.rstrip('/').split('/')
                    if (not name or '\\' in name or PurePosixPath(name).is_absolute()
                            or any(part in ('', '.', '..') for part in components)
                            or ':' in components[0] or '\x00' in entry.orig_filename
                            or stat.S_ISLNK(entry.external_attr >> 16)):
                        raise ValueError('APK 包含不安全的文件路径。')
                    if name in seen:
                        raise ValueError('APK 包含重复文件路径。')
                    seen.add(name)
                    if name == 'AndroidManifest.xml':
                        manifest_entry = entry
                    if re.fullmatch(r'classes(?:[2-9]|[1-9][0-9]+)?\.dex', name):
                        dex_count += 1
                    if name.startswith('lib/') and name.endswith('.so'):
                        match = re.fullmatch(r'lib/([A-Za-z0-9_+\-]+)/[^/]+\.so', name)
                        if not match:
                            raise ValueError('APK native 库路径不符合 lib/<ABI>/*.so 格式。')
                        abis.add(match.group(1))
                if manifest_entry is None:
                    raise ValueError('APK 缺少 AndroidManifest.xml。')
                if manifest_entry.file_size <= 0 or manifest_entry.file_size > MAX_MANIFEST:
                    raise ValueError('AndroidManifest.xml 为空或超过 16 MiB 安全上限。')
                with archive.open(manifest_entry) as manifest_source:
                    data = manifest_source.read(MAX_MANIFEST + 1)
                if len(data) > MAX_MANIFEST:
                    raise ValueError('AndroidManifest.xml 超过安全上限。')
                package, version_name, version_code = _manifest_metadata(data)
    except (OSError, zipfile.BadZipFile, RuntimeError, NotImplementedError, EOFError, struct.error, zlib.error) as exc:
        raise ValueError(f'无法读取 APK：{exc}') from exc
    return {'package': package, 'version_name': version_name, 'version_code': version_code,
            'abis': sorted(abis), 'bytes': byte_count, 'sha256': digest.hexdigest(), 'dex_count': dex_count}


def compatibility(apk_info: dict, runtime_abis) -> bool:
    """No native libraries means universal; otherwise require a reported ABI match."""
    apk_abis = set(apk_info.get('abis', []))
    if not apk_abis:
        return True
    if isinstance(runtime_abis, str):
        runtime_abis = [part.strip() for part in runtime_abis.split(',') if part.strip()]
    return bool(apk_abis.intersection(runtime_abis or []))
