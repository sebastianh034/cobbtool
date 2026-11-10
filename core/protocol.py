import os as _os, sys as _sys  # repo-layout bootstrap (added by reorg)
_R = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
_sys.path[:0] = [_os.path.join(_R, _d) for _d in ("core", "tools", "emulator", "recon")]; _os.chdir(_R)
"""
Corrected Accessport wire protocol.

Frame:  \x02 + uint32_BE(total) + \x00 + cmd(1) + payload + crc32_LE(4)
where   total = 1 + len(payload) + 3

The original client.py read this as MAGIC(4)=\x02\x00\x00\x00 + uint16_LE(len) at
offset 4. That is numerically identical only while total < 256, so every packet
built the old way with a payload over ~252 bytes is malformed.
"""
import struct
import zlib

BOOST_HEADER = (b'\x16\x00\x00\x00' + b'serialization::archive'
                + b'\x03\x04\x04\x04\x08\x01\x00\x00\x00')


def build(cmd: int, payload: bytes = b'') -> bytes:
    total = 1 + len(payload) + 3
    head = b'\x02' + struct.pack('>I', total) + b'\x00' + bytes([cmd])
    body = head + payload
    return body + struct.pack('<I', zlib.crc32(body) & 0xFFFFFFFF)


def build_boost(cmd: int, args: bytes = b'') -> bytes:
    return build(cmd, BOOST_HEADER + args)


def bstr(s: str) -> bytes:
    """Boost-serialized std::string: uint32_LE length + bytes."""
    enc = s.encode()
    return struct.pack('<I', len(enc)) + enc


def parse(frame: bytes):
    """Return (cmd, data) for one frame, or None."""
    if len(frame) < 7 or frame[0] != 0x02:
        return None
    total = int.from_bytes(frame[1:5], 'big')
    return frame[6], frame[7:7 + total - 4]


def iter_frames(stream: bytes):
    pos = 0
    while pos + 7 <= len(stream) and stream[pos] == 0x02:
        total = int.from_bytes(stream[pos + 1:pos + 5], 'big')
        yield stream[pos + 6], stream[pos + 7:pos + 7 + total - 4]
        pos += 7 + (total - 4) + 4


if __name__ == "__main__":
    from client import RAW_CMDS

    checks = {
        "version": build(0x04),
        "vehicle": build(0x03),
        "device":  build(0x28),
        "show_cur": build_boost(0x1a, bstr("show_cur no_comms")),
        "list_root": build_boost(0x26, bstr("")),
        "list_maps": build_boost(0x26, bstr("maps")),
        "list_datalog": build_boost(0x26, bstr("datalog")),
        "list_screenshots": build_boost(0x26, bstr("screenshots")),
    }
    ok = True
    for name, built in checks.items():
        want = RAW_CMDS[name]
        match = built == want
        ok &= match
        print(f"  {name:<18} {'OK' if match else 'MISMATCH'}")
        if not match:
            print(f"     built {built.hex()}")
            print(f"     want  {want.hex()}")
    print("\nall match" if ok else "\nMISMATCHES PRESENT")
