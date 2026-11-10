#!/usr/bin/env python3
import os as _os, sys as _sys  # repo-layout bootstrap (added by reorg)
_R = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
_sys.path[:0] = [_os.path.join(_R, _d) for _d in ("core", "tools", "emulator", "recon")]; _os.chdir(_R)
"""
COBB AccessPort Marriage Toolkit  (interactive)
================================================
Plug in an AccessPort, run this, follow the menu. It auto-detects the unit, reads its
own vehicle code and current map file, backs up first, and can unmarry / re-marry the
device's own marriage state entirely over USB -- no car, no soldering.

This talks to the device over its USB bulk protocol (WinUSB via Zadig on Windows).
It relies only on the device's own firmware behaviour; nothing here reflashes a car ECU
or contacts COBB's servers.  Validated on AP3-SUB-003 / v1.7.4.2-24844; on any other
AP3 SKU the tool probes the unit's paths, vendor binaries and eval path before it
offers a write, instead of trusting a part-number allowlist.

Files in this folder used as proven primitives:  client.py, upload.py, protocol.py
"""
import sys, os, re, time, json, hashlib, tempfile, struct
import usb.core, usb.util
import client, upload
import backup as backup_mod   # cmd 0x26 list_dir, so backups discover files rather than guess

# ---- units we have actually held --------------------------------------------
# Informational only. The tool no longer refuses a unit that is not listed here;
# whether a unit can be written is decided by probing it (probe_unit), because
# compatibility is established on the connected unit, not inferred from its SKU.
VALIDATED      = {("AP3-SUB-003", "v1.7.4.2-24844")}
UNITS_DIR      = os.path.join(_R, "units")

# device paths (relative to the USB sandbox root = /user/ap-user). The absolute
# forms are confirmed on the connected unit by probe_unit() before any write.
P_MAPNAMES  = "../ap-data/mapnames"
P_BACKUP    = "../ap-data/backup"
DEFAULT_DATA_DIR = "/user/ap-data"
DEFAULT_USER_DIR = "/user/ap-user"
STAGE_NAME  = "mnclean"                       # staged clean-mapnames copy in /user/ap-user
# FALLBACK name lists, used only when the device will not give us a directory
# listing. These were observed on an AP3-SUB-003; a unit from another manufacturer
# can keep different files, and backing up the wrong set means a later re-marry has
# nothing to restore from -- i.e. an "unmarry" that was supposed to be reversible
# quietly is not. do_backup() lists the directory first and only falls back to these.
AP_DATA_FILES = ["backup", "mapnames", "reflash_props", "realtime", "reflash",
                 "prev_install", "user.rom", "user.md5sum"]
AP_USER_FILES = ["settings", "backupcksum"]

# Never copy these out: huge, regenerable, or pure noise. Everything else in the
# directory is saved, because we cannot know what another manufacturer considers
# load-bearing.
BACKUP_SKIP = {"dump", "logs", "maps", "datalog", "presets", "images", "anim",
               "user.rom", "user.md5sum"}  # stat can wedge USB on newer firmware

try: sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception: pass


# ============================ low-level helpers ==============================
def unwrap(fr: bytes) -> bytes:
    """Unwrap every complete frame; reject corruption instead of truncating it."""
    if not fr.startswith(b"\x02"):
        return fr
    parts, pos = [], 0
    while pos < len(fr):
        if len(fr) - pos < 11 or fr[pos] != 2:
            raise DeviceTaskError("malformed or truncated file frame")
        total = int.from_bytes(fr[pos + 1:pos + 5], "big")
        end = pos + 7 + total
        if total < 4 or end > len(fr):
            raise DeviceTaskError("truncated file frame")
        # The captured trailer differs from the builders' zlib CRC. Validate
        # framing here; read_devfile verifies content using the device MD5.
        parts.append(fr[pos + 7:end - 4])
        pos = end
    return b"".join(parts)


def drain(dev, ms=1200):
    try:
        while True:
            x = bytes(dev.read(client.EP_IN, 512, timeout=ms))
            if len(x) < 512:
                break
    except usb.core.USBError:
        pass


def connect(quiet=False, expected_serial=None):
    """Select one unit; reconnects must match the original device-info serial."""
    try:
        devices = list(usb.core.find(find_all=True, idVendor=client.VID,
                                     backend=client._backend) or [])
        if expected_serial is None and len(devices) != 1:
            if devices and not quiet:
                print("[!] Connect only one AccessPort for initial selection.")
            return None
        matches = []
        for dev in devices:
            try:
                try:
                    dev.set_configuration(1)
                except usb.core.USBError:
                    if dev.get_active_configuration().bConfigurationValue != 1:
                        raise
                interface = dev.get_active_configuration()[(0, 0)]
                endpoints = {ep.bEndpointAddress: ep for ep in interface}
                if any(address not in endpoints or
                       usb.util.endpoint_type(endpoints[address].bmAttributes) != usb.util.ENDPOINT_TYPE_BULK
                       for address in (client.EP_IN, client.EP_OUT)):
                    raise DeviceTaskError("unsupported USB bulk endpoint layout")
                usb.util.claim_interface(dev, 0)
                if expected_serial is None or get_status(dev).get("serial") == expected_serial:
                    matches.append(dev)
                else:
                    usb.util.dispose_resources(dev)
            except (usb.core.USBError, DeviceTaskError, KeyError, ValueError) as e:
                if not quiet:
                    print(f"[!] Cannot use USB device: {e}")
                usb.util.dispose_resources(dev)
        if len(matches) == 1:
            return matches[0]
        for dev in matches:
            usb.util.dispose_resources(dev)
    except Exception as e:
        if not quiet:
            print(f"[!] USB error: {e}")
    return None


def read_devfile(dev, path):
    """Download exactly the stat size and require the device's MD5 acknowledgement.

    Assemble the stream before interpreting frames: USB reads can split a frame
    or contain multiple frames, including the final acknowledgement.
    """
    client.flush_input(dev, 300)
    stat = client.send(dev, client.build_stat_pkt(path))
    marker = b'serialization::archive'
    idx = stat.find(marker)
    offset = idx + len(marker) + 9
    try:
        if idx < 0:
            raise ValueError("missing stat archive")
        name_len = struct.unpack_from('<I', stat, offset + 2)[0]
        size = struct.unpack_from('<I', stat, offset + 6 + name_len)[0]
    except (ValueError, struct.error) as e:
        raise DeviceTaskError(f"invalid stat response for {path}") from e
    download_packet = client.build_download_pkt('C:/temp/' + path.rsplit('/', 1)[-1])
    dev.write(client.EP_OUT, download_packet, timeout=client.TIMEOUT)
    pending, content = b"", bytearray()
    raw = None
    while True:
        try:
            chunk = bytes(dev.read(client.EP_IN, 512, timeout=client.TIMEOUT))
        except usb.core.USBTimeoutError as e:
            raise DeviceTaskError(f"incomplete download or missing MD5 acknowledgement: {path}") from e
        if not chunk:
            raise DeviceTaskError(f"empty USB read during download: {path}")
        pending += chunk
        if raw is None:
            raw = pending[0] != 2
        if raw and len(content) < size:
            take = min(size - len(content), len(pending))
            content.extend(pending[:take])
            pending = pending[take:]
        while len(pending) >= 7:
            if pending[0] != 2:
                raise DeviceTaskError(f"invalid download framing: {path}")
            total = int.from_bytes(pending[1:5], 'big')
            if total < 4:
                raise DeviceTaskError(f"invalid download frame length: {path}")
            end = 7 + total
            if len(pending) < end:
                break
            payload = unwrap(pending[:end])
            pending = pending[end:]
            if len(content) == size:
                digest = (client.parse_response(payload) or "").strip()
                if digest != hashlib.md5(content).hexdigest() or pending:
                    raise DeviceTaskError(f"download checksum mismatch: {path}")
                return bytes(content)
            content.extend(payload)
            if len(content) > size:
                raise DeviceTaskError(f"download exceeds declared size: {path}")


def upload_file(dev, dest, data):
    client.flush_input(dev, 300)
    dev.write(client.EP_OUT, upload.build_put_setup(dest, len(data)), timeout=client.TIMEOUT); drain(dev)
    dev.write(client.EP_OUT, upload.build_put_data(data), timeout=client.TIMEOUT); drain(dev)


_FW_RE     = re.compile(r"^v\d+(?:\.\d+)")
_PART_RE   = re.compile(r"^AP[0-9]*(?:-[A-Z]{2,4}){1,2}-\d{3}$")
_SERIAL_RE = re.compile(r"^[A-Z]{2,4}\d{5,}$")
_STATE_RE  = re.compile(r"^(not installed|installed|recovery|unknown\b.*)$", re.I)
_ECU_RE    = re.compile(r"^ECU(?:-|\b)", re.I)
_CODE_RE   = re.compile(r"^[A-Za-z0-9_.-]+$")
DEV_INFO_KEYS = ["firmware", "part", "serial", "state", "vehicle", "ecu"]
# DeviceState.h::e_DeviceState as shipped in sh_funcs.sh on AP3 firmware.
KNOWN_AP_STATES = {"-1", "0", "1", "2", "3", "4", "5"}
PLACEHOLDER_VALUES = {"", "?", "(null)", "None"}
MARKER_TIMEOUT = 30          # seconds to wait for a fresh /tmp output marker


class MapnamesRestoreError(RuntimeError):
    """The clean mapnames could not be verified after a device task."""


class DeviceTaskError(RuntimeError):
    """The device task did not produce fresh output (stale or missing /tmp file)."""


def _classify_lines(lines):
    """Assign device-info lines by shape, not position.

    The reply from cmd 0x28 is six lines today, but that order is a firmware
    implementation detail. Matching each line on its own shape keeps the fields
    aligned on a SKU whose reply adds or drops one (e.g. no ECU line on a unit
    with no car attached), instead of silently shifting state into vehicle.
    """
    d = {k: "" for k in DEV_INFO_KEYS}
    used = set()

    def take(rx):
        for i, line in enumerate(lines):
            if i not in used and rx.match(line):
                used.add(i)
                return line
        return ""

    d["firmware"] = take(_FW_RE)
    d["part"] = take(_PART_RE)
    d["serial"] = take(_SERIAL_RE)
    d["state"] = take(_STATE_RE)
    d["ecu"] = take(_ECU_RE)
    for i, line in enumerate(lines):
        if i not in used:
            d["vehicle"] = line
            used.add(i)
            break
    rest = [l for i, l in enumerate(lines) if i not in used]
    for k in DEV_INFO_KEYS:
        if k != "serial" and not d[k] and rest:
            d[k] = rest.pop(0)
    return d


def parse_device_info(text):
    lines = [l.strip() for l in (text or "").splitlines() if l.strip()]
    d = _classify_lines(lines)
    d["raw"] = text or ""
    return d


def parse_eeprom(text):
    """Parse `readeeprom -i -s` shell assignments into a dict.

    Only KEY=VALUE lines with an uppercase key are kept, so shell noise or a
    tool banner on another firmware cannot masquerade as a state field.
    """
    d = {}
    for line in (text or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        k = k.strip()
        if re.match(r"^[A-Z][A-Z0-9_]*$", k) and not k.startswith("P_"):
            d[k] = v.strip()
    return d


def has_payload(mapnames):
    """True if a mapnames value contains shell command substitution.

    The tool's own payload is `reflash=$(...)`; a previous run that failed
    between the write and the cleanup leaves it behind. Running the probe would
    evaluate it again, so the startup guard checks this before doing anything.
    Legitimate values are map paths, never `$(`, a backtick, or a double quote
    (the device evals the value inside double quotes, so a quote can break out).
    """
    text = (mapnames.decode("latin1") if isinstance(mapnames, (bytes, bytearray))
            else str(mapnames or ""))
    for line in text.splitlines():
        s = line.strip()
        if (s and not s.startswith("#") and "=" in s
                and ("$(" in s or "`" in s or '"' in s)):
            return True
    return False


def get_status(dev):
    """Parse cmd 0x28 device-info. NOTE: reflects state read at last BOOT."""
    client.flush_input(dev, 300)
    info = client.parse_response(client.send(dev, client.RAW_CMDS["device"])) or ""
    return parse_device_info(info)


# ============================ device code-exec ===============================
def run_task(dev, taskstr, total=230):
    """Run a device task and wait for RUNTASK_FINISH.

    Do NOT stop on a quiet gap: diagnostic_file goes silent for many seconds
    while it tars/gzips, and returning early can restore mapnames before the
    payload has even been evaluated. (Mirrors core/exec_capture.py.)
    """
    client.flush_input(dev, 300)
    dev.write(client.EP_OUT, upload.build_boost(0x1a, upload.bstr(taskstr)), timeout=client.TIMEOUT)
    t0 = time.time(); buf = b""
    while time.time() - t0 < total:
        try:
            c = bytes(dev.read(client.EP_IN, 512, timeout=4000))
            if c: buf += c
            if b"RUNTASK_FINISH" in buf: break
        except usb.core.USBTimeoutError:
            continue
    return (b"RUNTASK_FINISH" in buf), buf


def _run_marker():
    """Per-run token that proves a /tmp output was written by THIS task."""
    return f"#RUN:{os.getpid()}-{time.time_ns()}"


def _wait_for_marker(dev, outfile, marker, timeout=None):
    """Read /tmp/<outfile> until this run's marker is present, or fail."""
    timeout = MARKER_TIMEOUT if timeout is None else timeout
    needle = marker.encode()
    deadline = time.time() + timeout
    while True:
        try:
            out = read_devfile(dev, "../../tmp/" + outfile)
        except (usb.core.USBError, DeviceTaskError):
            out = b""
        if needle in out:
            return out
        if time.time() >= deadline:
            raise DeviceTaskError(
                f"no fresh /tmp/{outfile}: the device task did not run "
                "or did not finish (stale output was discarded)")
        time.sleep(2)


def _restore_mapnames_now(dev, clean_mapnames):
    """Best-effort clean-mapnames restore with read-back proof. Never raises."""
    try:
        upload_file(dev, P_MAPNAMES, clean_mapnames)
        back = read_devfile(dev, P_MAPNAMES)
    except (usb.core.USBError, DeviceTaskError) as e:
        print(f"[warn] mapnames restore failed ({e!r}) -- mapnames may still be "
              "poisoned; re-connect and re-run before anything else.")
        return False
    if not back or back == b"33" or back != clean_mapnames:
        print("[warn] mapnames restore could not be verified (empty or different "
              "read-back) -- mapnames may still be poisoned; re-connect and re-run "
              "before anything else.")
        return False
    print("[restore] clean mapnames verified.")
    return True


def device_exec(dev, shell_cmd, outfile, clean_mapnames):
    """Run shell_cmd on device (it writes results to /tmp/<outfile>); restores mapnames; no reboot."""
    marker = _run_marker()
    # The marker is appended after the command's own redirect, so it proves the
    # command ran in THIS invocation. Without it, a failed task can hand back a
    # stale /tmp/ee, /tmp/probe or /tmp/cleanup from a previous run.
    cmd = f"rm -f /tmp/{outfile}; {shell_cmd}; echo '{marker}' >> /tmp/{outfile}"
    keep = "\n".join(l for l in clean_mapnames.decode("latin1").splitlines() if not l.startswith("reflash="))
    poison = ("reflash=$(" + cmd + ")\n" + keep + "\n").encode()
    # The restore runs under finally: a USB error in run_task (or the poison
    # upload) must NOT leave mapnames poisoned -- the map pointer stays corrupt
    # and the injected command re-runs on every diagnostic_file create, including
    # ones the UI triggers by itself. Restoration is part of the operation, not
    # optional cleanup. (Mirrors tools/carscan.py; this exact failure wedged a
    # unit mid-session, 2026-10.)
    out = b""
    try:
        upload_file(dev, P_MAPNAMES, poison)
        run_task(dev, "diagnostic_file create")
        out = _wait_for_marker(dev, outfile, marker)
    finally:
        if not _restore_mapnames_now(dev, clean_mapnames):
            # Replace the original error too: leaving the menu alive with a
            # poisoned mapnames is worse than losing the first traceback.
            raise MapnamesRestoreError(
                "mapnames could not be verified clean after the device task; "
                "do not run another operation. Reconnect and use "
                "--restore-mapnames with a known-clean file.") from None
    return out


def exec_reboot(dev, wee_cmd, clean_mapnames,
                data_dir=DEFAULT_DATA_DIR, user_dir=DEFAULT_USER_DIR):
    """writeeeprom + restore clean mapnames + reboot, in one payload.

    Returns False if nothing was started, None if the trigger write errored
    (the device may or may not have received it), and True if the trigger was
    accepted. The caller must treat None as "wait and verify", never as success.
    """
    stage_up = (STAGE_NAME if user_dir == DEFAULT_USER_DIR
                else user_dir.rstrip("/") + "/" + STAGE_NAME)
    stage_dev = user_dir.rstrip("/") + "/" + STAGE_NAME
    try:
        upload_file(dev, stage_up, clean_mapnames)
        stage_ok = read_devfile(dev, stage_up) == clean_mapnames
    except usb.core.USBError as e:
        print(f"[!] Could not stage the clean mapnames ({e!r}); nothing was written.")
        return False
    if not stage_ok:
        return False
    # Prove the cleanup half of the payload BEFORE the EEPROM write. If the
    # remount/copy path is wrong on this unit, the real payload would write the
    # EEPROM, fail to restore mapnames, and reboot with a live payload. This
    # dry run changes nothing an unmarry would not change anyway: it copies the
    # verified clean mapnames over mapnames itself.
    dry = ("{ mount /user -o remount,rw && cp " + stage_dev + " " +
           data_dir.rstrip("/") + "/mapnames && cmp " + stage_dev + " " + data_dir.rstrip("/") +
           "/mapnames && sync && echo CLEANUP_OK; } "
           "> /tmp/cleanup 2>&1")
    try:
        dry_out = device_exec(dev, dry, "cleanup", clean_mapnames)
    except usb.core.USBError:
        dry_out = b""
    if b"CLEANUP_OK" not in dry_out.splitlines():
        return False
    try:
        if read_devfile(dev, P_MAPNAMES) != clean_mapnames:
            return False
    except usb.core.USBError:
        return False
    # Always disarm after an EEPROM failure, but reboot only after a successful
    # write AND a verified cleanup. A failed cleanup must never reboot a payload.
    payload = (wee_cmd + "; write_rc=$?; mount /user -o remount,rw && cp " + stage_dev +
               " " + data_dir.rstrip("/") + "/mapnames && cmp " + stage_dev + " " +
               data_dir.rstrip("/") + "/mapnames && sync && test $write_rc -eq 0 && reboot -f")
    keep = "\n".join(l for l in clean_mapnames.decode("latin1").splitlines() if not l.startswith("reflash="))
    poison = ("reflash=$(" + payload + ")\n" + keep + "\n").encode()
    try:
        upload_file(dev, P_MAPNAMES, poison)
        client.flush_input(dev, 300)
        try:
            dev.write(client.EP_OUT, upload.build_boost(0x1a, upload.bstr("diagnostic_file create")),
                      timeout=client.TIMEOUT)
        except usb.core.USBError as e:
            # The trigger did not get an ACK. It may still have reached the
            # device, so do not tell the caller nothing happened; wait for a
            # reboot and verify. The restore is best-effort (it may race a
            # reboot); the caller's reconnect path is what proves the outcome.
            print(f"[!] Trigger write errored ({e!r}); the unit may or may not be "
                  "rebooting. Restoring clean mapnames and waiting for the outcome.")
            if not _restore_mapnames_now(dev, clean_mapnames):
                raise MapnamesRestoreError("trigger failed and clean mapnames could not be restored")
            return None
    except usb.core.USBError as e:
        print(f"[!] Could not upload the reboot payload ({e!r}); restoring clean mapnames.")
        if not _restore_mapnames_now(dev, clean_mapnames):
            raise MapnamesRestoreError("payload upload failed and clean mapnames could not be restored")
        return False
    return True


def wait_reconnect(total=230, grace=6, expected_serial=None):
    print("    waiting for reboot + re-enumeration (this takes ~1-2 min) ...")
    time.sleep(grace)   # let the trigger start; do not reuse the pre-reboot handle
    t0 = time.time()
    seen_gap = False
    while time.time() - t0 < total:
        dev = connect(quiet=True, expected_serial=expected_serial)
        if dev is None:
            seen_gap = True
            time.sleep(3)
            continue
        if not seen_gap:
            # A unit that never disappeared is probably the pre-reboot device.
            # Require a real disconnect once, so a missed trigger cannot look
            # like a successful reboot.
            try: usb.util.dispose_resources(dev)
            except Exception: pass
            time.sleep(3)
            continue
        try:
            client.flush_input(dev, 200)
            v = client.parse_response(client.send(dev, client.RAW_CMDS["version"]))
            if v and v.startswith("v"):
                return dev, v
            usb.util.dispose_resources(dev)
        except usb.core.USBError:
            try: usb.util.dispose_resources(dev)
            except Exception: pass
        time.sleep(3)
    return None, None


# ============================ high-level ops =================================
def live_eeprom(dev, clean_mapnames):
    """Ground-truth EEPROM state via readeeprom (slow, ~1 min). Returns dict."""
    out = device_exec(dev, "cd /ap-app/bin && ./readeeprom -i -s > /tmp/ee 2>&1", "ee", clean_mapnames)
    return parse_eeprom(out.decode("latin1", "replace"))


PROBE_CMD = (
    "{ "
    "echo P_DATA=$(test -d /user/ap-data && echo 1 || echo 0); "
    "echo P_USER=$(test -d /user/ap-user && echo 1 || echo 0); "
    "echo P_READ=$(test -x /ap-app/bin/readeeprom && echo 1 || echo 0); "
    "echo P_WRITE=$(test -x /ap-app/bin/writeeeprom && echo 1 || echo 0); "
    "echo P_STATE=$(grep -q -e --state /ap-app/bin/writeeeprom && echo 1 || echo 0); "
    "echo P_VEH=$(grep -q -e --vehicle /ap-app/bin/writeeeprom && echo 1 || echo 0); "
    "echo P_DIAG=$(test -x /ap-app/bin/diagnostic_file && echo 1 || echo 0); "
    "echo P_EVAL=$(grep -q mapnames /ap-app/bin/diagnostic_file && echo 1 || echo 0); "
    "echo P_TCM=$(grep -q cm1_state /ap-app/bin/writeeeprom && echo 1 || echo 0); "
    "cd /ap-app/bin && ./readeeprom -i -s 2>&1; "
    "} > /tmp/probe 2>&1"
)


def parse_probe(text):
    """Turn probe output into capabilities plus the unit's live EEPROM fields.

    The checks are the assumptions the unmarry chain actually makes: both user
    directories exist, both vendor EEPROM binaries are present, and the shipped
    diagnostic task is executable. The probe's own output is the proof that the
    eval path ran; mapnames-in-diagnostic is reported but not required. Anything
    missing leaves the unit readable/backup-able but not writable.
    """
    vals = {}
    for line in (text or "").splitlines():
        line = line.strip()
        if line.startswith("P_") and "=" in line:
            k, _, v = line.partition("=")
            vals[k] = v.strip() == "1"
    required = [("P_DATA", "/user/ap-data"), ("P_USER", "/user/ap-user"),
                ("P_READ", "readeeprom"), ("P_WRITE", "writeeeprom"),
                ("P_STATE", "writeeeprom --state"), ("P_VEH", "writeeeprom --vehicle"),
                ("P_DIAG", "diagnostic_file")]
    missing = [label for key, label in required if not vals.get(key)]
    return {
        "data_dir": DEFAULT_DATA_DIR if vals.get("P_DATA") else None,
        "user_dir": DEFAULT_USER_DIR if vals.get("P_USER") else None,
        "read_eeprom": bool(vals.get("P_READ")),
        "write_eeprom": bool(vals.get("P_WRITE")),
        # P_EVAL is reported, not required: these P_ lines only exist because the
        # payload itself ran on the device, which is stronger proof than a grep.
        "eval": bool(vals.get("P_EVAL")),
        "has_tcm": bool(vals.get("P_TCM")),
        "missing": missing,
        "ok": not missing,
        "eeprom": parse_eeprom(text),
    }


def probe_unit(dev, clean_mapnames):
    """Confirm this unit's paths/binaries/eval path before offering any write."""
    try:
        out = device_exec(dev, PROBE_CMD, "probe", clean_mapnames)
    except (usb.core.USBError, DeviceTaskError) as e:
        if isinstance(e, DeviceTaskError):
            print(f"[probe] no fresh output ({e}); leaving this unit read-only.")
        out = b""
    return parse_probe(out.decode("latin1", "replace"))


def _unit_name(status):
    """Filesystem-safe unit directory name (serial comes from the device)."""
    serial = (status or {}).get("serial")
    if not isinstance(serial, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", serial) or serial.lower() == "unknown":
        raise DeviceTaskError("missing or unsafe device serial; refusing shared backup storage")
    return serial


def unit_dir(status):
    d = os.path.join(UNITS_DIR, _unit_name(status))
    os.makedirs(d, exist_ok=True)
    return d


def _manifest_path(status):
    return os.path.join(UNITS_DIR, _unit_name(status), "manifest.json")


def _safe_name(name):
    """Reject device filenames that are not single path components."""
    name = str(name or "")
    return name if name not in ("", ".", "..") and not any(c in name for c in "/\\\x00") else ""


def _names_in(dev, devdir, fallback, sizes=None):
    """Names to back up from one directory: ask the device, else use `fallback`.

    LISTING BEATS GUESSING. The old code walked a hardcoded list captured from an
    AP3-SUB-003. On a unit from another manufacturer any file it does not name is
    silently skipped, and a skipped file is only discovered later, when a re-marry
    has nothing to restore from -- so an operation sold as reversible turns out not
    to be, on somebody else's device. Listing is also how we find files COBB adds in
    a firmware we have never seen.
    """
    try:
        entries = backup_mod.list_dir(dev, devdir, strict=True)
    except Exception as e:
        print(f"[backup] cannot list {devdir} ({e.__class__.__name__}); "
              "falling back to known filenames")
        return [name for name in fallback if name not in BACKUP_SKIP], False
    names = [e["name"] for e in entries
             if e.get("size", 0) > 0 and e["name"] not in BACKUP_SKIP]
    if not names:
        print(f"[backup] {devdir} listed empty; falling back to known filenames")
        return [name for name in fallback if name not in BACKUP_SKIP], False
    if sizes is not None:
        sizes.update({e["name"]: e["size"] for e in entries if e["name"] in names})
    extra = sorted(set(names) - set(fallback))
    if extra:
        print(f"[backup] {devdir}: listing found {len(extra)} file(s) not in the "
              f"known set: {', '.join(extra)}")
    return names, True


def _merge_eeprom(old, new):
    """Keep the re-marry snapshot when a later backup reads an uninstalled unit.

    Backing up after an unmarry is a normal recovery step, but `readeeprom` now
    reports AP_STATE=0 / AP_VEHICLE=?. Replacing the installed-state snapshot
    with that would erase the only record of the vehicle code. A fresh installed
    read (AP_STATE=1) is allowed to update the snapshot; an uninstalled/recovery
    read only fills fields the snapshot does not already have.
    """
    merged = dict(old or {}) if isinstance(old, dict) else {}
    new = dict(new or {}) if isinstance(new, dict) else {}
    # ECU and TCM have independent installation states. An installed ECU must
    # not overwrite a previously captured married TCM with its cleared state.
    previous = dict(merged)
    for k, v in new.items():
        state_key = "AP_CM1_STATE" if k.startswith("AP_CM1_") else "AP_STATE"
        if str(new.get(state_key, "")) == "1" or not previous.get(state_key):
            if v not in PLACEHOLDER_VALUES or k not in merged:
                merged[k] = v
        elif k not in merged or merged.get(k) in PLACEHOLDER_VALUES:
            merged[k] = v
    return merged


def do_backup(dev, status, clean_mapnames, probe=None):
    d = unit_dir(status)
    print(f"[backup] saving to {d} ...")
    saved, records, failures = [], {}, []
    data_sizes, user_sizes = {}, {}
    data_names, data_listed = _names_in(dev, "../ap-data", AP_DATA_FILES, data_sizes)
    user_names, user_listed = _names_in(dev, ".", AP_USER_FILES, user_sizes)
    # Stage all files before touching an existing backup. Directory names remain
    # distinct, and the manifest records the exact bytes that passed size checks.
    with tempfile.TemporaryDirectory(dir=d, prefix=".backup-") as staging:
        for folder, prefix, names, sizes in (
                ("ap-data", "../ap-data/", data_names, data_sizes),
                ("ap-user", "", user_names, user_sizes)):
            for raw in names:
                name = _safe_name(raw)
                if not name:
                    failures.append(str(raw))
                    continue
                relative = folder + "/" + name
                try:
                    data = read_devfile(dev, prefix + name)
                    if not data or data == b"33" or (name in sizes and len(data) != sizes[name]):
                        raise DeviceTaskError("empty, error, or size-mismatched download")
                    if relative == "ap-data/mapnames" and data != clean_mapnames:
                        raise DeviceTaskError("mapnames differs from the captured clean reference")
                    target = os.path.join(staging, relative)
                    os.makedirs(os.path.dirname(target), exist_ok=True)
                    with open(target, "wb") as f:
                        f.write(data)
                    records[relative] = {"size": len(data), "sha256": hashlib.sha256(data).hexdigest()}
                    saved.append(relative)
                except (usb.core.USBError, DeviceTaskError) as e:
                    failures.append(relative)
                    print(f"[backup] {relative}: {e}")
        if failures or not saved:
            print("[backup] incomplete download; existing backup left unchanged.")
            return _load_manifest(status)
        for relative in saved:
            target = os.path.join(d, relative)
            os.makedirs(os.path.dirname(target), exist_ok=True)
            os.replace(os.path.join(staging, relative), target)
    if not (data_listed and user_listed):
        print("[backup] fallback filenames used; this backup will not authorize EEPROM writes.")
    existing = _load_manifest(status)
    eeprom = _merge_eeprom(existing.get("eeprom") or {},
                           (probe or {}).get("eeprom") or {})
    if not eeprom.get("AP_STATE"):
        print("[backup] WARNING: no live EEPROM snapshot. Re-marry will fall back to "
              "the backup file or ask for the vehicle code.")
    vehicle_file = _vehicle_from_backupfile(d) or existing.get("vehicle_code_from_backup_file") or ""
    manifest = dict(existing)
    manifest.update({
        "serial": status["serial"], "part": status["part"], "firmware": status["firmware"],
        "state_at_backup": status["state"], "vehicle": status["vehicle"],
        "vehicle_code_from_backup_file": vehicle_file,
        "eeprom": eeprom,
        "saved_files": saved,
        "files": records,
        "backup_complete": bool(data_listed and user_listed and "ap-data/mapnames" in records),
        "file_set": ("listed" if (data_listed and user_listed) else "fallback"),
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
    })
    if eeprom and not manifest.get("eeprom_captured"):
        manifest["eeprom_captured"] = time.strftime("%Y-%m-%d %H:%M:%S")
    _write_manifest(status, manifest)
    print(f"[backup] saved {len(saved)} files + manifest.json")
    print(f"[backup] vehicle code on record: {manifest['vehicle_code_from_backup_file'] or '(unknown)'}")
    if eeprom.get("AP_CM1_VEHICLE"):
        print(f"[backup] TCM vehicle code on record: {eeprom['AP_CM1_VEHICLE']}")
    return manifest


def _vehicle_from_backupfile(d):
    p = os.path.join(d, "ap-data", "backup")
    if not os.path.exists(p):
        p = os.path.join(d, "backup")  # legacy lookup for recovery only
    if os.path.exists(p):
        with open(p, "r", errors="replace") as f:
            for line in f.read().splitlines():
                if line.startswith("vehicle="):
                    v = line.split("=", 1)[1].strip()
                    if v and v != "?":
                        return v
    return ""


def _write_manifest(status, manifest):
    """A failed save must not truncate the previous recovery snapshot."""
    directory = unit_dir(status)
    fd, temporary = tempfile.mkstemp(dir=directory, prefix=".manifest-")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(manifest, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temporary, _manifest_path(status))
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _load_manifest(status):
    mp = _manifest_path(status)
    if not os.path.exists(mp):
        return {}
    try:
        with open(mp) as f:
            m = json.load(f)
    except (OSError, ValueError):
        return {}
    return m if isinstance(m, dict) and m.get("serial", status.get("serial")) == status.get("serial") else {}


def _verified_backup(status, clean_mapnames):
    """Recheck ownership and every saved file before authorizing a write."""
    m = _load_manifest(status)
    if (m.get("serial") != status.get("serial") or m.get("part") != status.get("part")
            or m.get("firmware") != status.get("firmware") or m.get("backup_complete") is not True or m.get("file_set") != "listed"):
        return False
    records = m.get("files")
    if not isinstance(records, dict) or "ap-data/mapnames" not in records:
        return False
    for relative, record in records.items():
        if not isinstance(relative, str) or relative.count("/") != 1:
            return False
        folder, name = relative.split("/")
        if folder not in ("ap-data", "ap-user") or not _safe_name(name) or not isinstance(record, dict):
            return False
        try:
            with open(os.path.join(unit_dir(status), relative), "rb") as f:
                data = f.read()
        except OSError:
            return False
        if len(data) != record.get("size") or hashlib.sha256(data).hexdigest() != record.get("sha256"):
            return False
        if relative == "ap-data/mapnames" and data != clean_mapnames:
            return False
    return True


def _snapshot_from_manifest(status, key):
    m = _load_manifest(status)
    eeprom = m.get("eeprom")
    if not isinstance(eeprom, dict):
        return ""
    v = eeprom.get(key) or ""
    return "" if str(v).strip() in PLACEHOLDER_VALUES else str(v).strip()


def known_vehicle(status):
    """Best vehicle code for re-marry: EEPROM snapshot, backup file, then prompt."""
    return (_snapshot_from_manifest(status, "AP_VEHICLE")
            or _load_manifest(status).get("vehicle_code_from_backup_file") or "")


def known_cm1_vehicle(status):
    """TCM vehicle code from the EEPROM snapshot, if the unit had one."""
    return _snapshot_from_manifest(status, "AP_CM1_VEHICLE")


def _save_eeprom_snapshot(status, eeprom):
    """Record what we are about to overwrite, so re-marry can put it back."""
    m = _load_manifest(status)
    m["eeprom"] = _merge_eeprom(m.get("eeprom") or {}, eeprom)
    m["eeprom_captured"] = time.strftime("%Y-%m-%d %H:%M:%S")
    _write_manifest(status, m)


def _safe_code(code):
    return (isinstance(code, str) and code not in PLACEHOLDER_VALUES
            and bool(_CODE_RE.fullmatch(code)))


def build_marriage_cmd(to_state, vehicle=None, cm1_state=None, cm1_vehicle=None):
    """Build the vendor-binary command that performs the state change.

    COBB's own installer writes the ECU and the TCM as two separate calls
    (`--state/--vehicle` and `--cm1_state/--cm1_vehicle`), so an unmarry that
    only clears the ECU leaves a married TCM behind on units that have one.
    The calls are joined with `&&`: if the ECU write fails, do not leave a
    half-applied TCM change behind.
    """
    if to_state not in (0, 1):
        raise ValueError(f"unsupported target state: {to_state!r}")
    cmds = []
    if to_state == 0:
        cmds.append("./writeeeprom --state=0 --vehicle=? --quiet")
    else:
        if not _safe_code(vehicle):
            raise ValueError(f"unsafe/empty vehicle code: {vehicle!r}")
        cmds.append(f"./writeeeprom --state=1 --vehicle={vehicle} --quiet")
    if cm1_state is not None:
        if cm1_state not in (0, 1):
            raise ValueError(f"unsupported TCM state: {cm1_state!r}")
        cm1v = "?" if cm1_state == 0 else (cm1_vehicle or "")
        if cm1_state == 1 and not _safe_code(cm1v):
            raise ValueError(f"unsafe/empty TCM vehicle code: {cm1_vehicle!r}")
        cmds.append(f"./writeeeprom --cm1_state={cm1_state} --cm1_vehicle={cm1v} --quiet")
    return "cd /ap-app/bin && " + " && ".join(cmds)


# ============================ menu actions ===================================
def confirm(msg):
    return input(msg + " [type YES]: ").strip() == "YES"


def action_status(dev, clean_mapnames):
    s = get_status(dev)
    print("\n--- AccessPort status (as of last boot) ---")
    print(f"  Part number : {s['part']}")
    print(f"  Serial      : {s['serial']}")
    print(f"  Firmware    : {s['firmware']}")
    print(f"  State       : {s['state']}")
    print(f"  Vehicle     : {s['vehicle']}")
    if input("  Deep-verify live EEPROM now? (slow ~1min) [y/N]: ").strip().lower() == "y":
        try:
            ee = live_eeprom(dev, clean_mapnames)
        except DeviceTaskError as e:
            print(f"  LIVE EEPROM : unavailable ({e})")
            return s
        if ee:
            print("  LIVE EEPROM :")
            for k in sorted(ee):
                print(f"    {k}={ee[k]}")
        else:
            print("  LIVE EEPROM : (no response — readeeprom may be unavailable)")
    return s


def action_toggle(dev, status, clean_mapnames, probe, to_state):
    if not probe.get("ok"):
        print("[!] This unit failed the capability preflight; refusing to write.")
        print("    Missing: " + ", ".join(probe.get("missing") or ["probe did not answer"]))
        return dev
    _unit_name(status)
    if to_state == 0 and not _verified_backup(status, clean_mapnames):
        print("[!] A complete, verified backup for this unit is required. Run menu 2 first.")
        return dev

    # The cached cmd 0x28 state is from last boot. Read the EEPROM now, both to
    # decide whether there is anything to do and to know if a TCM is married.
    print("\n[toggle] reading live EEPROM (~1 min) ...")
    live = live_eeprom(dev, clean_mapnames)
    if not live.get("AP_STATE"):
        print("[!] Could not read live EEPROM; refusing to write blind.")
        return dev
    if live["AP_STATE"] not in KNOWN_AP_STATES:
        print(f"[!] Unrecognised AP_STATE={live['AP_STATE']!r}; this tool only knows "
              f"{', '.join(sorted(KNOWN_AP_STATES))}. Refusing to write.")
        return dev

    tcm = bool(probe.get("has_tcm")) or "AP_CM1_STATE" in live
    if tcm and "AP_CM1_STATE" not in live:
        print("[!] This unit supports TCM pairing, but readeeprom did not report "
              "AP_CM1_STATE. Refusing to guess whether a TCM is married.")
        return dev
    if tcm and live.get("AP_CM1_STATE") not in ("0", "1"):
        print("[!] Unknown TCM state; refusing to write.")
        return dev
    if to_state == 0 and tcm and live.get("AP_CM1_STATE") == "1" and not _safe_code(live.get("AP_CM1_VEHICLE")):
        print("[!] Married TCM has no usable vehicle code; refusing an irreversible clear.")
        return dev
    cm1_state = cm1_vehicle = None
    snapshot = None

    if to_state == 0:
        if live.get("AP_STATE") == "0" and (not tcm or live.get("AP_CM1_STATE") == "0"):
            print(f"[toggle] already uninstalled (AP_STATE=0); nothing to write.")
            return dev
        # Unmarry is only reversible if the vehicle code survives somewhere. If
        # the EEPROM has none, make the user supply it now, and store it in the
        # snapshot that re-marry reads later.
        if not _safe_code(live.get("AP_VEHICLE")):
            code = input("    No usable AP_VEHICLE in the EEPROM. Enter this unit's "
                         "vehicle code to store for re-marry (blank = abort): ").strip()
            if not _safe_code(code):
                print("[!] Without a vehicle code this would not be reversible. Aborting.")
                return dev
            live["AP_VEHICLE"] = code
        if tcm and live.get("AP_CM1_STATE") not in (None, "0"):
            cm1_state, cm1_vehicle = 0, "?"
        snapshot = live
        wee = build_marriage_cmd(0, cm1_state=cm1_state, cm1_vehicle=cm1_vehicle)
        what = "UNMARRY (ECU state -> 0"
        if cm1_state is not None:
            what += ", TCM state -> 0"
        what += ")"
    else:
        if live.get("AP_STATE") == "1":
            print(f"[toggle] already installed (AP_VEHICLE={live.get('AP_VEHICLE')}); "
                  "re-marry will overwrite the vehicle code.")
        veh = known_vehicle(status)
        if not veh:
            veh = input("    Enter the vehicle code to re-marry to (e.g. SUBA_US_WRXM_10): ").strip()
        if not _safe_code(veh):
            print(f"[!] Invalid vehicle code {veh!r}. Aborting.")
            return dev
        cm1_vehicle = known_cm1_vehicle(status)
        snap_cm1_state = _snapshot_from_manifest(status, "AP_CM1_STATE")
        if tcm and snap_cm1_state == "1" and _safe_code(cm1_vehicle):
            cm1_state = 1
        elif tcm and snap_cm1_state == "1":
            print("[!] TCM was married but its vehicle code is missing; refusing a partial re-marry.")
            return dev
        elif tcm and cm1_vehicle:
            print("[toggle] TCM code on record, but the snapshot says the TCM was not "
                  "married; not writing a TCM state.")
        wee = build_marriage_cmd(1, veh, cm1_state, cm1_vehicle)
        what = f"RE-MARRY (ECU state -> 1, vehicle={veh}"
        if cm1_state is not None:
            what += f", TCM vehicle={cm1_vehicle}"
        what += ")"

    print(f"\n[toggle] {what}")
    print(f"    payload: {wee}")
    if not confirm("    This writes the device EEPROM and reboots. Proceed?"):
        print("    cancelled."); return dev
    if snapshot is not None:
        try:
            _save_eeprom_snapshot(status, snapshot)
        except OSError as e:
            print(f"[!] Could not save the EEPROM snapshot ({e}); refusing to write.")
            return dev
    started = exec_reboot(dev, wee, clean_mapnames,
                          data_dir=probe.get("data_dir") or DEFAULT_DATA_DIR,
                          user_dir=probe.get("user_dir") or DEFAULT_USER_DIR)
    if started is False:
        print("[!] The write was not started (staging or cleanup gate failed). "
              "Nothing was written.")
        return dev
    if started is None:
        print("[toggle] trigger outcome is unknown; waiting for the unit to reboot "
              "or stay up, then verifying/disarming ...")
    try: usb.util.dispose_resources(dev)
    except Exception: pass
    dev2, ver = wait_reconnect(expected_serial=status["serial"])
    if not dev2:
        print("[!] Device did not come back in time. Check the screen; it may still be booting.")
        # If the trigger was missed, the unit is still up and may still be armed.
        still = connect(quiet=True, expected_serial=status["serial"])
        if still is not None:
            print("[toggle] unit is still reachable; verifying/disarming mapnames ...")
            restored = _restore_mapnames_now(still, clean_mapnames)
            try: usb.util.dispose_resources(still)
            except Exception: pass
            if not restored:
                raise MapnamesRestoreError(
                    "the unit is reachable but clean mapnames could not be verified; "
                    "do not power-cycle. Reconnect and run --restore-mapnames.")
            return None
        raise MapnamesRestoreError(
            "the unit did not re-enumerate and could not be disarmed after the trigger; "
            "do not power-cycle. Reconnect and check status before any write.")
    s = get_status(dev2)
    print(f"[toggle] back online ({ver}). Device now reports: {s['state']} / {s['vehicle']}")
    _verify_toggle(dev2, clean_mapnames, to_state, veh if to_state else None,
                   cm1_state, cm1_vehicle)
    return dev2


def _verify_toggle(dev, clean_mapnames, to_state, vehicle, cm1_state=None, cm1_vehicle=None):
    """Re-read the EEPROM after the reboot and say whether the write took."""
    print("[toggle] verifying live EEPROM (~1 min) ...")
    ee = live_eeprom(dev, clean_mapnames)
    if not ee.get("AP_STATE"):
        print("[!] Could not verify: readeeprom returned nothing.")
        return
    ok = ee["AP_STATE"] == str(to_state)
    if to_state == 1:
        ok = ok and ee.get("AP_VEHICLE") == vehicle
    if cm1_state is not None:
        ok = ok and ee.get("AP_CM1_STATE") == str(cm1_state)
        if cm1_state == 1:
            ok = ok and ee.get("AP_CM1_VEHICLE") == cm1_vehicle
    if ok:
        extra = f" AP_VEHICLE={ee['AP_VEHICLE']}" if to_state else ""
        if cm1_state is not None:
            extra += f" AP_CM1_STATE={ee.get('AP_CM1_STATE')}"
            if cm1_state == 1:
                extra += f" AP_CM1_VEHICLE={ee.get('AP_CM1_VEHICLE')}"
        print(f"[toggle] verified: AP_STATE={ee['AP_STATE']}" + extra)
    else:
        print("[!] VERIFY FAILED: the EEPROM does not show the change we just wrote.")
        print(f"    AP_STATE={ee.get('AP_STATE')!r} AP_VEHICLE={ee.get('AP_VEHICLE')!r}"
              + (f" AP_CM1_STATE={ee.get('AP_CM1_STATE')!r}"
                 f" AP_CM1_VEHICLE={ee.get('AP_CM1_VEHICLE')!r}"
                 if cm1_state is not None else ""))
        print("    Do not assume the operation worked. Re-run status and back up again.")


USAGE = """\
COBB AccessPort Marriage Toolkit

  python3 tools/cobbtool.py
      Interactive menu: status, backup, unmarry, re-marry.

  python3 tools/cobbtool.py --restore-mapnames FILE
      Upload FILE to the unit's mapnames without triggering the diagnostic
      task. Use this to clear a leftover payload after a failed run, then
      start the tool normally.

  Keep the repository layout: the script adds core/, tools/ and friends to the
  import path itself, so it can be run from any directory. No car is needed;
  the AccessPort must be on USB.
"""


def restore_mapnames(path):
    """Upload a known-clean mapnames without evaluating it. Returns exit code."""
    print("[restore] uploading mapnames only; no diagnostic task will run.")
    try:
        data = open(path, "rb").read()
    except OSError as e:
        print(f"[!] Cannot read {path}: {e}")
        return 2
    if not data:
        print("[!] That file is empty. Refusing.")
        return 2
    if has_payload(data):
        print("[!] That file contains a command payload. Refusing to restore it.")
        return 2
    if b"reflash=" not in data and b"realtime=" not in data:
        print("[!] That does not look like a mapnames file (no reflash= or realtime=).")
        return 2
    dev = connect()
    if dev is None:
        print("[!] No AccessPort found (USB VID 0x1A84).")
        return 1
    try:
        status = get_status(dev)
    except usb.core.USBError as e:
        print(f"[restore] could not read device info ({e}); refusing an unidentified write.")
        return 1
    _unit_name(status)
    print(f"[restore] unit {status.get('part') or '?'}  serial {status.get('serial') or '?'}")
    # A mapnames file from another unit can point at maps this unit does not
    # have. Refuse an obvious units/<serial>/ mismatch before writing.
    parts = os.path.abspath(path).split(os.sep)
    if "units" in parts and status.get("serial"):
        i = len(parts) - 1 - parts[::-1].index("units")
        owner = parts[i + 1] if i + 1 < len(parts) else ""
        if owner and owner != _unit_name(status):
            print(f"[!] That file belongs to unit {owner}, but this unit is "
                  f"{status['serial']}. Refusing to cross-restore.")
            return 2
    try:
        upload_file(dev, P_MAPNAMES, data)
    except usb.core.USBError as e:
        print(f"[!] Upload failed: {e}")
        return 1
    try:
        back = read_devfile(dev, P_MAPNAMES)
    except (usb.core.USBError, DeviceTaskError):
        back = b""
    if back == data:
        print("[restore] verified by read-back.")
        return 0
    if not back or back == b"33":
        print("[!] Uploaded, but the read-back was empty, so the restore is NOT verified.")
        print("    Start the tool normally and check status before any write.")
        return 1
    print("[!] Read-back does not match. Do not assume the restore worked.")
    return 1


def main():
    print("=" * 60)
    print(" COBB AccessPort Marriage Toolkit")
    print("=" * 60)
    dev = connect()
    if dev is None:
        print("\n[!] No AccessPort found (USB VID 0x1A84).")
        print("    - Plug it in.")
        print("    - On Windows, bind the WinUSB driver to it with Zadig (see GUIDE).")
        return 1
    status = get_status(dev)
    _unit_name(status)
    print(f"\nConnected: {status['part']}  serial {status['serial']}  fw {status['firmware']}")
    print(f"State: {status['state']}   Vehicle: {status['vehicle']}")

    # capture the unit's own current mapnames as the clean restore reference
    try:
        clean_mapnames = read_devfile(dev, P_MAPNAMES)
        if not clean_mapnames or clean_mapnames == b"33":
            raise ValueError
    except Exception:
        print("[!] Could not read this unit's mapnames; cannot proceed safely.")
        return 1
    print(f"[init] captured this unit's mapnames ({len(clean_mapnames)}B) as restore reference.")
    if has_payload(clean_mapnames):
        print("[!] This unit's mapnames already contains a command payload.")
        print("    Stopping before the probe, which would evaluate it again.")
        print("    Restore a known-clean copy first:")
        print("      python3 tools/cobbtool.py --restore-mapnames units/<serial>/ap-data/mapnames")
        print("    (for the validated SUB-003, backup/ap-data/mapnames also works).")
        return 1

    # Find out what THIS unit actually provides instead of comparing part numbers.
    # The paths and binaries below are the portability contract; another SKU
    # or firmware must demonstrate them rather than inheriting compatibility.
    print("[init] probing this unit's paths, vendor binaries and eval path (~1 min) ...")
    probe = probe_unit(dev, clean_mapnames)
    if probe["ok"]:
        validated = (status["part"], status["firmware"]) in VALIDATED
        print("[init] capabilities: "
              f"data={probe['data_dir']}  user={probe['user_dir']}  "
              f"readeeprom=yes  writeeeprom=yes  "
              f"mapnames-in-diagnostic={'yes' if probe['eval'] else 'no'}  "
              f"tcm={'yes' if probe['has_tcm'] else 'no'}  "
              "(payload executed on this unit)")
        print(f"[init] {status['part'] or '?'} / {status['firmware'] or '?'} — "
              + ("validated unit." if validated else
                 "not in the validated set, but all required capabilities are present."))
    else:
        print("[!] Capability preflight FAILED. Missing: " + ", ".join(probe["missing"]))
        print("    Status and backup stay available; unmarry/re-marry are disabled.")
        print("    A missing piece means this unit does not expose the chain the tool")
        print("    relies on, so writing it would be a guess.")
    if probe.get("eeprom", {}).get("AP_STATE") is not None:
        print(f"[init] live EEPROM: AP_STATE={probe['eeprom'].get('AP_STATE')} "
              f"AP_VEHICLE={probe['eeprom'].get('AP_VEHICLE')}"
              + (f" AP_CM1_STATE={probe['eeprom'].get('AP_CM1_STATE')}"
                 if "AP_CM1_STATE" in probe["eeprom"] else ""))

    while True:
        print("\n" + "-" * 40)
        print(" 1) Show status")
        print(" 2) Back up this unit  (do this first!)")
        print(" 3) Unmarry  (state -> Not installed, then reboot)")
        print(" 4) Re-marry (state -> Installed, then reboot)")
        print(" 5) Quit")
        choice = input("Select: ").strip()
        try:
            if choice == "1":
                status = action_status(dev, clean_mapnames)
            elif choice == "2":
                do_backup(dev, status, clean_mapnames, probe)
            elif choice == "3":
                dev = action_toggle(dev, status, clean_mapnames, probe, 0)
                if dev is None:
                    dev = connect(quiet=True, expected_serial=status["serial"])
                    if dev is None:
                        print("[!] Unit not reachable. Replug it and restart.")
                        return 1
                status = get_status(dev)
            elif choice == "4":
                dev = action_toggle(dev, status, clean_mapnames, probe, 1)
                if dev is None:
                    dev = connect(quiet=True, expected_serial=status["serial"])
                    if dev is None:
                        print("[!] Unit not reachable. Replug it and restart.")
                        return 1
                status = get_status(dev)
            elif choice == "5":
                print("bye."); return 0
            else:
                print("?")
        except usb.core.USBError as e:
            print(f"[!] USB error: {e}. Reconnecting ...")
            dev = connect(quiet=True, expected_serial=status["serial"])
            if dev is None:
                print("[!] Unit not reachable. Replug it and restart.")
                return 1
            status = get_status(dev)


if __name__ == "__main__":
    if len(sys.argv) > 1:
        if sys.argv[1] in ("-h", "--help"):
            print(USAGE)
            raise SystemExit(0)
        if sys.argv[1] == "--restore-mapnames" and len(sys.argv) == 3:
            raise SystemExit(restore_mapnames(sys.argv[2]))
        print(USAGE)
        raise SystemExit(2)
    try:
        raise SystemExit(main() or 0)
    except (MapnamesRestoreError, DeviceTaskError) as e:
        print(f"\n[!] {e}")
        raise SystemExit(3)
    except KeyboardInterrupt:
        print("\ninterrupted.")
        raise SystemExit(130)
