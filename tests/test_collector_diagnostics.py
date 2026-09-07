import copy
import json
from pathlib import Path
import struct
import subprocess
from types import SimpleNamespace

import pytest

from ups_panel import collector, usbmon

ROOT = Path(__file__).parents[1]
FRAME = bytes.fromhex((ROOT / 'fixtures/online.hex').read_text().splitlines()[0])
DEVICE = {'bus': 3, 'device': 2, 'path': '3-4', 'serial': 'PRIVATE-SERIAL'}


def header(timestamp=100, *, status=-2, length=64, captured=64, endpoint=0x81,
           transfer=1, kind='C', bus=3, device=2, missing=0):
    value = bytearray(64)
    value[8:12] = bytes([ord(kind), transfer, endpoint, device])
    value[15] = missing
    struct.pack_into('=H', value, 12, bus)
    struct.pack_into('=q', value, 16, int(timestamp))
    struct.pack_into('=i', value, 24, int((timestamp - int(timestamp)) * 1_000_000))
    struct.pack_into('=iII', value, 28, status, length, captured)
    return bytes(value)


class Clock:
    now = 100.0

    def time(self):
        return self.now

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now = round(self.now + seconds, 6)


@pytest.mark.parametrize('options,payload,reason', [
    ({}, FRAME, 'accepted'),
    ({'status': 0}, FRAME, 'accepted'),
    ({'length': 128, 'captured': 128}, FRAME * 2, 'accepted'),
    ({'bus': 4}, FRAME, 'unrelated'),
    ({'device': 3}, FRAME, 'unrelated'),
    ({'kind': 'S'}, FRAME, 'not_completion'),
    ({'transfer': 2}, FRAME, 'not_interrupt_in'),
    ({'endpoint': 0x82}, FRAME, 'not_interrupt_in'),
    ({'endpoint': 1}, FRAME, 'not_interrupt_in'),
    ({'status': -32}, FRAME, 'invalid_status'),
    ({'missing': 1}, FRAME, 'missing_payload'),
    ({'length': 128, 'captured': 64}, FRAME, 'invalid_length'),
    ({'length': 63, 'captured': 63}, FRAME[:63], 'invalid_length'),
    ({'length': 128, 'captured': 128}, FRAME + b'\0' * 64, 'invalid_report_id'),
    ({}, b'\0' * 64, 'invalid_report_id'),
])
def test_classification_preserves_transport_acceptance(options, payload, reason):
    packet = header(**options)
    assert usbmon.classify_event(packet, payload, 3, 2) == reason
    decoded = usbmon.decode_event(packet, payload, 3, 2)
    assert (decoded is not None) == (reason == 'accepted')
    if decoded:
        assert decoded['frame'] == FRAME
        assert decoded['timestamp'] == 100
        assert decoded['usb_status'] in (0, -2)


def test_other_devices_and_unattributable_headers_do_not_touch_payload_or_counters(monkeypatch):
    class PrivatePayload:
        def __len__(self):
            raise AssertionError('An unrelated payload was inspected')

    clock = Clock()
    monkeypatch.setattr(collector, 'time', clock)
    capture = collector.CaptureDiagnostics()
    assert capture.observe(header(bus=4), PrivatePayload(), 3, 2) == 'unrelated'
    assert capture.observe(b'\0' * 63, PrivatePayload(), 3, 2) == 'invalid_header'
    assert capture.snapshot()['state'] == 'waiting'
    clock.now += 11
    state = capture.snapshot()
    assert state['state'] == 'no_target_activity'
    assert not any(state['counters'].values())
    assert state['last_target_seen_at'] is None
    assert len(capture.buckets) == 0


def test_recent_diagnostics_expire_and_are_bounded(monkeypatch):
    clock = Clock()
    monkeypatch.setattr(collector, 'time', clock)
    capture = collector.CaptureDiagnostics()
    for _ in range(180):
        capture.observe(header(transfer=2), FRAME, 3, 2)
        clock.now += 1
    state = capture.snapshot()
    assert state['state'] == 'no_interrupt_in'
    assert state['counters']['target_events'] == 180
    assert 0 < state['recent_counters']['target_events'] <= 60
    assert len(capture.buckets) <= 60
    capture.counters['target_events'] = collector.COUNTER_MAX
    capture.observe(header(), FRAME, 3, 2)
    assert capture.counters['target_events'] == collector.COUNTER_MAX
    capture.valid_sample(clock.now)
    assert capture.snapshot()['state'] == 'fresh'
    clock.now += 61
    state = capture.snapshot()
    assert state['state'] == 'stale'
    assert not any(state['recent_counters'].values())
    assert len(capture.buckets) == 0
    assert capture.snapshot(available=False)['state'] == 'unavailable'
    assert capture.snapshot(available=False, replay=True)['state'] == 'replay'


def test_invalid_transport_and_invalid_sample_are_distinct(monkeypatch):
    clock = Clock()
    monkeypatch.setattr(collector, 'time', clock)
    capture = collector.CaptureDiagnostics()
    capture.observe(header(length=128), FRAME, 3, 2)
    assert capture.snapshot()['state'] == 'no_complete_report'
    capture.observe(header(), FRAME, 3, 2)
    capture.count('invalid_sample')
    assert capture.snapshot()['state'] == 'invalid_reports'
    capture.valid_sample(clock.now)
    assert capture.snapshot()['state'] == 'fresh'
    clock.now -= 1
    assert capture.snapshot()['state'] != 'fresh'
    assert 'frame' not in json.dumps(capture.snapshot())


def test_nut_whitelist_query_time_and_private_identity(monkeypatch):
    clock = Clock()
    calls = []
    response = '\n'.join([
        'ups.status: OL DISCHRG', 'ups.alarm: ' + 'x' * 300 + '\t',
        'battery.charge.low: 20', 'battery.runtime.low: 120', 'battery.runtime: 65535',
        'driver.name: usbhid-ups', 'driver.version: 2.8.4', 'driver.version.data: Arduino HID 0.21',
        'driver.version.internal: 0.67', 'driver.version.usb: libusb-1.0.29',
        'driver.parameter.subdriver: Arduino', 'driver.flag.pollonly: enabled',
        'ups.firmware: 3.3_1.2', 'ups.firmware.aux: 1.2',
        'device.mfr: UGREEN', 'device.model: US3000',
        'device.serial: PRIVATE-SERIAL', 'ups.serial: PRIVATE-SERIAL',
        'ups.vendorid: 2b89', 'ups.productid: ffff',
        'driver.parameter.password: NEVER_EXPORT_PASSWORD', 'unknown: NEVER_EXPORT_UNKNOWN',
    ])

    def run(args, **kwargs):
        calls.append((args, kwargs))
        clock.now += .25
        return SimpleNamespace(returncode=0, stdout=response, stderr='NEVER_EXPORT_STDERR')

    monkeypatch.setattr(collector, 'time', clock)
    monkeypatch.setattr(collector.subprocess, 'run', run)
    result = collector.nut_snapshot('ups@localhost', DEVICE)
    assert result['timestamp'] == 100.25
    assert result['timestamp_kind'] == 'query_completed'
    assert result['target'] == 'ups@localhost'
    assert result['association'] == {'status': 'matched', 'reason': 'local_serial_and_usb_ids'}
    assert result['pollonly'] == {'state': 'enabled', 'source': 'nut_reported_flag'}
    assert result['values']['ups.firmware'] == '3.3_1.2'
    assert result['values']['driver.version.internal'] == '0.67'
    assert len(result['values']['ups.alarm']) == collector.NUT_VALUE_LIMIT
    assert all(len(value) <= collector.NUT_VALUE_LIMIT for value in result['values'].values())
    assert set(result['values']) <= collector.NUT_ALLOWED
    encoded = json.dumps(result)
    assert all(secret not in encoded for secret in ('PRIVATE-SERIAL', 'NEVER_EXPORT_PASSWORD',
                                                     'NEVER_EXPORT_UNKNOWN', 'NEVER_EXPORT_STDERR'))
    assert calls == [([collector.NUT_UPSC, 'ups@localhost'], {'capture_output': True, 'text': True, 'timeout': 2})]


@pytest.mark.parametrize('raw,state,source', [
    (None, 'unknown', 'not_reported'), ('enabled', 'enabled', 'nut_reported_flag'),
    ('1', 'enabled', 'nut_reported_flag'), ('disabled', 'disabled', 'nut_reported_flag'),
    ('0', 'disabled', 'nut_reported_flag'), ('unexpected', 'unknown', 'nut_reported_flag'),
])
def test_pollonly_is_not_inferred_from_missing_configuration(raw, state, source):
    values = {} if raw is None else {'driver.flag.pollonly': raw}
    assert collector.pollonly_fact(values) == {'state': state, 'source': source}


@pytest.mark.parametrize('extra,target,expected', [
    ({}, 'ups@localhost', 'matched'),
    ({}, 'ups@[::1]:3493', 'matched'),
    ({}, 'ups@nas.local', 'unverified'),
    ({'ups.serial': 'ANOTHER-SERIAL'}, 'ups@localhost', 'different'),
    ({'device.serial': 'ANOTHER-SERIAL'}, 'ups@localhost', 'unverified'),
    ({'ups.vendorid': '1234'}, 'ups@localhost', 'different'),
    ({'ups.productid': ''}, 'ups@localhost', 'unverified'),
    ({'ups.serial': 'PRIVATE-SERIAL' + 'x' * 300}, 'ups@localhost', 'unverified'),
])
def test_nut_association_does_not_guess_or_match_truncated_identifiers(extra, target, expected):
    values = {'ups.serial': DEVICE['serial'], 'ups.vendorid': '2b89', 'ups.productid': 'ffff', **extra}
    association = collector.nut_association(values, target, DEVICE)
    assert association['status'] == expected
    assert DEVICE['serial'] not in json.dumps(association)


@pytest.mark.parametrize('failure,code', [
    (FileNotFoundError('PRIVATE-PATH'), 'upsc_missing'),
    (OSError('PRIVATE-PATH'), 'query_unavailable'),
    (subprocess.TimeoutExpired('SECRET-COMMAND', 2), 'query_timeout'),
])
def test_nut_failures_are_codes_without_external_messages(monkeypatch, failure, code):
    def fail(*_args, **_kwargs):
        raise failure
    monkeypatch.setattr(collector.subprocess, 'run', fail)
    result = collector.nut_snapshot('ups@localhost')
    assert not result['available'] and result['error_code'] == code
    assert all(secret not in json.dumps(result) for secret in ('PRIVATE-PATH', 'SECRET-COMMAND'))


def test_nut_rejects_argument_injection_and_oversized_output(monkeypatch):
    calls = []
    monkeypatch.setattr(collector.subprocess, 'run', lambda *a, **kw: calls.append(a))
    assert collector.nut_snapshot('-l')['error_code'] == 'invalid_target'
    assert collector.nut_snapshot('ups@localhost\npassword')['error_code'] == 'invalid_target'
    assert not calls
    monkeypatch.setattr(collector.subprocess, 'run', lambda *a, **kw: SimpleNamespace(
        returncode=0, stdout='x' * (collector.NUT_RESPONSE_LIMIT + 1)))
    assert collector.nut_snapshot('ups')['error_code'] == 'response_too_large'


def test_sysfs_descriptors_do_not_change_capture_identity_or_reset_reader(tmp_path, monkeypatch):
    root = tmp_path / 'sysfs'
    directory = root / DEVICE['path']
    directory.mkdir(parents=True)
    descriptors = {'idVendor': '2b89', 'idProduct': 'ffff', 'busnum': '3', 'devnum': '2',
                   'serial': DEVICE['serial'], 'manufacturer': 'UGREEN', 'product': 'US3000', 'bcdDevice': '0100'}
    for name, value in descriptors.items():
        (directory / name).write_text(value)
    before = usbmon.discover(root)
    assert before == DEVICE
    initial_metadata = collector.usb_metadata(before, root=root, dev_root=tmp_path)
    assert initial_metadata['bcd_device'] == '0100'
    (directory / 'product').write_text('US3000 revised descriptor')
    assert usbmon.discover(root) == before
    assert collector.usb_metadata(before, root=root, dev_root=tmp_path)['product'] != initial_metadata['product']

    clock = Clock()
    snapshots, readers = [], []
    metadata_reader = collector.usb_metadata

    class Reader:
        def __init__(self, bus):
            assert bus == 3
            readers.append(self)
            self.closed = False

        def read(self, timeout):
            clock.sleep(timeout)
            (directory / 'product').write_text(f'Display-only description {clock.now}')
            return header(clock.now), FRAME

        def stats(self):
            return 0, 0

        def close(self):
            self.closed = True

    monkeypatch.setattr(collector, 'time', clock)
    monkeypatch.setattr(collector.signal, 'signal', lambda *_: None)
    monkeypatch.setattr(collector, 'Reader', Reader)
    monkeypatch.setattr(collector, 'discover', lambda serial='': usbmon.discover(root, serial))
    monkeypatch.setattr(collector, 'usb_metadata', lambda device, discovery='found': metadata_reader(
        device, discovery, root=root, dev_root=tmp_path))
    monkeypatch.setattr(collector, 'nut_snapshot', lambda *_: {'available': False})
    monkeypatch.setattr(collector, 'atomic_json', lambda path, data: snapshots.append(copy.deepcopy(data)))
    collector.run(tmp_path / 'unused.json', duration=12)
    assert len(readers) == 1 and readers[0].closed
    assert all(item['device'] == DEVICE for item in snapshots)
    assert len({item['collector']['usb']['product'] for item in snapshots}) > 1
    assert snapshots[-1]['diagnostics']['capture']['counters']['target_events'] >= 20
    assert snapshots[-1]['diagnostics']['capture']['state'] == 'fresh'


@pytest.mark.parametrize('failure,discovery', [(RuntimeError('Ambiguous'), 'ambiguous'),
                                               (OSError('Unavailable'), 'unavailable')])
def test_discovery_loss_clears_previously_fresh_identity_and_association(tmp_path, monkeypatch, failure, discovery):
    clock = Clock()
    snapshots, readers = [], []

    def find(**_):
        if clock.now >= 103:
            raise failure
        return DEVICE.copy()

    class Reader:
        def __init__(self, _):
            readers.append(self)
            self.closed = False

        def read(self, timeout):
            clock.sleep(timeout)
            return header(clock.now), FRAME

        def stats(self):
            return 0, 0

        def close(self):
            self.closed = True

    monkeypatch.setattr(collector, 'time', clock)
    monkeypatch.setattr(collector.signal, 'signal', lambda *_: None)
    monkeypatch.setattr(collector, 'Reader', Reader)
    monkeypatch.setattr(collector, 'discover', find)
    monkeypatch.setattr(collector, 'nut_snapshot', lambda *_: {
        'available': True, 'association': {'status': 'matched', 'reason': 'local_serial_and_usb_ids'}})
    monkeypatch.setattr(collector, 'atomic_json', lambda path, data: snapshots.append(copy.deepcopy(data)))
    collector.run(tmp_path / 'unused.json', duration=6)
    assert any(item['sample'] is not None for item in snapshots)
    failed = [item for item in snapshots if item['collector']['usb']['discovery'] == discovery]
    assert failed
    for item in failed:
        assert item['device'] is None and item['sample'] is None
        assert item['nut']['association']['status'] == 'unverified'
        assert item['diagnostics']['capture']['state'] == 'unavailable'
        assert item['diagnostics']['capture']['last_valid_sample_at'] is None
    assert all(reader.closed for reader in readers)
