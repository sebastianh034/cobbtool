import os as _os, sys as _sys  # repo-layout bootstrap (added by reorg)
_R = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
_sys.path[:0] = [_os.path.join(_R, _d) for _d in ("core", "tools", "emulator", "recon")]; _os.chdir(_R)
"""
COBB Accessport USB file-UPLOAD (write) primitive + unmarry payload builder.

Status of the write path (reverse-engineered 2026-09, from backup/rootfs libap_comms.so
+ real download captures in log/):

  * Upload is a TWO-PHASE exchange, mirroring download (setup 0x20 / data 0x21):
        cmd 0x22  = PutFileSetup   (dest filename + file size)
        cmd 0x23  = PutFile (data) (file bytes streamed in <=512KB chunks)
    then the device returns a 32-hex-char MD5 ack, same as a download.
  * 0x23 is CONFIRMED: usb::USBManagerClient::OnPutFileOUT hardcodes command 0x23
    for the data phase (`cmp r1,#2 ; moveq r1,#0x23`).
  * 0x22 setup byte-layout is RECONSTRUCTED by analogy to the captured 0x20 stat
    packet, with the file size written into the field-block the device fills on a
    download.  ***UNVERIFIED*** — the exact size offset / marker for 0x22 is not
    proven from a real capture.  Do NOT fire build_put_setup() at the device blind:
    a malformed 0x22 stalls EP 0x82 and needs a physical replug.

Safe way to make it authoritative:  capture ONE real APManager
"Upload selected map(s) to Accessport" over USB, run parse_upload_capture() on it,
and copy the exact setup bytes it prints.  Then this module can replay with the
destination path swapped to /user/ap-user/settings.

The captured 0x20 stat packet (validated) looks like, after the Boost header:
    00 01 | u32le(len) name | 27 zero bytes | u32le(len) name    (device fills the 27)
"""
import struct
import zlib
from pathlib import Path

BOOST_HEADER = (b'\x16\x00\x00\x00' + b'serialization::archive'
                + b'\x03\x04\x04\x04\x08\x01\x00\x00\x00')

CMD_PUT_SETUP = 0x22
CMD_PUT_DATA  = 0x23
CHUNK = 0x80000  # 512KB — the size doSendPacketAndFile streams per libap_comms


# ── framing (32-bit BE length — protocol.py-compatible) ───────────────────────

def build(cmd: int, payload: bytes = b'') -> bytes:
    total = 1 + len(payload) + 3
    body = b'\x02' + struct.pack('>I', total) + b'\x00' + bytes([cmd]) + payload
    return body + struct.pack('<I', zlib.crc32(body) & 0xFFFFFFFF)


def build_boost(cmd: int, args: bytes = b'') -> bytes:
    return build(cmd, BOOST_HEADER + args)


def bstr(s) -> bytes:
    enc = s.encode() if isinstance(s, str) else s
    return struct.pack('<I', len(enc)) + enc


# ── upload packet builders ────────────────────────────────────────────────────

def build_put_setup(dest_name: str, file_size: int, size_off: int = 0) -> bytes:
    """
    cmd 0x22 PutFileSetup — RECONSTRUCTED (see module docstring). `size_off` is the
    byte offset of the u32le size inside the 27-byte field block (0 = first slot);
    exposed so a capture can pin it without editing code.
    """
    enc = dest_name.encode()
    block = bytearray(27)
    struct.pack_into('<I', block, size_off, file_size)
    payload = (b'\x00\x01'
               + bstr(enc)
               + bytes(block)
               + bstr(enc))
    return build_boost(CMD_PUT_SETUP, payload)


def build_put_data(chunk: bytes) -> bytes:
    """cmd 0x23 PutFile data frame — RAW bytes, no Boost header (mirrors the download
    data frame which uses cmd 0x01 + raw payload, confirmed in log/capture2.pcapng)."""
    return build(CMD_PUT_DATA, chunk)


# ── unmarry payload ───────────────────────────────────────────────────────────

def unmarry_settings_payload(orig_settings: bytes = b'') -> bytes:
    """
    Poisoned /user/ap-user/settings whose value is command-substituted when any UI
    task reads it via get_settings_val -> get_named_out_val -> eval (sh_funcs.sh).
    Runs the exact car-independent unmarry from the uninstall script, then the
    matching cleanup, then restores a benign value so nothing loops.

    NOTE: keep this as the LAST resort content; verify state via cmd 0x28 afterward.
    """
    cmd = (
        "cd /ap-app/bin && ./writeeeprom --state=0 --vehicle=? --quiet ; "
        "./cur_map --working=/user/ap-data --clear --realtime --reflash --quiet ; "
        "rm -f /user/ap-data/reflash_props ; rm -f /user/ap-data/user.rom"
    )
    # gauge_layout is a real settings key read early by the UI.
    line = f"gauge_layout=$({cmd})\n".encode()
    return orig_settings + line


# ── capture parser: derive the REAL 0x22/0x23 bytes from an APManager upload ────

def _iter_pcapng_bulk(path):
    data = Path(path).read_bytes()
    EPS = {0x02, 0x81, 0x03, 0x82}
    off = 0
    while off < len(data) - 8:
        bt = struct.unpack_from("<I", data, off)[0]
        bl = struct.unpack_from("<I", data, off + 4)[0]
        if bl < 12 or off + bl > len(data):
            break
        if bt == 0x06 and bl >= 28:
            cl = struct.unpack_from("<I", data, off + 20)[0]
            pd = data[off + 28:off + 28 + cl]
            if len(pd) >= 27:
                hl = struct.unpack_from("<H", pd, 0)[0]
                tr, ep, info = pd[22], pd[21], pd[20]
                dl = struct.unpack_from("<I", pd, 23)[0]
                if tr == 3 and ep in EPS and dl > 0:
                    pl = pd[hl:hl + dl]
                    if pl and not all(b == 0 for b in pl):
                        yield ("IN" if info & 1 else "OUT"), ep, pl
        off += bl


def parse_upload_capture(path):
    """Print any 0x22/0x23 (upload) frames found in a USBPcap capture."""
    found = False
    for d, ep, pl in _iter_pcapng_bulk(path):
        if d == "OUT" and len(pl) >= 7 and pl[0] == 0x02 and pl[6] in (CMD_PUT_SETUP, CMD_PUT_DATA):
            found = True
            i = pl.find(BOOST_HEADER)
            after = pl[i + len(BOOST_HEADER):-4] if i >= 0 else b''
            print(f"\ncmd 0x{pl[6]:02x}  total={len(pl)}  boost_hdr@{i}")
            print(f"  full:  {pl.hex()}")
            print(f"  after: {after.hex()}")
    if not found:
        print(f"No 0x22/0x23 upload frames in {path} "
              f"(APManager read-only captures contain none).")
    return found


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1:
        parse_upload_capture(sys.argv[1])
    else:
        s = build_put_setup("/user/ap-user/settings", 123)
        print("reconstructed 0x22 setup (UNVERIFIED):")
        print(" ", s.hex())
        print("\nunmarry settings payload:")
        print(unmarry_settings_payload().decode())
