import os as _os, sys as _sys  # repo-layout bootstrap (added by reorg)
_R = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
_sys.path[:0] = [_os.path.join(_R, _d) for _d in ("core", "tools", "emulator", "recon")]; _os.chdir(_R)
"""Device-independent tests for the marriage tool. No AP needed.
Run: python3 tests/test_cobbtool.py    (asserts; prints OK or raises)."""
import builtins
import contextlib
import io
import json
import tempfile
import hashlib

import usb.core

import cobbtool


# --- cmd 0x28 device-info parsing -------------------------------------------

SUB003_INFO = (
    "v1.7.4.2-24844\nAP3-SUB-003\nSUB0000001\nInstalled\n"
    "2010 USDM Impreza WRX MT\nECU-1\n"
)


def test_status_parses_the_captured_reply():
    d = cobbtool.parse_device_info(SUB003_INFO)
    assert d["firmware"] == "v1.7.4.2-24844", d
    assert d["part"] == "AP3-SUB-003", d
    assert d["serial"] == "SUB0000001", d
    assert d["state"] == "Installed", d
    assert d["vehicle"] == "2010 USDM Impreza WRX MT", d
    assert d["ecu"] == "ECU-1", d


def test_status_is_not_position_dependent():
    # another SKU/firmware may reorder or add a line; shape still identifies it
    text = ("AP-FOR-001\nv1.7.6.0-29170\nFOR0123456\nSome Ford Focus ST\n"
            "Not installed\nECU-4\n")
    d = cobbtool.parse_device_info(text)
    assert d["part"] == "AP-FOR-001", d
    assert d["firmware"] == "v1.7.6.0-29170", d
    assert d["serial"] == "FOR0123456", d
    assert d["state"] == "Not installed", d
    assert d["vehicle"] == "Some Ford Focus ST", d
    assert d["ecu"] == "ECU-4", d


def test_status_survives_a_missing_ecu_line():
    # an unmarried unit with no car attached can omit the ECU line
    d = cobbtool.parse_device_info("v1.7.4.2-24844\nAP3-SUB-004\nSUB0999999\nNot installed\n(none)\n")
    assert d["part"] == "AP3-SUB-004", d
    assert d["serial"] == "SUB0999999", d
    assert d["state"] == "Not installed", d
    assert d["vehicle"] == "(none)", d
    assert d["ecu"] == "", d


def test_status_empty_reply_does_not_raise():
    d = cobbtool.parse_device_info("")
    assert d["part"] == "" and d["state"] == "", d


# --- EEPROM / preflight parsing ---------------------------------------------

EEPROM_TEXT = """readeeprom: v1.7.6
AP_STATE=1
AP_VEHICLE=SUBA_US_WRXM_10
AP_MODEL=3
AP_CM1_STATE=1
AP_CM1_VEHICLE=SUBA_US_WRXM_10_TCM
"""


def test_eeprom_parses_shell_assignments():
    d = cobbtool.parse_eeprom(EEPROM_TEXT)
    assert d["AP_STATE"] == "1", d
    assert d["AP_VEHICLE"] == "SUBA_US_WRXM_10", d
    assert d["AP_CM1_VEHICLE"] == "SUBA_US_WRXM_10_TCM", d
    assert "P_DATA" not in d


PROBE_TEXT = """P_DATA=1
P_USER=1
P_READ=1
P_WRITE=1
P_STATE=1
P_VEH=1
P_DIAG=1
P_EVAL=1
P_TCM=1
""" + EEPROM_TEXT


def test_probe_ok_when_every_capability_is_present():
    p = cobbtool.parse_probe(PROBE_TEXT)
    assert p["ok"], p
    assert p["missing"] == [], p
    assert p["has_tcm"] is True, p
    assert p["eeprom"]["AP_STATE"] == "1", p


def test_probe_reports_the_eval_grep_without_gating_on_it():
    # The P_ lines only exist because the payload ran, so a missing mapnames
    # string in diagnostic_file is information, not a refusal.
    p = cobbtool.parse_probe(PROBE_TEXT.replace("P_EVAL=1", "P_EVAL=0"))
    assert p["ok"], p
    assert p["eval"] is False, p


def test_probe_with_no_answer_is_not_ok():
    p = cobbtool.parse_probe("")
    assert not p["ok"], p
    assert len(p["missing"]) == 7, p


def test_probe_refuses_a_writeeeprom_without_the_flags_we_call():
    p = cobbtool.parse_probe(PROBE_TEXT.replace("P_STATE=1", "P_STATE=0"))
    assert not p["ok"], p
    assert "writeeeprom --state" in p["missing"], p


# --- writeeeprom command construction ---------------------------------------

def test_unmarry_clears_ecu_only_when_there_is_no_tcm():
    cmd = cobbtool.build_marriage_cmd(0)
    assert cmd == "cd /ap-app/bin && ./writeeeprom --state=0 --vehicle=? --quiet", cmd
    assert "cm1" not in cmd, cmd


def test_unmarry_also_clears_a_married_tcm():
    cmd = cobbtool.build_marriage_cmd(0, cm1_state=0, cm1_vehicle="?")
    assert "--state=0 --vehicle=? --quiet" in cmd, cmd
    assert "--cm1_state=0 --cm1_vehicle=? --quiet" in cmd, cmd


def test_remarry_restores_ecu_and_tcm_from_the_snapshot():
    cmd = cobbtool.build_marriage_cmd(
        1, "SUBA_US_WRXM_10", cm1_state=1, cm1_vehicle="SUBA_US_WRXM_10_TCM")
    assert "--state=1 --vehicle=SUBA_US_WRXM_10 --quiet" in cmd, cmd
    assert "--cm1_state=1 --cm1_vehicle=SUBA_US_WRXM_10_TCM --quiet" in cmd, cmd


def test_remarry_refuses_a_missing_or_injectable_vehicle_code():
    for bad in ("", "?", "SUBA_US_WRXM_10; reboot -f", None):
        try:
            cobbtool.build_marriage_cmd(1, bad)
        except ValueError:
            continue
        raise AssertionError(f"accepted unsafe vehicle code {bad!r}")


# --- manifest / vehicle lookup ----------------------------------------------

def _with_unit(status, files):
    tmp = tempfile.mkdtemp(prefix="ap-units-")
    old = cobbtool.UNITS_DIR
    cobbtool.UNITS_DIR = tmp
    d = _os.path.join(tmp, status["serial"])
    _os.makedirs(d, exist_ok=True)
    for name, content in files.items():
        with open(_os.path.join(d, name), "w") as f:
            f.write(content)
    return tmp, old


def test_known_vehicle_prefers_the_live_eeprom_snapshot():
    status = {"serial": "SUB0000001"}
    manifest = {"vehicle_code_from_backup_file": "SUBA_US_WRX_02",
                "eeprom": {"AP_VEHICLE": "SUBA_US_WRXM_10"}}
    _, old = _with_unit(status, {"manifest.json": json.dumps(manifest)})
    try:
        assert cobbtool.known_vehicle(status) == "SUBA_US_WRXM_10"
    finally:
        cobbtool.UNITS_DIR = old


def test_known_vehicle_falls_back_to_the_backup_file():
    status = {"serial": "SUB0000001"}
    manifest = {"vehicle_code_from_backup_file": "SUBA_US_WRX_02", "eeprom": {}}
    _, old = _with_unit(status, {"manifest.json": json.dumps(manifest)})
    try:
        assert cobbtool.known_vehicle(status) == "SUBA_US_WRX_02"
    finally:
        cobbtool.UNITS_DIR = old


def test_empty_tcm_placeholder_is_not_a_code():
    status = {"serial": "SUB0000001"}
    manifest = {"eeprom": {"AP_CM1_VEHICLE": "?"}}
    _, old = _with_unit(status, {"manifest.json": json.dumps(manifest)})
    try:
        assert cobbtool.known_cm1_vehicle(status) == ""
    finally:
        cobbtool.UNITS_DIR = old


def test_snapshot_is_written_before_the_unmarry_write():
    status = {"serial": "SUB0000001"}
    tmp, old = _with_unit(status, {"manifest.json": json.dumps({"serial": "SUB0000001"})})
    try:
        cobbtool._save_eeprom_snapshot(status, {"AP_STATE": "1", "AP_VEHICLE": "SUBA_US_WRXM_10"})
        m = json.load(open(_os.path.join(tmp, "SUB0000001", "manifest.json")))
        assert m["serial"] == "SUB0000001", m
        assert m["eeprom"]["AP_VEHICLE"] == "SUBA_US_WRXM_10", m
        assert m["eeprom_captured"], m
    finally:
        cobbtool.UNITS_DIR = old


# --- toggle decisions (device calls mocked) ---------------------------------

PROBE_OK = {"ok": True, "missing": [], "has_tcm": True, "eval": True,
            "data_dir": "/user/ap-data", "user_dir": "/user/ap-user", "eeprom": {}}


def _run_toggle(to_state, live, status=None, probe=None, manifest=None):
    """Call action_toggle with the USB-facing pieces replaced; return the command."""
    status = status or {"serial": "SUB0000001", "part": "AP3-SUB-003",
                        "firmware": "v1.7.4.2-24844", "state": "Installed",
                        "vehicle": "2010 WRX"}
    probe = probe or dict(PROBE_OK)
    clean = b"mapnames"
    backup = {"serial": status["serial"], "part": status["part"], "firmware": status["firmware"],
              "backup_complete": True, "file_set": "listed",
              "files": {"ap-data/mapnames": {"size": len(clean),
                        "sha256": hashlib.sha256(clean).hexdigest()}}}
    if manifest is not None:
        backup.update(manifest)
    tmp, old_units = _with_unit(status, {"manifest.json": json.dumps(backup)})
    backup_dir = _os.path.join(tmp, status["serial"], "ap-data")
    _os.makedirs(backup_dir)
    with open(_os.path.join(backup_dir, "mapnames"), "wb") as f:
        f.write(clean)
    saved = (cobbtool.live_eeprom, cobbtool.exec_reboot, cobbtool.confirm)
    seen = {}

    cobbtool.live_eeprom = lambda dev, clean: live
    cobbtool.confirm = lambda msg: True
    def fake_exec(dev, wee, clean, **kw):
        seen["wee"] = wee
        return False          # stop before the reboot path prints anything else
    cobbtool.exec_reboot = fake_exec
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            cobbtool.action_toggle(None, status, b"mapnames", probe, to_state)
    finally:
        cobbtool.live_eeprom, cobbtool.exec_reboot, cobbtool.confirm = saved
        cobbtool.UNITS_DIR = old_units
    return seen.get("wee", "")


def test_unmarry_clears_a_married_tcm():
    wee = _run_toggle(0, {"AP_STATE": "1", "AP_VEHICLE": "SUBA_US_WRXM_10",
                          "AP_CM1_STATE": "1", "AP_CM1_VEHICLE": "SUBA_US_WRXM_10_TCM"})
    assert "--state=0 --vehicle=? --quiet" in wee, wee
    assert "--cm1_state=0 --cm1_vehicle=? --quiet" in wee, wee


def test_unmarry_skips_tcm_when_it_was_never_married():
    wee = _run_toggle(0, {"AP_STATE": "1", "AP_VEHICLE": "SUBA_US_WRXM_10",
                          "AP_CM1_STATE": "0", "AP_CM1_VEHICLE": "?"})
    assert "--state=0" in wee, wee
    assert "cm1" not in wee, wee


def test_unmarry_is_a_noop_when_already_uninstalled():
    wee = _run_toggle(0, {"AP_STATE": "0", "AP_VEHICLE": "?",
                          "AP_CM1_STATE": "0", "AP_CM1_VEHICLE": "?"})
    assert wee == "", wee


def test_remarry_restores_the_tcm_vehicle_from_the_snapshot():
    snapshot = {"AP_STATE": "1", "AP_VEHICLE": "SUBA_US_WRXM_10",
                "AP_CM1_STATE": "1", "AP_CM1_VEHICLE": "SUBA_US_WRXM_10_TCM"}
    wee = _run_toggle(1, snapshot, manifest={"eeprom": snapshot})
    assert "--state=1 --vehicle=SUBA_US_WRXM_10 --quiet" in wee, wee
    assert "--cm1_state=1 --cm1_vehicle=SUBA_US_WRXM_10_TCM --quiet" in wee, wee


def test_a_failed_preflight_never_reads_or_writes_the_device():
    status = {"serial": "SUB0000001"}
    called = {"live": False, "exec": False}
    saved = (cobbtool.live_eeprom, cobbtool.exec_reboot)
    cobbtool.live_eeprom = lambda *a: called.__setitem__("live", True)
    cobbtool.exec_reboot = lambda *a, **k: called.__setitem__("exec", True)
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            cobbtool.action_toggle(None, status, b"mapnames",
                                   {"ok": False, "missing": ["writeeeprom"]}, 0)
    finally:
        cobbtool.live_eeprom, cobbtool.exec_reboot = saved
    assert called == {"live": False, "exec": False}, called


def test_an_unknown_state_is_refused_before_any_write():
    status = {"serial": "SUB0000001"}
    called = {"exec": False}
    tmp, old_units = _with_unit(status, {"manifest.json": json.dumps({"serial": "SUB0000001"})})
    saved = (cobbtool.live_eeprom, cobbtool.exec_reboot)
    cobbtool.live_eeprom = lambda *a: {"AP_STATE": "9", "AP_VEHICLE": "SUBA_US_WRXM_10"}
    cobbtool.exec_reboot = lambda *a, **k: called.__setitem__("exec", True)
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            cobbtool.action_toggle(None, status, b"mapnames", dict(PROBE_OK), 0)
    finally:
        cobbtool.live_eeprom, cobbtool.exec_reboot = saved
        cobbtool.UNITS_DIR = old_units
    assert called["exec"] is False, called


def test_unmarry_aborts_when_no_vehicle_code_can_be_recorded():
    status = {"serial": "SUB0000001"}
    called = {"exec": False}
    tmp, old_units = _with_unit(status, {"manifest.json": json.dumps({"serial": "SUB0000001"})})
    saved = (cobbtool.live_eeprom, cobbtool.exec_reboot, builtins.input)
    cobbtool.live_eeprom = lambda *a: {"AP_STATE": "1", "AP_VEHICLE": "?",
                                       "AP_CM1_STATE": "0"}
    cobbtool.exec_reboot = lambda *a, **k: called.__setitem__("exec", True)
    builtins.input = lambda *a: ""
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            cobbtool.action_toggle(None, status, b"mapnames", dict(PROBE_OK), 0)
    finally:
        cobbtool.live_eeprom, cobbtool.exec_reboot, builtins.input = saved
        cobbtool.UNITS_DIR = old_units
    assert called["exec"] is False, called


def test_exec_reboot_aborts_before_the_payload_if_cleanup_dry_run_fails():
    clean = b"reflash=/user/ap-user/maps/x.ptm\nrealtime=/user/ap-user/maps/x.ptm\n"
    uploads = []
    saved = (cobbtool.upload_file, cobbtool.read_devfile, cobbtool.device_exec)
    cobbtool.upload_file = lambda dev, dest, data: uploads.append((dest, data))
    cobbtool.read_devfile = lambda dev, path: clean
    cobbtool.device_exec = lambda *a, **k: b""          # dry run did not report OK
    try:
        assert cobbtool.exec_reboot(None, "./writeeeprom --state=0", clean) is False
    finally:
        cobbtool.upload_file, cobbtool.read_devfile, cobbtool.device_exec = saved
    assert len(uploads) == 1, uploads                    # stage only; no poison written
    assert uploads[0][1] == clean, uploads


def test_verify_reports_a_successful_unmarry():
    out = io.StringIO()
    saved = cobbtool.live_eeprom
    cobbtool.live_eeprom = lambda *a: {"AP_STATE": "0", "AP_VEHICLE": "?"}
    try:
        with contextlib.redirect_stdout(out):
            cobbtool._verify_toggle(None, b"mapnames", 0, None)
    finally:
        cobbtool.live_eeprom = saved
    assert "verified" in out.getvalue(), out.getvalue()


def test_verify_shouts_when_the_write_did_not_take():
    out = io.StringIO()
    saved = cobbtool.live_eeprom
    cobbtool.live_eeprom = lambda *a: {"AP_STATE": "1", "AP_VEHICLE": "SUBA_US_WRXM_10"}
    try:
        with contextlib.redirect_stdout(out):
            cobbtool._verify_toggle(None, b"mapnames", 0, None)
    finally:
        cobbtool.live_eeprom = saved
    assert "VERIFY FAILED" in out.getvalue(), out.getvalue()


def test_payload_detection_finds_leftover_command_substitution():
    assert cobbtool.has_payload(b"reflash=$(id)\n")
    assert cobbtool.has_payload(b"realtime=`id`\n")
    assert not cobbtool.has_payload(b"reflash=/ap-data/maps/x.ptm\n")
    assert not cobbtool.has_payload(b"# reflash=$(id)\n")


def test_restore_refuses_a_file_that_is_itself_a_payload():
    f = tempfile.NamedTemporaryFile("w", delete=False)
    f.write("reflash=$(id)\n")
    f.close()
    out = io.StringIO()
    try:
        with contextlib.redirect_stdout(out):
            rc = cobbtool.restore_mapnames(f.name)
    finally:
        _os.unlink(f.name)
    assert rc == 2, rc
    assert "command payload" in out.getvalue(), out.getvalue()


# --- safety regressions -----------------------------------------------------

CLEAN_MAPNAMES = (b"reflash=/user/ap-user/maps/x.ptm\n"
                  b"realtime=/user/ap-user/maps/x.ptm\n")


def test_device_exec_still_restores_when_the_task_raises():
    uploads = []
    saved = (cobbtool.upload_file, cobbtool.run_task, cobbtool.read_devfile,
             cobbtool._run_marker)
    cobbtool._run_marker = lambda: "#RUN:test"
    cobbtool.upload_file = lambda dev, dest, data: uploads.append((dest, data))
    def boom(*a, **k):
        raise usb.core.USBError("task failed")
    cobbtool.run_task = boom
    cobbtool.read_devfile = lambda dev, path: CLEAN_MAPNAMES
    try:
        try:
            cobbtool.device_exec(None, "echo hi > /tmp/ee", "ee", CLEAN_MAPNAMES)
        except usb.core.USBError:
            pass
        else:
            raise AssertionError("the USB error did not propagate")
    finally:
        (cobbtool.upload_file, cobbtool.run_task, cobbtool.read_devfile,
         cobbtool._run_marker) = saved
    assert len(uploads) == 2, uploads
    assert b"reflash=$(" in uploads[0][1], uploads
    assert uploads[1] == (cobbtool.P_MAPNAMES, CLEAN_MAPNAMES), uploads


def test_device_exec_rejects_a_stale_tmp_file():
    saved = (cobbtool.upload_file, cobbtool.run_task, cobbtool.read_devfile,
             cobbtool._run_marker)
    old_timeout = cobbtool.MARKER_TIMEOUT
    cobbtool.MARKER_TIMEOUT = 0
    cobbtool._run_marker = lambda: "#RUN:test"
    cobbtool.upload_file = lambda *a, **k: None
    cobbtool.run_task = lambda *a, **k: (True, b"")
    def read(dev, path):
        if path.endswith("/ee"):
            return b"OLD CLEANUP_OK\n"
        return CLEAN_MAPNAMES
    cobbtool.read_devfile = read
    try:
        try:
            cobbtool.device_exec(None, "echo hi > /tmp/ee", "ee", CLEAN_MAPNAMES)
        except cobbtool.DeviceTaskError:
            pass
        else:
            raise AssertionError("stale output was accepted")
    finally:
        (cobbtool.upload_file, cobbtool.run_task, cobbtool.read_devfile,
         cobbtool._run_marker) = saved
        cobbtool.MARKER_TIMEOUT = old_timeout


def test_device_exec_fails_loudly_when_restore_is_unverified():
    saved = (cobbtool.upload_file, cobbtool.run_task, cobbtool.read_devfile,
             cobbtool._run_marker)
    old_timeout = cobbtool.MARKER_TIMEOUT
    cobbtool.MARKER_TIMEOUT = 0
    cobbtool._run_marker = lambda: "#RUN:test"
    cobbtool.upload_file = lambda *a, **k: None
    cobbtool.run_task = lambda *a, **k: (True, b"")
    def read(dev, path):
        if path.endswith("/ee"):
            return b"OK\n#RUN:test\n"
        return b""
    cobbtool.read_devfile = read
    try:
        try:
            cobbtool.device_exec(None, "echo hi > /tmp/ee", "ee", CLEAN_MAPNAMES)
        except cobbtool.MapnamesRestoreError:
            pass
        else:
            raise AssertionError("unverified restore was accepted")
    finally:
        (cobbtool.upload_file, cobbtool.run_task, cobbtool.read_devfile,
         cobbtool._run_marker) = saved
        cobbtool.MARKER_TIMEOUT = old_timeout


class _TriggerFailDev:
    def __init__(self):
        self.writes = 0

    def write(self, ep, data, timeout=None):
        self.writes += 1
        if self.writes == 5:          # stage setup/data + poison setup/data first
            raise usb.core.USBError("trigger failed")

    def read(self, ep, size, timeout=0):
        raise usb.core.USBTimeoutError("quiet")


def test_exec_reboot_disarms_when_the_trigger_write_fails():
    dev = _TriggerFailDev()
    uploads = []
    real_upload = cobbtool.upload_file
    saved = (cobbtool.device_exec, cobbtool.read_devfile)
    cobbtool.device_exec = lambda *a, **k: b"CLEANUP_OK"
    cobbtool.read_devfile = lambda dev, path: CLEAN_MAPNAMES
    def record(dev, dest, data):
        uploads.append((dest, data))
        return real_upload(dev, dest, data)
    cobbtool.upload_file = record
    try:
        started = cobbtool.exec_reboot(dev, "./writeeeprom --state=0 --vehicle=? --quiet",
                                       CLEAN_MAPNAMES)
    finally:
        cobbtool.upload_file = real_upload
        cobbtool.device_exec, cobbtool.read_devfile = saved
    assert started is None, started      # trigger ACK failed; outcome is unknown
    assert dev.writes >= 7, dev.writes
    assert uploads[-1] == (cobbtool.P_MAPNAMES, CLEAN_MAPNAMES), uploads


def test_device_exec_restore_failure_replaces_the_task_error():
    saved = (cobbtool.upload_file, cobbtool.run_task, cobbtool.read_devfile,
             cobbtool._run_marker)
    cobbtool._run_marker = lambda: "#RUN:test"
    cobbtool.upload_file = lambda *a, **k: None
    def boom(*a, **k):
        raise usb.core.USBError("task failed")
    cobbtool.run_task = boom
    cobbtool.read_devfile = lambda *a, **k: b""      # restore read-back is empty
    try:
        try:
            cobbtool.device_exec(None, "echo hi > /tmp/ee", "ee", CLEAN_MAPNAMES)
        except cobbtool.MapnamesRestoreError:
            pass
        else:
            raise AssertionError("the original USB error was allowed to win")
    finally:
        (cobbtool.upload_file, cobbtool.run_task, cobbtool.read_devfile,
         cobbtool._run_marker) = saved


class _FakeDev:
    def read(self, *a, **k):
        return b""

    def write(self, *a, **k):
        return None


def test_wait_reconnect_requires_a_disconnect_before_accepting():
    old, new = _FakeDev(), _FakeDev()
    seq = [old, None, new]
    def fake_connect(*a, **k):
        return seq.pop(0) if seq else None
    saved = (cobbtool.connect, cobbtool.client.flush_input, cobbtool.client.send,
             cobbtool.client.parse_response, cobbtool.time.sleep)
    cobbtool.connect = fake_connect
    cobbtool.client.flush_input = lambda *a, **k: None
    cobbtool.client.send = lambda *a, **k: b"v1.0"
    cobbtool.client.parse_response = lambda b: "v1.0"
    cobbtool.time.sleep = lambda s: None
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            dev, ver = cobbtool.wait_reconnect(total=10, grace=0)
    finally:
        (cobbtool.connect, cobbtool.client.flush_input, cobbtool.client.send,
         cobbtool.client.parse_response, cobbtool.time.sleep) = saved
    assert dev is new, dev
    assert ver == "v1.0", ver


def test_wait_reconnect_does_not_accept_a_unit_that_never_left():
    old = _FakeDev()
    saved = (cobbtool.connect, cobbtool.time.sleep, cobbtool.time.time)
    ticks = iter([0, 1, 6, 7])
    cobbtool.connect = lambda *a, **k: old
    cobbtool.time.sleep = lambda s: None
    cobbtool.time.time = lambda: next(ticks, 999)
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            dev, ver = cobbtool.wait_reconnect(total=5, grace=0)
    finally:
        cobbtool.connect, cobbtool.time.sleep, cobbtool.time.time = saved
    assert dev is None and ver is None, (dev, ver)


def test_probe_with_no_fresh_output_stays_read_only():
    saved = cobbtool.device_exec
    cobbtool.device_exec = lambda *a, **k: (_ for _ in ()).throw(
        cobbtool.DeviceTaskError("no fresh output"))
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            p = cobbtool.probe_unit(None, b"mapnames")
    finally:
        cobbtool.device_exec = saved
    assert not p["ok"], p


def test_backup_does_not_replace_an_installed_snapshot_with_an_uninstalled_read():
    status = {"serial": "SUB0000001", "part": "AP3-SUB-003",
              "firmware": "v1.7.4.2-24844", "state": "Not installed",
              "vehicle": "(none)"}
    old_manifest = {
        "serial": status["serial"],
        "vehicle_code_from_backup_file": "SUBA_US_WRXM_10",
        "eeprom": {"AP_STATE": "1", "AP_VEHICLE": "SUBA_US_WRXM_10",
                   "AP_CM1_STATE": "1", "AP_CM1_VEHICLE": "SUBA_US_WRXM_10_TCM"},
    }
    tmp, old = _with_unit(status, {"manifest.json": json.dumps(old_manifest)})
    saved = (cobbtool._names_in, cobbtool.read_devfile)
    cobbtool._names_in = (lambda dev, devdir, fallback, sizes=None:
                          (["mapnames"], True) if devdir == "../ap-data" else ([], True))
    cobbtool.read_devfile = lambda dev, path: b"mapnames"
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            cobbtool.do_backup(None, status, b"mapnames",
                               {"eeprom": {"AP_STATE": "0", "AP_VEHICLE": "?",
                                           "AP_CM1_STATE": "0", "AP_CM1_VEHICLE": "?"}})
        m = json.load(open(_os.path.join(tmp, "SUB0000001", "manifest.json")))
    finally:
        cobbtool._names_in, cobbtool.read_devfile = saved
        cobbtool.UNITS_DIR = old
    assert m["eeprom"]["AP_VEHICLE"] == "SUBA_US_WRXM_10", m
    assert m["eeprom"]["AP_CM1_VEHICLE"] == "SUBA_US_WRXM_10_TCM", m
    assert m["vehicle_code_from_backup_file"] == "SUBA_US_WRXM_10", m
    assert "ap-data/mapnames" in m["saved_files"], m


def test_backup_leaves_the_manifest_alone_when_nothing_could_be_read():
    status = {"serial": "SUB0000001", "part": "AP3-SUB-003",
              "firmware": "v1.7.4.2-24844", "state": "Not installed",
              "vehicle": "(none)"}
    old_manifest = {"serial": status["serial"], "timestamp": "keep-me",
                    "eeprom": {"AP_STATE": "1", "AP_VEHICLE": "SUBA_US_WRXM_10"}}
    tmp, old = _with_unit(status, {"manifest.json": json.dumps(old_manifest)})
    saved = (cobbtool._names_in, cobbtool.read_devfile)
    cobbtool._names_in = lambda dev, devdir, fallback, sizes=None: ([], True)
    cobbtool.read_devfile = lambda dev, path: b""
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            cobbtool.do_backup(None, status, b"mapnames", {"eeprom": {}})
        m = json.load(open(_os.path.join(tmp, "SUB0000001", "manifest.json")))
    finally:
        cobbtool._names_in, cobbtool.read_devfile = saved
        cobbtool.UNITS_DIR = old
    assert m == old_manifest, m


def test_verify_checks_the_tcm_state():
    out = io.StringIO()
    saved = cobbtool.live_eeprom
    cobbtool.live_eeprom = lambda *a: {"AP_STATE": "0", "AP_VEHICLE": "?",
                                      "AP_CM1_STATE": "1"}
    try:
        with contextlib.redirect_stdout(out):
            cobbtool._verify_toggle(None, b"mapnames", 0, None, 0, None)
    finally:
        cobbtool.live_eeprom = saved
    assert "VERIFY FAILED" in out.getvalue(), out.getvalue()


def test_payload_detection_finds_a_quote_breakout():
    assert cobbtool.has_payload(b'reflash=x"; id; echo "\n')
    assert not cobbtool.has_payload(b"reflash=/user/ap-user/maps/x.ptm\n")


def test_marriage_commands_are_chained_with_and():
    cmd = cobbtool.build_marriage_cmd(0, cm1_state=0)
    assert " ; " not in cmd, cmd
    assert " && ./writeeeprom --cm1_state=0" in cmd, cmd


def test_unmarry_refuses_when_the_tcm_state_is_missing():
    status = {"serial": "SUB0000001"}
    tmp, old = _with_unit(status, {"manifest.json": json.dumps({"serial": "SUB0000001"})})
    called = {"exec": False}
    saved = (cobbtool.live_eeprom, cobbtool.exec_reboot)
    cobbtool.live_eeprom = lambda *a: {"AP_STATE": "1", "AP_VEHICLE": "SUBA_US_WRXM_10"}
    cobbtool.exec_reboot = lambda *a, **k: called.__setitem__("exec", True)
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            cobbtool.action_toggle(None, status, b"mapnames", dict(PROBE_OK), 0)
    finally:
        cobbtool.live_eeprom, cobbtool.exec_reboot = saved
        cobbtool.UNITS_DIR = old
    assert called["exec"] is False, called


def test_restore_mapnames_treats_an_empty_readback_as_unverified():
    f = tempfile.NamedTemporaryFile("wb", delete=False)
    f.write(b"reflash=/user/ap-user/maps/x.ptm\n")
    f.close()
    saved = (cobbtool.connect, cobbtool.get_status, cobbtool.upload_file,
             cobbtool.read_devfile)
    cobbtool.connect = lambda *a, **k: object()
    cobbtool.get_status = lambda dev: {"serial": "SUB0000001"}
    cobbtool.upload_file = lambda *a, **k: None
    cobbtool.read_devfile = lambda *a, **k: b""
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            rc = cobbtool.restore_mapnames(f.name)
    finally:
        _os.unlink(f.name)
        (cobbtool.connect, cobbtool.get_status, cobbtool.upload_file,
         cobbtool.read_devfile) = saved
    assert rc == 1, rc


def test_restore_mapnames_refuses_another_units_file():
    with tempfile.TemporaryDirectory(prefix="ap-restore-") as tmp:
        d = _os.path.join(tmp, "units", "OTHER")
        _os.makedirs(d)
        p = _os.path.join(d, "mapnames")
        with open(p, "wb") as f:
            f.write(b"reflash=/user/ap-user/maps/x.ptm\n")
        called = {"upload": False}
        saved = (cobbtool.connect, cobbtool.get_status, cobbtool.upload_file)
        cobbtool.connect = lambda *a, **k: object()
        cobbtool.get_status = lambda dev: {"serial": "SUB0000001"}
        cobbtool.upload_file = lambda *a, **k: called.__setitem__("upload", True)
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                rc = cobbtool.restore_mapnames(p)
        finally:
            cobbtool.connect, cobbtool.get_status, cobbtool.upload_file = saved
    assert rc == 2, rc
    assert called["upload"] is False, called


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
    print(f"OK ({len(tests)} tests)")
