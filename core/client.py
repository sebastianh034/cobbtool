import os as _os, sys as _sys  # repo-layout bootstrap (added by reorg)
_R = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
_sys.path[:0] = [_os.path.join(_R, _d) for _d in ("core", "tools", "emulator", "recon")]; _os.chdir(_R)
"""
COBB Accessport USB client.
Cross-platform: Windows uses the bundled `libusb` pip wheel; macOS/Linux use the system
libusb (e.g. `brew install libusb`). On Windows the device needs the WinUSB driver (Zadig);
on macOS no driver setup is needed (no kernel driver claims this vendor bulk device).
Protocol: Boost.Serialization over bulk endpoints 0x03 (OUT) / 0x82 (IN).
"""
import usb.core
import usb.util
import usb.backend.libusb1
import sys
import os
import zlib
import struct
import time
import ctypes.util
from pathlib import Path

VID = 0x1A84
PID = 0x0121
EP_OUT = 0x03   # 512-byte bulk OUT
EP_IN  = 0x82   # 512-byte bulk IN
TIMEOUT = 3000

def _get_backend():
    """Find a libusb-1.0 backend across platforms.
    1) bundled `libusb` pip wheel (Windows-friendly, no system install needed)
    2) pyusb's default probe (system libusb on PATH / standard dirs)
    3) explicit Homebrew / common macOS + Linux dylib paths."""
    # 1) bundled wheel
    try:
        import libusb  # the `libusb` pip package ships the shared lib
        be = usb.backend.libusb1.get_backend(find_library=lambda x: libusb.dll._name)
        if be:
            return be
    except Exception:
        pass
    # 2) default probe (uses ctypes.util.find_library internally)
    be = usb.backend.libusb1.get_backend()
    if be:
        return be
    # 3) explicit candidate paths (Homebrew arm64/intel, MacPorts, Linux)
    cands = [
        "/opt/homebrew/lib/libusb-1.0.0.dylib",   # Apple Silicon Homebrew
        "/usr/local/lib/libusb-1.0.0.dylib",       # Intel Homebrew
        "/opt/local/lib/libusb-1.0.0.dylib",       # MacPorts
        "/usr/lib/libusb-1.0.so.0",
        "/usr/lib/x86_64-linux-gnu/libusb-1.0.so.0",
    ]
    found = ctypes.util.find_library("usb-1.0") or ctypes.util.find_library("libusb-1.0")
    if found:
        cands.insert(0, found)
    for path in cands:
        if path and os.path.exists(path):
            be = usb.backend.libusb1.get_backend(find_library=lambda x, p=path: p)
            if be:
                return be
    return None

_backend = _get_backend()

# ── Boost.Serialization packet builder ───────────────────────────────────────

MAGIC        = b'\x02\x00\x00\x00'
BOOST_HEADER = b'\x16\x00\x00\x00' + b'serialization::archive' + b'\x03\x04\x04\x04\x08\x01\x00\x00\x00'

def crc32(data: bytes) -> bytes:
    return struct.pack('<I', zlib.crc32(data) & 0xFFFFFFFF)

def build_boost_pkt(cmd_type: int, payload: bytes) -> bytes:
    """
    Wire format (reverse-engineered from capture):
      MAGIC(4) + len_field(2) + body(body_size) + CRC(4)
    where body = cmd(1) + BOOST_HEADER(35) + payload
    and   len_field = body_size + 3
    CRC is appended using zlib (device does not appear to verify it).
    """
    body      = bytes([cmd_type]) + BOOST_HEADER + payload
    len_field = len(body) + 3
    pkt       = MAGIC + struct.pack('<H', len_field) + body
    return pkt + crc32(pkt)

def build_stat_pkt(name: str) -> bytes:
    """cmd 0x20 — stat/prepare a file for download."""
    enc     = name.encode()
    # Structure: \x00\x01 + name_len(4B) + name + 27 zero bytes + fullpath_len(4B) + fullpath
    # (27 zeros hold size/type/timestamps, filled in by device in response)
    payload = (b'\x00\x01'
               + struct.pack('<I', len(enc)) + enc
               + b'\x00' * 27
               + struct.pack('<I', len(enc)) + enc)
    return build_boost_pkt(0x20, payload)

def build_download_pkt(pc_path: str) -> bytes:
    """cmd 0x21 — request file download; device streams raw bytes then sends MD5 ack."""
    enc     = pc_path.encode()
    payload = struct.pack('<I', len(enc)) + enc
    return build_boost_pkt(0x21, payload)

def build_serialized(cmd_type: int, payload: str) -> bytes:
    """Compat wrapper — string-payload Boost commands (list_dir, file_info, etc.)."""
    enc = payload.encode()
    return build_boost_pkt(cmd_type, struct.pack('<I', len(enc)) + enc)

def build_simple(cmd_type: int) -> bytes:
    """No-payload command packet: MAGIC + len + cmd + crc (matches RAW_CMDS entries).
    cmd_version/device/vehicle use this."""
    body = bytes([cmd_type])
    pkt  = MAGIC + struct.pack('<H', len(body) + 3) + body
    return pkt + crc32(pkt)

# Known command IDs (reverse-engineered from capture)
CMD_VERSION   = 0x04
CMD_VEHICLE   = 0x03
CMD_DEVICE    = 0x28
CMD_UNK       = 0x1f
CMD_SHOW_CUR  = 0x1a   # serialized, payload = "show_cur no_comms"
CMD_LIST_DIR  = 0x26   # serialized, payload = directory name
CMD_FILE_INFO = 0x12   # serialized, payload = filename
CMD_FILE_GET  = 0x20   # serialized, payload = "dest_path\0src_path" (guessed)

# ── USB I/O ───────────────────────────────────────────────────────────────────

def open_device():
    if _backend is None:
        print("[!] No libusb backend found.")
        if sys.platform == "darwin":
            print("    macOS: run `brew install libusb`  (and `pip install pyusb`).")
        elif sys.platform.startswith("linux"):
            print("    Linux: install libusb-1.0 (e.g. `sudo apt install libusb-1.0-0`).")
        else:
            print("    Windows: `pip install libusb pyusb`.")
        sys.exit(1)
    dev = usb.core.find(idVendor=VID, idProduct=PID, backend=_backend)
    if dev is None:
        print(f"[!] Accessport not found (VID {VID:#06x} PID {PID:#06x}).")
        if sys.platform == "darwin":
            print("    - Is the AP plugged into this Mac's USB and powered?")
            print("    - No driver setup needed on macOS; try a different port/cable.")
            print("    - If PermissionError follows, re-run in a terminal with USB access.")
        else:
            print("    Is the WinUSB driver installed via Zadig, and the AP plugged in?")
        sys.exit(1)
    try:
        dev.set_configuration(1)
    except usb.core.USBError:
        pass
    # macOS/Linux may bind a kernel driver to interface 0; detach it, then claim.
    # The claim is racy (the OS can re-grab it), so retry a few times.
    err = None
    for _ in range(6):
        try:
            if dev.is_kernel_driver_active(0):
                dev.detach_kernel_driver(0)
        except Exception:
            pass
        try:
            usb.util.claim_interface(dev, 0)
            err = None
            break
        except usb.core.USBError as e:
            err = e
            time.sleep(0.3)
    if err is not None:
        print(f"[!] could not claim interface 0: {err}")
        print("    macOS is holding it. Unplug/replug the AP and re-run; if it persists,")
        print("    quit any COBB app and try a different USB port.")
    clear_stalls(dev)   # a previous run may have left an endpoint HALTed
    return dev

def clear_stalls(dev):
    """Clear a HALT/stall on both bulk endpoints so a wedged transfer becomes usable
    again WITHOUT a physical replug. Best-effort; harmless if not stalled."""
    for ep in (EP_OUT, EP_IN):
        try:
            dev.clear_halt(ep)
        except Exception:
            pass

def reset_device(dev):
    """Full USB reset (device re-enumerates) — recovers a wedge that clear_halt can't.
    Returns a fresh handle, or None if it hasn't re-enumerated yet."""
    clear_stalls(dev)
    try:
        dev.reset()
    except Exception:
        pass
    try:
        usb.util.dispose_resources(dev)
    except Exception:
        pass
    time.sleep(2)
    for _ in range(10):
        d = usb.core.find(idVendor=VID, idProduct=PID, backend=_backend)
        if d is not None:
            try:
                d.set_configuration(1)
            except Exception:
                pass
            clear_stalls(d)
            return d
        time.sleep(1)
    return None

def flush_input(dev, timeout_ms: int = 500):
    """Discard any leftover data sitting in the IN endpoint buffer."""
    while True:
        try:
            dev.read(EP_IN, 512, timeout=timeout_ms)
        except (usb.core.USBTimeoutError, usb.core.USBError):
            break

def send(dev, pkt: bytes) -> bytes:
    dev.write(EP_OUT, pkt, timeout=TIMEOUT)
    chunks = []
    while True:
        try:
            chunk = bytes(dev.read(EP_IN, 512, timeout=TIMEOUT))
            chunks.append(chunk)
            if len(chunk) < 512:
                break
        except usb.core.USBTimeoutError:
            break
    return b''.join(chunks)

def send_stream(dev, pkt: bytes) -> str:
    """Send a command and read streaming text lines until RUNTASK_FINISH."""
    dev.write(EP_OUT, pkt, timeout=TIMEOUT)
    lines = []
    while True:
        try:
            chunk = bytes(dev.read(EP_IN, 512, timeout=TIMEOUT))
            text  = chunk.decode('utf-8', errors='replace').strip('\x00')
            lines.append(text)
            if '[RUNTASK_FINISH]' in text:
                break
        except usb.core.USBTimeoutError:
            break
    return ''.join(lines)

def parse_response(data: bytes) -> str | None:
    """Extract the string payload from a Boost.Serialization response."""
    marker = b'serialization::archive'
    idx = data.find(marker)
    if idx == -1:
        return data.decode('utf-8', errors='replace').strip('\x00')
    after = data[idx + len(marker):]
    # Skip 9 version bytes
    after = after[9:]
    if len(after) < 4:
        return None
    strlen = struct.unpack_from('<I', after)[0]
    if strlen == 0 or strlen > 65536:
        return None
    return after[4:4 + strlen].decode('utf-8', errors='replace')

# ── Commands ──────────────────────────────────────────────────────────────────

def cmd_version(dev) -> str:
    resp = send(dev, build_simple(CMD_VERSION))
    return parse_response(resp) or "<no response>"

def cmd_device_info(dev) -> str:
    resp = send(dev, build_simple(CMD_DEVICE))
    return parse_response(resp) or "<no response>"

def cmd_vehicle_info(dev) -> str:
    resp = send(dev, build_simple(CMD_VEHICLE))
    return parse_response(resp) or "<no response>"

def cmd_list_dir(dev, path: str = "") -> list[str]:
    resp = send(dev, build_serialized(CMD_LIST_DIR, path))
    raw  = parse_response(resp)
    if not raw:
        return []
    # Directory entries are embedded as filenames — extract readable strings
    results = []
    for chunk in raw.split('\x00'):
        chunk = chunk.strip()
        if chunk and len(chunk) > 1 and not any(c < ' ' for c in chunk):
            results.append(chunk)
    return results

def cmd_file_info(dev, filename: str) -> str:
    resp = send(dev, build_serialized(CMD_FILE_INFO, filename))
    return parse_response(resp) or "<no response>"

def cmd_show_current(dev) -> bytes:
    return send(dev, build_serialized(CMD_SHOW_CUR, "show_cur no_comms"))

def download_file(dev, name: str, debug: bool = False) -> bytes:
    """
    Download a file from the Accessport using the two-step protocol:
      1. cmd 0x20 (stat)   — prepares the file, device returns stat info
      2. cmd 0x21 (download) — device streams raw file bytes, then sends MD5 ack

    Returns the raw file bytes.
    """
    # Flush any leftover data before starting
    flush_input(dev, timeout_ms=300)

    # Step 1: stat
    stat_pkt = build_stat_pkt(name)
    if debug:
        print(f"  [stat] sending {len(stat_pkt)}B: {stat_pkt.hex()}")
    stat_resp = send(dev, stat_pkt)
    if debug:
        print(f"  [stat] response {len(stat_resp)}B: {stat_resp.hex()}")
    if not stat_resp or stat_resp[:4] != MAGIC:
        print(f"  [!] Unexpected stat response: {stat_resp[:32].hex()}")
        return b''

    # Parse size from stat response (at offset 2+4+len(name) after payload start)
    try:
        marker = b'serialization::archive'
        idx    = stat_resp.find(marker)
        after  = stat_resp[idx + len(marker) + 9:]  # skip 9 Boost version bytes
        # payload: \x00\x01 + name_len(4) + name + size(4) + type(4) + ...
        name_len = struct.unpack_from('<I', after, 2)[0]
        file_size = struct.unpack_from('<I', after, 2 + 4 + name_len)[0]
        file_type = struct.unpack_from('<I', after, 2 + 4 + name_len + 4)[0]
        print(f"  [stat] '{name}': size={file_size}B  type=0x{file_type:04x}")
    except Exception:
        file_size = 0

    # Step 2: download
    dl_pkt = build_download_pkt(f'C:/temp/{name}')
    if debug:
        print(f"  [dl]   sending {len(dl_pkt)}B: {dl_pkt.hex()}")
    dev.write(EP_OUT, dl_pkt, timeout=TIMEOUT)

    # Read: first come raw file bytes (possibly Boost-framed), then an MD5 ack
    # The MD5 ack is a Boost packet whose payload is exactly a 32-hex-char MD5 + newline.
    # Some file types (type=0x21) are ALSO Boost-serialized, so we can't treat every
    # MAGIC-starting chunk as the ack — we must check the actual payload.
    import re as _re
    MD5_RE = _re.compile(rb'^[0-9a-f]{32}\n?$')

    file_chunks = []
    while True:
        try:
            chunk = bytes(dev.read(EP_IN, 512, timeout=TIMEOUT))
            if debug:
                print(f"  [dl]   chunk {len(chunk)}B: {chunk[:32].hex()}")
            if chunk[:4] == MAGIC:
                payload = parse_response(chunk)
                if payload and MD5_RE.match(payload.strip().encode()):
                    # Confirmed MD5 ack — transfer complete
                    if debug:
                        print(f"  [dl]   MD5 ack: {payload.strip()!r}")
                    break
                else:
                    # Boost-framed file content (binary file type)
                    if debug:
                        print(f"  [dl]   Boost-framed data chunk (not ack)")
                    file_chunks.append(chunk)
            else:
                file_chunks.append(chunk)
        except usb.core.USBTimeoutError:
            if debug:
                print(f"  [dl]   timeout — no further data")
            break

    return b''.join(file_chunks)

# ── Exact captured packets (fallback if CRC scheme differs) ──────────────────

RAW_CMDS = {
    "version":    bytes.fromhex("020000000400 04 cddb7501".replace(" ", "")),
    "vehicle":    bytes.fromhex("020000000400 03 7ffba911".replace(" ", "")),
    "device":     bytes.fromhex("020000000400 28 c9eab704".replace(" ", "")),
    "show_cur":   bytes.fromhex("020000003c001a1600000073657269616c697a6174696f6e3a3a617263686976650304040408010000001100000073686f775f637572206e6f5f636f6d6d73761756f0"),
    "list_root":  bytes.fromhex("020000002b00261600000073657269616c697a6174696f6e3a3a61726368697665030404040801000000000000000039dfa458"),
    "list_datalog": bytes.fromhex("020000003200261600000073657269616c697a6174696f6e3a3a6172636869766503040404080100000007000000646174616c6f67c0000f05"),
    "list_maps":  bytes.fromhex("020000002f00261600000073657269616c697a6174696f6e3a3a61726368697665030404040801000000040000006d6170732443b0b4"),
    "list_screenshots": bytes.fromhex("020000003600261600000073657269616c697a6174696f6e3a3a617263686976650304040408010000000b00000073637265656e73686f74738df8881e"),
}

def raw_cmd(dev, name: str, debug: bool = False) -> bytes:
    pkt  = RAW_CMDS[name]
    if debug:
        print(f"  >>> SEND ({len(pkt)}B): {pkt.hex()}")
    resp = send(dev, pkt)
    if debug:
        print(f"  <<< RECV ({len(resp)}B): {resp[:128].hex()}")
    return resp

# ── Main ──────────────────────────────────────────────────────────────────────

def section(title):
    print(f"\n{'=' * 60}\n{title}\n{'=' * 60}")

def hexdump(data: bytes, indent: str = "    ") -> None:
    for i in range(0, len(data), 16):
        chunk = data[i:i+16]
        hex_part = ' '.join(f'{b:02x}' for b in chunk)
        asc_part = ''.join(chr(b) if 32 <= b < 127 else '.' for b in chunk)
        print(f"{indent}{i:04x}  {hex_part:<48}  {asc_part}")

def analyze_settings(data: bytes) -> None:
    """Try to find marriage-related fields in the settings binary blob."""
    print(f"\n  Raw ({len(data)} bytes):")
    hexdump(data)
    print()

    # Scan for readable strings
    import re
    strings = re.findall(rb'[ -~]{3,}', data)
    if strings:
        print("  Readable strings found:")
        for s in strings:
            print(f"    {s.decode('ascii', errors='replace')!r}")

    # Try to find known vehicle identifiers
    vehicle_markers = [b'SUBA', b'SUB', b'WRX', b'2010', b'USDM',
                       b'SUBA_US', b'SUBA_US_WRXM']
    for marker in vehicle_markers:
        idx = data.find(marker)
        if idx != -1:
            print(f"  [!] Found {marker!r} at offset {idx}")

    # Look for the device serial (SUB0000001 from device info)
    serial = b'SUB034'
    idx = data.find(serial)
    if idx != -1:
        print(f"  [!] Found serial prefix {serial!r} at offset {idx}")

CANDIDATE_FILES = [
    "settings",
    "backupcksum",
    "marriage",
    "vehicle",
    "binding",
    "married",
    "ecudata",
    "carinfo",
    "profile",
    "deviceinfo",
    "config",
]

def list_root_files(dev) -> list[tuple[str,int,int]]:
    """Parse root directory listing. Returns list of (name, size, type)."""
    resp = raw_cmd(dev, "list_root")
    if not resp:
        return []
    marker = b'serialization::archive'
    idx = resp.find(marker)
    if idx == -1:
        return []
    after = resp[idx + len(marker) + 9:]

    files = []
    # The list response is a Boost-serialized array of file_info structs.
    # Each entry has: \x00\x01 + name_len(4) + name + size(4) + type(4) + 19 bytes padding
    # Scan for all occurrences of the \x00\x01 prefix pattern
    i = 0
    while i < len(after) - 6:
        if after[i] == 0x00 and after[i+1] == 0x01:
            try:
                name_len = struct.unpack_from('<I', after, i+2)[0]
                if 1 <= name_len <= 64 and i + 2 + 4 + name_len + 8 <= len(after):
                    name = after[i+2+4:i+2+4+name_len].decode('ascii', errors='replace')
                    if all(32 <= ord(c) < 127 for c in name):
                        file_size = struct.unpack_from('<I', after, i+2+4+name_len)[0]
                        file_type = struct.unpack_from('<I', after, i+2+4+name_len+4)[0]
                        if file_size > 0:
                            files.append((name, file_size, file_type))
            except Exception:
                pass
        i += 1
    return files

def main():
    print("Connecting to COBB Accessport...")
    dev = open_device()
    flush_input(dev)
    time.sleep(0.3)
    print("Connected.\n")

    section("DEVICE INFO")
    resp = raw_cmd(dev, "version")
    print(f"  Firmware : {parse_response(resp)}")

    resp = raw_cmd(dev, "device")
    info = parse_response(resp)
    if info:
        lines  = [l for l in info.split('\n') if l.strip()]
        labels = ["Firmware", "Device ID", "Serial", "Status", "Vehicle", "ECU ID"]
        for label, value in zip(labels, lines):
            print(f"  {label:<12}: {value}")

    section("ROOT DIRECTORY LISTING (raw)")
    flush_input(dev, 500)
    resp = raw_cmd(dev, "list_root")
    print(f"  Response {len(resp)}B:")
    hexdump(resp)
    files = list_root_files(dev)
    if files:
        print(f"\n  Parsed {len(files)} entries:")
        for name, size, ftype in files:
            print(f"    {name:<20}  size={size:>6}  type=0x{ftype:04x}")

    section("DOWNLOAD FILES")
    log_dir = Path(".") / "log"
    log_dir.mkdir(exist_ok=True)

    # Known files from directory listing (skip directories)
    to_download = ["settings", "backupcksum"]

    for name in to_download:
        print(f"\n  >> {name}")
        try:
            data = download_file(dev, name, debug=True)
            if data:
                out = log_dir / f"{name}.bin"
                out.write_bytes(data)
                print(f"     {len(data)} bytes  saved to {out.name}")
                analyze_settings(data)
            else:
                print("     (empty / no response)")
        except Exception as e:
            print(f"     [!] Error: {e}")

    usb.util.dispose_resources(dev)
    print("\nDone.")

if __name__ == "__main__":
    main()
