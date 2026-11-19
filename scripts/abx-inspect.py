#!/usr/bin/env python3
"""Read packages.xml; emit only Chaoxing's version/path/certificate identities.

Supports Android ABX v0 and ordinary XML. Never writes Android data, emits other
packages, or prints certificate material from unrelated packages.
"""

import argparse
import hashlib
import json
import struct
import sys
import xml.etree.ElementTree as ET
from pathlib import Path


PACKAGE = "com.chaoxing.mobile"


class AbxReader:
    def __init__(self, data):
        if not data.startswith(b"ABX\x00"):
            raise ValueError("Unsupported ABX version")
        self.data = data
        self.pos = 4
        self.strings = []

    def read(self, size):
        if size < 0 or self.pos + size > len(self.data):
            raise ValueError("Truncated ABX input")
        value = self.data[self.pos:self.pos + size]
        self.pos += size
        return value

    def integer(self, size, signed=False):
        return int.from_bytes(self.read(size), "big", signed=signed)

    def utf(self):
        # Java's modified UTF-8 uses C0 80 for NUL and may encode surrogates.
        raw = self.read(self.integer(2)).replace(b"\xc0\x80", b"\x00")
        value = raw.decode("utf-8", "surrogatepass")
        return value.encode("utf-16", "surrogatepass").decode("utf-16")

    def interned(self):
        index = self.integer(2)
        if index == 0xFFFF:
            value = self.utf()
            self.strings.append(value)
            return value
        if index >= len(self.strings):
            raise ValueError("Invalid ABX string reference")
        return self.strings[index]

    def value(self, kind):
        if kind == 1:
            return ""
        if kind == 2:
            return self.utf()
        if kind == 3:
            return self.interned()
        if kind in (4, 5):
            return self.read(self.integer(2)).hex()
        if kind in (6, 7):
            return str(self.integer(4, signed=kind == 6))
        if kind in (8, 9):
            return str(self.integer(8, signed=kind == 8))
        if kind == 10:
            return str(struct.unpack(">f", self.read(4))[0])
        if kind == 11:
            return str(struct.unpack(">d", self.read(8))[0])
        if kind in (12, 13):
            return "true" if kind == 12 else "false"
        raise ValueError("Unsupported ABX value type")

    def parse(self):
        stack = []
        root = None
        while self.pos < len(self.data):
            token = self.integer(1)
            event, kind = token & 0x0F, token >> 4
            if event == 15:  # ATTRIBUTE: name always uses the interned table.
                if not stack:
                    raise ValueError("ABX attribute outside an element")
                name = self.interned()
                stack[-1].set(name, self.value(kind))
            elif event == 2:
                node = ET.Element(self.interned())
                if stack:
                    stack[-1].append(node)
                elif root is None:
                    root = node
                else:
                    raise ValueError("Multiple ABX roots")
                stack.append(node)
            elif event == 3:
                name = self.interned()
                if not stack or stack.pop().tag != name:
                    raise ValueError("Mismatched ABX element")
            elif event == 1:
                break
            elif event != 0:
                self.value(kind)  # Text/comments are irrelevant to package identity.
        if root is None or stack:
            raise ValueError("Incomplete ABX document")
        return root


def inspect(path):
    data = path.read_bytes()
    root = AbxReader(data).parse() if data.startswith(b"ABX") else ET.fromstring(data)
    matches = [p for p in root.findall("package") if p.get("name") == PACKAGE]
    if len(matches) != 1:
        return {"package": PACKAGE, "found": False, "match_count": len(matches)}

    # Android serializes a shared certificate-index table across the whole file.
    # Read it internally to resolve target references, but emit only target hashes.
    certificates = {}
    for cert in root.iter("cert"):
        if cert.get("index") is not None and cert.get("key") is not None:
            certificates[cert.get("index")] = cert.get("key")

    def cert_identity(cert):
        index = cert.get("index")
        key = cert.get("key") or certificates.get(index)
        result = {"index": index, "resolved": key is not None}
        if key is not None:
            try:
                result["certificate_sha256"] = hashlib.sha256(bytes.fromhex(key)).hexdigest()
            except ValueError:
                raise ValueError("Target certificate is not valid hexadecimal") from None
        return result

    package = matches[0]
    result = {"package": PACKAGE, "found": True}
    for name in ("version", "versionCode", "versionMajor", "versionName", "codePath"):
        if name in package.attrib:
            result[name] = package.get(name)
    sigs = package.find("sigs")
    result["signatures"] = [] if sigs is None else [cert_identity(c) for c in sigs.findall("cert")]
    past = None if sigs is None else sigs.find("pastSigs")
    result["past_signatures"] = [] if past is None else [cert_identity(c) for c in past.findall("cert")]
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--path", type=Path, default=Path.home() / ".local/share/waydroid/data/system/packages.xml")
    args = parser.parse_args()
    try:
        result = inspect(args.path)
    except (OSError, ValueError, ET.ParseError, UnicodeError, struct.error) as error:
        # Never dump XML fragments or raw parser errors that could contain data.
        print(json.dumps({"package": PACKAGE, "error": type(error).__name__}), file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0 if result["found"] else 2


if __name__ == "__main__":
    sys.exit(main())
