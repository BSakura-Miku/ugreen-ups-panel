import copy
import json
from pathlib import Path
import struct
from types import SimpleNamespace

import pytest

from ups_panel import collector, doctor
from ups_panel.protocol import parse_frame

ROOT = Path(__file__).parents[1]
FRAME = bytes.fromhex((ROOT / 'fixtures/online.hex').read_text().splitlines()[0])
DEVICE = {'bus': 3, 'device': 2, 'path': '3-PRIVATE-PATH', 'serial': 'PRIVATE-SERIAL'}


class Clock:
    now = 500.0

    def time(self):
        return self.now

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now = round(self.now + seconds, 6)


def packet(timestamp, device=2):
    header = bytearray(64)
    header[8:12] = bytes([ord('C'), 1, 0x81, device])
    struct.pack_into('=H', header, 12, 3)
    struct.pack_into('=q', header, 16, int(timestamp))
    struct.pack_into('=i', header, 24, int((timestamp - int(timestamp)) * 1_000_000))
    struct.pack_into('=iII', header, 28, -2, 64, 64)
    return bytes(header), FRAME


def live_snapshot(timestamp=500):
    return {'schema': 1, 'source': 'usbmon', 'heartbeat': timestamp,
            'device': DEVICE.copy(), 'sample': parse_frame(FRAME, timestamp, -2),
            'nut': {'target': 'PRIVATE-TARGET@localhost'}}


@pytest.fixture
def environment(tmp_path, monkeypatch):
    clock = Clock()
    monkeypatch.setattr(doctor, 'time', clock)
    monkeypatch.setattr(collector, 'time', clock)
    monkeypatch.delenv('UPS_NUT_TARGET', raising=False)
    monkeypatch.delenv('UPS_SERIAL', raising=False)
    host = {'system': 'Linux', 'kernel_release': '6.1.24-PRIVATE-HOST', 'python_version': '3.12.9',
            'python_supported': True, 'systemd_available': True, 'usbmon_module_loaded': True,
            'upsc_available': True}
    usb = {'discovery': 'found', 'vendor_id': '2b89', 'product_id': 'ffff',
           'manufacturer': 'PRIVATE-MANUFACTURER', 'product': 'PRIVATE-PRODUCT',
           'bcd_device': '0100', 'usbmon_node_exists': True, 'usbmon_readable': True}
    monkeypatch.setattr(doctor, 'host_metadata', lambda: copy.deepcopy(host))
    monkeypatch.setattr(doctor, 'usb_metadata', lambda *_: copy.deepcopy(usb))
    monkeypatch.setattr(doctor, 'discover', lambda **_: DEVICE.copy())
    commands = []

    def run(args, **kwargs):
        commands.append((args, kwargs))
        return SimpleNamespace(returncode=0, stderr='PRIVATE-ERROR', stdout='\n'.join([
            'ups.status: OL DISCHRG PRIVATE-HOST', 'ups.alarm: PRIVATE-ALARM',
            'ups.serial: PRIVATE-SERIAL', 'ups.vendorid: 2b89', 'ups.productid: ffff',
            'battery.runtime: 65535', 'driver.name: usbhid-ups', 'driver.version: 2.8.4-PRIVATE-HOST',
            'driver.version.data: Arduino HID 0.21', 'ups.firmware: 3.3_1.2',
            'driver.parameter.password: PRIVATE-PASSWORD',
        ]))

    monkeypatch.setattr(collector.subprocess, 'run', run)
    def no_reader(*_):
        raise AssertionError('Default diagnostics must not open usbmon')
    monkeypatch.setattr(doctor, 'Reader', no_reader)
    settings = tmp_path / 'collector.env'
    settings.write_text('UPS_NUT_TARGET=PRIVATE-TARGET@localhost\nUPS_SERIAL=PRIVATE-SERIAL\n')
    snapshot = tmp_path / 'snapshot.json'
    snapshot.write_text(json.dumps(live_snapshot()))
    return SimpleNamespace(clock=clock, host=host, usb=usb, commands=commands,
                           settings=settings, snapshot=snapshot)


def test_default_doctor_uses_existing_snapshot_and_only_readonly_nut_query(environment):
    report = doctor.diagnose(environment.snapshot, settings_path=environment.settings)
    assert report['read_only'] is True
    assert report['private_telemetry']['status'] == 'fresh'
    assert report['private_telemetry']['device_association'] == 'matched'
    assert report['collector_build'] is None and report['capture'] is None  # Legacy snapshot.
    assert report['observation']['status'] == 'not_requested'
    assert report['host']['kernel_version'] == '6.1.24'
    assert report['usb']['bcd_device'] == '0100'  # Raw USB device version, never a firmware conversion.
    assert report['nut']['ups_firmware'] == '3.3_1.2'
    assert report['nut']['pollonly'] == 'unknown'
    assert report['nut']['association'] == 'matched'
    assert report['nut']['status_tokens'] == ['DISCHRG', 'OL']
    assert report['nut']['alarm_present'] is True
    assert report['nut']['runtime_is_65535'] is True
    assert report['checks']['private_report'] == 'fresh'
    encoded = json.dumps(report, allow_nan=False)
    assert 'PRIVATE-' not in encoded
    assert 'frame_hex' not in encoded and FRAME.hex() not in encoded
    assert 'target' not in encoded and 'serial' not in encoded
    assert 'ups.alarm' not in encoded and 'values' not in report['nut']
    assert environment.commands == [([collector.NUT_UPSC, 'PRIVATE-TARGET@localhost'],
                                     {'capture_output': True, 'text': True, 'timeout': 2})]


def test_opt_in_observation_prefers_a_fresh_snapshot(environment):
    report = doctor.diagnose(environment.snapshot, observe_seconds=60, settings_path=environment.settings)
    assert report['observation']['status'] == 'used_existing_snapshot'
    assert report['private_telemetry']['source'] == 'snapshot'


def test_nut_failure_is_independent_of_private_capture(environment, monkeypatch):
    monkeypatch.setattr(doctor, 'nut_snapshot', lambda *_: {
        'available': False, 'timestamp': environment.clock.time(), 'error_code': 'query_failed'})
    report = doctor.diagnose(environment.snapshot, settings_path=environment.settings)
    assert report['checks']['private_report'] == 'fresh'
    assert report['checks']['nut_query'] == 'unavailable'
    assert report['nut']['error_code'] == 'query_failed'


@pytest.mark.parametrize('change,status,reason', [
    ({'schema': True}, 'invalid', 'snapshot_schema_invalid'),
    ({'source': 'replay'}, 'not_live', 'snapshot_not_usbmon'),
    ({'sample': None}, 'missing', 'snapshot_has_no_sample'),
    ({'heartbeat': 480}, 'stale', 'snapshot_stale'),
    ({'heartbeat': 501}, 'stale', 'snapshot_future_timestamp'),
    ({'device': {**DEVICE, 'serial': 'OTHER'}}, 'device_mismatch', 'snapshot_device_changed'),
])
def test_private_snapshot_failures_are_distinct(change, status, reason):
    result = doctor.private_status({**live_snapshot(), **change}, DEVICE, 500)
    assert result['status'] == status and result['reason'] == reason


@pytest.mark.parametrize('sample_change', [
    {'timestamp': float('nan')}, {'timestamp': True}, {'timestamp': 10 ** 400},
    {'soc': float('inf')}, {'cells': [4.0] * 3}, {'mode': {'PRIVATE': 'DATA'}},
])
def test_malformed_samples_do_not_become_fresh(sample_change):
    snapshot = live_snapshot()
    snapshot['sample'].update(sample_change)
    result = doctor.private_status(snapshot, DEVICE, 500)
    assert result['status'] == 'invalid'
    assert 'PRIVATE' not in json.dumps(result, allow_nan=False)


def test_passive_observation_is_bounded_and_never_exports_unrelated_payload(environment, monkeypatch):
    environment.snapshot.write_text(json.dumps(live_snapshot(480)))
    readers = []

    class Reader:
        def __init__(self, bus):
            assert bus == 3
            readers.append(self)
            self.closed = False
            self.reads = 0

        def read(self, timeout):
            environment.clock.sleep(timeout)
            self.reads += 1
            return packet(environment.clock.time(), device=9 if self.reads == 1 else 2)

        def close(self):
            self.closed = True

    monkeypatch.setattr(doctor, 'Reader', Reader)
    report = doctor.diagnose(environment.snapshot, observe_seconds=2, settings_path=environment.settings)
    assert len(readers) == 1 and readers[0].closed and readers[0].reads == 4
    assert report['observation'] == {'status': 'completed', 'duration_sec': 2.0, 'code': None}
    assert report['private_telemetry']['status'] == 'fresh'
    assert report['private_telemetry']['source'] == 'passive_observation'
    assert report['capture']['counters']['target_events'] == 3
    assert report['capture']['counters']['accepted_reports'] == 3
    encoded = json.dumps(report, allow_nan=False)
    assert 'PRIVATE-' not in encoded and FRAME.hex() not in encoded
    assert len(environment.commands) == 1  # No probing/second driver/reconfiguration commands.


def test_missing_node_does_not_trigger_modprobe_or_reader(environment):
    environment.snapshot.unlink()
    environment.usb['usbmon_node_exists'] = False
    environment.usb['usbmon_readable'] = False
    report = doctor.diagnose(environment.snapshot, observe_seconds=2, settings_path=environment.settings)
    assert report['observation']['status'] == 'unavailable'
    assert report['observation']['code'] == 'usbmon_not_ready'
    assert report['private_telemetry']['status'] == 'missing'
    assert len(environment.commands) == 1


def test_ambiguous_usb_is_reported_without_selecting_a_device(environment, monkeypatch):
    def ambiguous(**_):
        raise RuntimeError('PRIVATE USB identities')
    monkeypatch.setattr(doctor, 'discover', ambiguous)
    report = doctor.diagnose(environment.snapshot, observe_seconds=1, settings_path=environment.settings)
    assert report['usb']['discovery'] == 'ambiguous'
    assert report['private_telemetry']['device_association'] == 'unverified'
    assert report['nut']['association'] == 'unverified'
    assert 'PRIVATE' not in json.dumps(report)


@pytest.mark.parametrize('value,expected', [
    ('6.1.24-PRIVATE-HOST', '6.1.24'), ('3.3_1.2', '3.3_1.2'), ('2.8.4', '2.8.4'),
    ('192.0.2.7', None), ('192.0.2.7-PRIVATE-HOST', None), ('2001:db8::1', None),
    ('2001:db8::1-PRIVATE-HOST', None), ('[2001:db8::1]', None), ('PRIVATE-HOST', None),
])
def test_safe_versions_do_not_export_addresses_or_host_suffixes(value, expected):
    assert doctor.numeric_version(value) == expected


def test_malformed_optional_metadata_is_ignored_and_ip_versions_are_redacted(environment):
    snapshot = live_snapshot()
    snapshot['collector'] = {'build': {'version': '192.0.2.7', 'revision': 'PRIVATE-TOKEN',
                                      'source_sha256': 'PRIVATE-PATH'}}
    snapshot['diagnostics'] = {'capture': {'schema': 1, 'state': ['PRIVATE'], 'counters': {
        'target_events': True, 'accepted_reports': -1, 'not_interrupt_in': 10 ** 100,
        'private_data': 'PRIVATE-TOKEN'}, 'recent_counters': 'PRIVATE'}}
    environment.snapshot.write_text(json.dumps(snapshot))
    report = doctor.diagnose(environment.snapshot, settings_path=environment.settings)
    assert report['collector_build'] == {'version': None, 'revision': None, 'source_sha256': None}
    assert not any(report['capture']['counters'].values())
    assert 'PRIVATE' not in json.dumps(report)
    result = doctor.safe_nut({'error_code': [], 'timestamp': float('inf'), 'pollonly': {'state': {}},
                             'association': {'status': []}, 'values': {'driver.version': '192.0.2.7'}}, 500)
    assert result['driver_version'] is None and result['query_age_sec'] is None
    assert result['association'] == 'unverified' and result['pollonly'] == 'unknown'


def test_settings_are_filtered_without_shell_evaluation(tmp_path):
    path = tmp_path / 'settings'
    path.write_text('UPS_NUT_TARGET="ups@localhost"\nUPS_SERIAL="PRIVATE-SERIAL"\n'
                    'PASSWORD=PRIVATE-PASSWORD\nEXEC=$(touch /PRIVATE-PATH)\n'
                    'UPS_CALIBRATION_CONFIG=/PRIVATE-PATH\n')
    assert doctor.read_settings(path) == {'UPS_NUT_TARGET': 'ups@localhost', 'UPS_SERIAL': 'PRIVATE-SERIAL'}


@pytest.mark.parametrize('content,reason', [
    ('PRIVATE-PASSWORD', 'snapshot_unreadable_or_invalid'),
    ('{"schema":1,"private":NaN}', 'snapshot_unreadable_or_invalid'),
    ('["PRIVATE"]', 'snapshot_invalid'),
    ('x' * (doctor.SNAPSHOT_LIMIT + 1), 'snapshot_too_large'),
])
def test_bad_snapshot_returns_only_a_fixed_reason(tmp_path, content, reason):
    path = tmp_path / 'private-path'
    path.write_text(content)
    assert doctor.read_snapshot(path) == (None, reason)


@pytest.mark.parametrize('duration', [-1, 60.1, float('nan'), float('inf'), True])
def test_doctor_rejects_unbounded_observation_before_any_io(duration, monkeypatch):
    monkeypatch.setattr(doctor, 'read_snapshot', lambda *_: pytest.fail('No reads expected'))
    with pytest.raises(ValueError, match='between 0 and 60'):
        doctor.diagnose(observe_seconds=duration)


def test_cli_defaults_to_json_and_does_not_add_mutating_commands(monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(doctor, 'diagnose', lambda *args: calls.append(args) or {'schema': 1, 'read_only': True})
    assert doctor.main([]) == 0
    assert json.loads(capsys.readouterr().out) == {'schema': 1, 'read_only': True}
    assert calls[0] == (Path('/run/ugreen-ups-panel/latest.json'), None, None, 0)
    with pytest.raises(SystemExit) as exc:
        doctor.main(['--observe-seconds', 'nan'])
    assert exc.value.code == 2
    assert len(calls) == 1
