"""Offline regression checks for backup integrity, cleanup and device identity.

Run directly: python3 tests/test_cobbtool_safety.py
No real USB discovery or device commands are used.
"""
import contextlib
import hashlib
import importlib.util
import io
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('cobbtool_suite', Path(__file__).with_name('test_cobbtool.py'))
suite = importlib.util.module_from_spec(spec)
spec.loader.exec_module(suite)
c = suite.cobbtool
import protocol

STATUS = {'serial': 'FOR0123456', 'part': 'AP3-FOR-001', 'firmware': 'v1.0',
          'state': 'Installed', 'vehicle': 'Ford'}
CLEAN = b'reflash=/user/ap-user/maps/example.ptm\n'
LIVE = {'AP_STATE': '1', 'AP_VEHICLE': 'FORD_FOCUS',
        'AP_CM1_STATE': '1', 'AP_CM1_VEHICLE': 'FORD_TCM'}


class SafetyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.units = patch.object(c, 'UNITS_DIR', self.tmp.name)
        self.units.start()
        self.addCleanup(self.units.stop)
        self.output = contextlib.redirect_stdout(io.StringIO())
        self.output.__enter__()
        self.addCleanup(self.output.__exit__, None, None, None)

    def backup(self, files=None, listed=True):
        files = files or {'../ap-data/mapnames': CLEAN, '../ap-data/shared': b'data', 'shared': b'user'}
        def names(dev, directory, fallback, sizes=None):
            prefix = '../ap-data/' if directory == '../ap-data' else ''
            chosen = {key[len(prefix):]: value for key, value in files.items()
                      if (key.startswith('../ap-data/') if prefix else '/' not in key)}
            if sizes is not None:
                sizes.update({key: len(value) for key, value in chosen.items()})
            return list(chosen), listed
        with patch.object(c, '_names_in', names), patch.object(c, 'read_devfile', side_effect=lambda dev, name: files[name]):
            return c.do_backup(None, STATUS, CLEAN, {'eeprom': LIVE})

    def test_multiframe_download_keeps_all_data(self):
        self.assertEqual(c.unwrap(protocol.build(1, b'first') + protocol.build(1, b'second')), b'firstsecond')

    def download(self, data, pieces, declared=None):
        import struct
        import usb.core
        name = b'mapnames'
        stat = b'serialization::archive' + b'\0'*9 + b'\0\1' + struct.pack('<I', len(name)) + name + struct.pack('<I', len(data) if declared is None else declared)
        replies = iter(pieces)
        def read(*args, **kwargs):
            try:
                return next(replies)
            except StopIteration:
                raise usb.core.USBTimeoutError('end')
        dev = SimpleNamespace(read=read, write=lambda *a, **kw: None)
        with patch.object(c.client, 'flush_input'), patch.object(c.client, 'send', return_value=stat):
            return c.read_devfile(dev, 'mapnames')

    def test_download_handles_fragmentation_coalescing_and_md5_file(self):
        for data in (b'hello world', b'f'*32, b''):
            content = protocol.build(1, data[:3]) + protocol.build(1, data[3:]) if data else b''
            wire = content + protocol.build(1, hashlib.md5(data).hexdigest().encode())
            for pieces in ([wire], [wire[i:i+2] for i in range(0, len(wire), 2)]):
                self.assertEqual(self.download(data, pieces), data)

    def test_download_accepts_raw_content_with_verified_ack(self):
        data = b'plain text map file'
        wire = data + protocol.build(1, hashlib.md5(data).hexdigest().encode())
        self.assertEqual(self.download(data, [wire[:4], wire[4:]]), data)

    def test_download_rejects_missing_wrong_or_truncated_ack_and_wrong_size(self):
        data = b'hello'
        content = protocol.build(1, data)
        ack = protocol.build(1, hashlib.md5(data).hexdigest().encode())
        for wire in (content, content + protocol.build(1, b'0'*32), content + ack[:-2],
                     protocol.build(1, b'jello') + ack):
            with self.subTest(wire=wire), self.assertRaises(c.DeviceTaskError):
                self.download(data, [wire])
        with self.assertRaises(c.DeviceTaskError):
            self.download(data, [content + ack], declared=6)

    def test_corrupt_or_truncated_frames_fail(self):
        frame = protocol.build(1, b'payload')
        for data in (frame[:-1], frame + b'junk', b'\x02\0\0\0\x03\0\1' + b'\0'*4):
            with self.subTest(data=data), self.assertRaises(c.DeviceTaskError):
                c.unwrap(data)

    def test_missing_and_unsafe_serials_refused(self):
        for serial in (None, '', 'unknown', '..', 'FOR/123', 'FOR.123'):
            with self.subTest(serial=serial), self.assertRaises(c.DeviceTaskError):
                c.unit_dir({'serial': serial})

    def test_status_parser_does_not_invent_missing_serial(self):
        self.assertEqual(c.parse_device_info('v1.0\nAP3-FOR-001\nInstalled\nFord\nextra\n')['serial'], '')

    def test_backup_preserves_same_named_files_and_verifies(self):
        self.backup()
        root = Path(c.unit_dir(STATUS))
        self.assertEqual((root / 'ap-data/shared').read_bytes(), b'data')
        self.assertEqual((root / 'ap-user/shared').read_bytes(), b'user')
        self.assertTrue(c._verified_backup(STATUS, CLEAN))

    def test_modified_or_missing_backup_file_blocks_write(self):
        self.backup()
        target = Path(c.unit_dir(STATUS)) / 'ap-user/shared'
        target.write_bytes(b'changed')
        self.assertFalse(c._verified_backup(STATUS, CLEAN))
        target.unlink()
        self.assertFalse(c._verified_backup(STATUS, CLEAN))

    def test_wrong_owner_fallback_and_legacy_backups_block_write(self):
        self.backup(listed=False)
        self.assertFalse(c._verified_backup(STATUS, CLEAN))
        self.backup()
        manifest_path = Path(c._manifest_path(STATUS))
        manifest = json.loads(manifest_path.read_text())
        for updates in ({'serial': 'SUB0000001'}, {'part': 'AP3-SUB-003'},
                        {'firmware': 'v2.0'}, {'backup_complete': False}, {'files': {}}):
            manifest_path.write_text(json.dumps(dict(manifest, **updates)))
            self.assertFalse(c._verified_backup(STATUS, CLEAN))
        manifest_path.write_text('{}')
        self.assertFalse(c._verified_backup(STATUS, CLEAN))

    def test_bad_backup_size_leaves_previous_backup_untouched(self):
        self.backup()
        original = Path(c._manifest_path(STATUS)).read_bytes()
        with patch.object(c, 'read_devfile', return_value=b'x'), patch.object(c.backup_mod, 'list_dir',
                return_value=[{'name': 'mapnames', 'size': 100}]):
            c.do_backup(None, STATUS, CLEAN)
        self.assertEqual(Path(c._manifest_path(STATUS)).read_bytes(), original)
        self.assertTrue(c._verified_backup(STATUS, CLEAN))

    def test_unsafe_listing_filename_does_not_escape_backup(self):
        with patch.object(c.backup_mod, 'list_dir', return_value=[{'name': '../escape', 'size': 1}]), patch.object(c, 'read_devfile') as read:
            c.do_backup(None, STATUS, CLEAN)
        read.assert_not_called()
        self.assertFalse(Path(c._manifest_path(STATUS)).exists())

    def test_incomplete_directory_listing_is_rejected(self):
        import struct
        response = b'serialization::archive' + b'\0'*9 + b'\0\0' + struct.pack('<I', 2)
        with patch.object(c.backup_mod, 'flush_input'), patch.object(c.backup_mod, 'send', return_value=response):
            with self.assertRaises(ValueError):
                c.backup_mod.list_dir(None, '.', strict=True)

    def test_invalid_tcm_state_or_missing_code_blocks_clear(self):
        for updates in ({'AP_CM1_STATE': 'unexpected'}, {'AP_CM1_STATE': ''}, {'AP_CM1_VEHICLE': '?'}):
            self.assertEqual(suite._run_toggle(0, dict(LIVE, **updates)), '')

    def test_missing_saved_tcm_code_blocks_partial_remarry(self):
        snapshot = dict(LIVE, AP_CM1_VEHICLE='?')
        self.assertEqual(suite._run_toggle(1, LIVE, manifest={'eeprom': snapshot}), '')

    def test_empty_or_wrong_owner_manifest_blocks_clear(self):
        for manifest in ({'files': {}, 'saved_files': []}, {'serial': 'OTHER'}):
            self.assertEqual(suite._run_toggle(0, LIVE, manifest=manifest), '')

    def test_ecu_snapshot_does_not_erase_independent_tcm_snapshot(self):
        merged = c._merge_eeprom(LIVE, dict(LIVE, AP_CM1_STATE='0', AP_CM1_VEHICLE='?'))
        self.assertEqual(merged['AP_CM1_STATE'], '1')
        self.assertEqual(merged['AP_CM1_VEHICLE'], 'FORD_TCM')

    def capture_cleanup(self):
        captured, uploads = {}, []
        def task(dev, command, outfile, clean):
            captured['dry'] = command
            return b'CLEANUP_OK\n'
        with patch.object(c, 'device_exec', task), patch.object(c, 'read_devfile', return_value=CLEAN), \
             patch.object(c, 'upload_file', side_effect=lambda dev, dest, data: uploads.append(data)), \
             patch.object(c.client, 'flush_input'):
            c.exec_reboot(SimpleNamespace(write=lambda *a, **kw: None), 'writeeeprom', CLEAN)
        captured['payload'] = uploads[-1].decode().splitlines()[0][len('reflash=$('):-1]
        return captured

    def shell(self, command, failing):
        # Every device command is a shell function. No mount, copy or reboot
        # executable can run on the host; only the temporary log is written.
        log = Path(self.tmp.name) / 'calls'
        output = Path(self.tmp.name) / 'cleanup'
        functions = []
        for name in ('writeeeprom', 'mount', 'cp', 'cmp', 'sync', 'reboot'):
            functions.append(f'{name}() {{ echo {name} >> "{log}"; return {1 if name == failing else 0}; }};')
        result = subprocess.run(['/bin/sh', '-c', '\n'.join(functions) + command.replace('/tmp/cleanup', str(output))], capture_output=True)
        calls = log.read_text().splitlines()
        log.unlink()
        return result, output.read_bytes() if output.exists() else b'', calls

    def test_cleanup_dry_run_reports_no_success_after_failure(self):
        dry = self.capture_cleanup()['dry']
        for failing in ('mount', 'cp', 'cmp', 'sync'):
            result, output, _ = self.shell(dry, failing)
            self.assertNotEqual(result.returncode, 0)
            self.assertNotIn(b'CLEANUP_OK', output)
        result, output, _ = self.shell(dry, None)
        self.assertEqual(result.returncode, 0)
        self.assertIn(b'CLEANUP_OK', output)

    def test_payload_disarms_after_write_failure_but_never_reboots_after_failure(self):
        payload = self.capture_cleanup()['payload']
        for failing in ('writeeeprom', 'mount', 'cp', 'cmp', 'sync'):
            _, _, calls = self.shell(payload, failing)
            self.assertNotIn('reboot', calls)
            if failing == 'writeeeprom':
                self.assertIn('cp', calls)
        _, _, calls = self.shell(payload, None)
        self.assertEqual(calls[-1], 'reboot')

    def test_failed_cleanup_gate_does_not_upload_payload(self):
        with patch.object(c, 'device_exec', return_value=b'cp failed\n'), patch.object(c, 'read_devfile', return_value=CLEAN), patch.object(c, 'upload_file') as upload:
            self.assertFalse(c.exec_reboot(None, 'writeeeprom', CLEAN))
        self.assertEqual(upload.call_count, 1)  # staging only

    def fake_device(self, serial):
        endpoints = [SimpleNamespace(bEndpointAddress=ep, bmAttributes=2) for ep in (c.client.EP_IN, c.client.EP_OUT)]
        return SimpleNamespace(serial=serial, set_configuration=lambda n: None,
                               get_active_configuration=lambda: {(0, 0): endpoints})

    def test_connection_refuses_ambiguous_initial_selection(self):
        with patch.object(c.usb.core, 'find', return_value=[self.fake_device('a'), self.fake_device('b')]), patch.object(c.usb.util, 'claim_interface') as claim:
            self.assertIsNone(c.connect())
        claim.assert_not_called()

    def test_reconnect_selects_original_serial_among_other_devices(self):
        other, original = self.fake_device('SUB0000001'), self.fake_device(STATUS['serial'])
        with patch.object(c.usb.core, 'find', return_value=[other, original]), patch.object(c.usb.util, 'claim_interface'), \
             patch.object(c.usb.util, 'dispose_resources'), patch.object(c, 'get_status', side_effect=lambda dev: {'serial': dev.serial}):
            self.assertIs(c.connect(expected_serial=STATUS['serial']), original)
        with patch.object(c, 'connect', side_effect=[None, original]) as connect, patch.object(c.time, 'sleep'), \
             patch.object(c.client, 'flush_input'), patch.object(c.client, 'send'), patch.object(c.client, 'parse_response', return_value='v1.0'):
            self.assertIs(c.wait_reconnect(expected_serial=STATUS['serial'])[0], original)
        for call in connect.call_args_list:
            self.assertEqual(call.kwargs['expected_serial'], STATUS['serial'])

    def test_connection_rejects_incompatible_endpoints(self):
        dev = self.fake_device(STATUS['serial'])
        dev.get_active_configuration = lambda: {(0, 0): []}
        with patch.object(c.usb.core, 'find', return_value=[dev]), patch.object(c.usb.util, 'dispose_resources'), patch.object(c.usb.util, 'claim_interface') as claim:
            self.assertIsNone(c.connect())
        claim.assert_not_called()


if __name__ == '__main__':
    unittest.main()
