import os as _os, sys as _sys  # repo-layout bootstrap (added by reorg)
_R = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
_sys.path[:0] = [_os.path.join(_R, _d) for _d in ("core", "tools", "emulator", "recon")]; _os.chdir(_R)
"""
Full backup of every USB-accessible file on the Accessport.
Read-only: uses cmd 0x26 (list dir), 0x20 (stat), 0x21 (download).

Listing entry layout (reverse-engineered):
  name_len(4) + name + mtime(4) + size(4) + <padding/markers> + path_len(4) + path
Padding length varies with Boost item-version markers, so entries are delimited by
locating the trailing full path instead of assuming a fixed stride.

Download stream framing:
  MAGIC(4) + len_field(2) + 0x01 + file_bytes + CRC(4),  len_field = len(body)+3
"""
import hashlib
import json
import re
import struct
import time
from pathlib import Path

import usb.core
import usb.util

from client import (
    open_device, flush_input, send, raw_cmd, parse_response,
    build_serialized, build_stat_pkt, build_download_pkt,
    MAGIC, EP_OUT, EP_IN, TIMEOUT, CMD_LIST_DIR,
)

OUT_DIR = Path(".") / "backup"
MD5_RE = re.compile(rb'^[0-9a-f]{32}\n?$')


def parse_dir_listing(resp: bytes, dirname: str) -> list[dict]:
    marker = b'serialization::archive'
    idx = resp.find(marker)
    if idx == -1:
        return []
    body = resp[idx + len(marker) + 9:]
    if len(body) < 6:
        return []
    count = struct.unpack_from('<I', body, 2)[0]
    pos = 6
    if body[pos:pos + 2] == b'\x00\x01':
        pos += 2

    entries = []
    for _ in range(count):
        if pos + 4 > len(body):
            break
        name_len = struct.unpack_from('<I', body, pos)[0]
        if not (1 <= name_len <= 255) or pos + 4 + name_len + 8 > len(body):
            break
        name = body[pos + 4:pos + 4 + name_len].decode('ascii', errors='replace')
        p = pos + 4 + name_len
        mtime, size = struct.unpack_from('<II', body, p)

        full = f"{dirname}/{name}" if dirname else name
        needle = struct.pack('<I', len(full)) + full.encode()
        hit = body.find(needle, p + 8)
        if hit == -1:
            break
        entries.append({"name": name, "path": full, "size": size, "mtime": mtime})
        pos = hit + len(needle)
    return entries


def list_dir(dev, path: str, strict: bool = False) -> list[dict]:
    flush_input(dev, 300)
    response = send(dev, build_serialized(CMD_LIST_DIR, path))
    entries = parse_dir_listing(response, path)
    if strict:
        marker = b'serialization::archive'
        idx = response.find(marker)
        offset = idx + len(marker) + 9 + 2
        if idx < 0 or len(response) < offset + 4:
            raise ValueError("malformed directory listing")
        count = struct.unpack_from('<I', response, offset)[0]
        if count != len(entries) or len({e['name'] for e in entries}) != count:
            raise ValueError("incomplete or duplicate directory listing")
    return entries


def iter_frames(stream: bytes):
    """
    Yield (kind, data) per frame.
      0x02 + uint32_BE(len) + 0x00 + kind(1) + data + crc32(4),  len = len(data)+4
    The length is 32-bit big-endian — a 16-bit LE read only happens to work for
    packets under 256 bytes.
    """
    pos = 0
    while pos + 7 <= len(stream):
        if stream[pos] != 0x02:
            break
        total = int.from_bytes(stream[pos + 1:pos + 5], 'big')
        kind = stream[pos + 6]
        data = stream[pos + 7:pos + 7 + total - 4]
        yield kind, data
        pos += 7 + (total - 4) + 4


def download(dev, path: str, size: int, debug: bool = False) -> bytes:
    flush_input(dev, 300)
    stat_resp = send(dev, build_stat_pkt(path))
    if not stat_resp or stat_resp[:4] != MAGIC:
        print(f"     [!] bad stat response: {stat_resp[:32].hex()}")
        return b''

    dev.write(EP_OUT, build_download_pkt(f'C:/temp/{Path(path).name}'), timeout=TIMEOUT)

    chunks = []
    while True:
        try:
            chunk = bytes(dev.read(EP_IN, 512, timeout=TIMEOUT))
        except usb.core.USBTimeoutError:
            break
        chunks.append(chunk)
        if debug:
            print(f"     chunk {len(chunk)}B {chunk[:16].hex()}")

    # The trailing MD5 ack looks exactly like file content when the file itself
    # holds a hash (backupcksum), so only frames past the expected size can be it.
    parts, md5_ack, got = [], None, 0
    for kind, frame in iter_frames(b''.join(chunks)):
        if got >= size and MD5_RE.match(frame.strip()):
            md5_ack = frame.strip().decode()
        else:
            parts.append(frame)
            got += len(frame)

    data = b''.join(parts)
    if size and len(data) != size:
        print(f"     [!] size mismatch: got {len(data)}B, expected {size}B")
    if md5_ack:
        actual = hashlib.md5(data).hexdigest()
        print(f"     md5 {'OK' if actual == md5_ack else f'MISMATCH {actual} != {md5_ack}'}")
    return data


def main():
    print("Connecting...")
    dev = open_device()
    flush_input(dev)
    time.sleep(0.3)
    OUT_DIR.mkdir(exist_ok=True)

    manifest = {"device": {}, "tree": {}}
    for key in ("version", "device"):
        flush_input(dev, 300)
        manifest["device"][key] = parse_response(raw_cmd(dev, key))
    print(manifest["device"]["device"])

    dirs = ["", "maps", "datalog", "screenshots", "dump"]
    for d in dirs:
        entries = list_dir(dev, d)
        manifest["tree"][d or "/"] = entries
        print(f"\n== {d or '/'} == {len(entries)} entries")
        for e in entries:
            print(f"   {e['name']:<36} {e['size']:>8}B  mtime={e['mtime']}")

    print("\n== downloading ==")
    for d in dirs:
        for e in manifest["tree"][d or "/"]:
            if e["size"] == 0:
                continue
            dest = OUT_DIR / (d or "") / e["name"]
            dest.parent.mkdir(parents=True, exist_ok=True)
            if dest.exists() and dest.stat().st_size == e["size"]:
                print(f"  skip {e['path']}")
                continue
            print(f"  >> {e['path']} ({e['size']}B)")
            try:
                data = download(dev, e["path"], e["size"])
            except Exception as ex:
                print(f"     [!] {ex}")
                continue
            if data:
                dest.write_bytes(data)
                print(f"     saved {len(data)}B")
            else:
                print("     (empty)")

    (OUT_DIR / "manifest.json").write_text(json.dumps(manifest, indent=2))
    usb.util.dispose_resources(dev)
    print(f"\nDone. Backup in {OUT_DIR}")


if __name__ == "__main__":
    main()
