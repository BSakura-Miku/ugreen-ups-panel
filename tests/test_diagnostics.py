import csv
from copy import deepcopy
import io
import json
from pathlib import Path

import pytest

from ups_panel.build_info import get_build_info
from ups_panel.diagnostics import (diagnostic_export, diagnostic_view, export_point,
                                   observation_csv, safe_version, system_view)
from ups_panel.protocol import parse_frame
from ups_panel.raw_observation import RawObservationMonitor

FRAME = bytes.fromhex((Path(__file__).parents[1] / 'fixtures/online.hex').read_text().splitlines()[0])


def snapshot(now=100):
    return {'schema': 1, 'heartbeat': now, 'fresh': True, 'source': 'usbmon',
            'device': {'bus': 3, 'device': 2, 'serial': 'PRIVATE_SERIAL', 'path': '/private/usb/device'},
            'sample': parse_frame(FRAME, now),
            'nut': {'available': True, 'timestamp': now, 'target': 'private@192.0.2.7',
                    'values': {'ups.status': 'OL DISCHRG', 'battery.runtime': '65535'}},
            'collector': {'build': {'version': '0.8.0', 'revision': None, 'source_sha256': 'a' * 64},
                          'host': {'system': 'Linux', 'python_supported': True},
                          'usb': {'discovery': 'found', 'usbmon_readable': True, 'bcd_device': '0200'}}}


def render(value, now=100):
    monitor = RawObservationMonitor()
    monitor.ingest(value, now=now)
    return diagnostic_view(value, monitor.snapshot(value, now=now), now=now)


@pytest.mark.parametrize('raw,quality,seconds', [
    ('65535', 'sentinel', None), ('4294967295', 'sentinel', None), ('-1', 'sentinel', None),
    ('3600', 'unverified', 3600), ('0', 'unverified', 0), ('abc', 'invalid', None),
    ('3600.5', 'invalid', None), ('=HYPERLINK("private")', 'invalid', None),
])
def test_runtime_quality_does_not_convert_sentinels_or_assume_accuracy(raw, quality, seconds):
    value = snapshot()
    value['nut']['values']['battery.runtime'] = raw
    runtime = system_view(value, 100)['runtime']
    assert runtime['raw'] == raw and runtime['quality'] == quality and runtime['seconds'] == seconds


def test_system_notices_are_fresh_reported_facts_and_keep_thresholds_separate():
    value = snapshot()
    value['nut']['values'].update({'ups.status': 'OL DISCHRG LB RB OVER ALARM FSD OFF PRIVATE_TOKEN',
                                  'ups.alarm': 'replace battery', 'battery.charge.low': '20',
                                  'battery.runtime.low': '300'})
    system = system_view(value, 100)
    assert system['status_tokens'] == ['OL', 'DISCHRG', 'LB', 'RB', 'OVER', 'ALARM', 'FSD', 'OFF']
    assert {item['code'] for item in system['notices']} == {'LB', 'RB', 'OVER', 'ALARM', 'FSD', 'OFF', 'online_discharge'}
    assert all('系统' in item['label'] for item in system['notices'])
    assert system['thresholds'] == {'charge_low': 20, 'runtime_low_sec': 300}
    expired = system_view(value, 146)
    assert not expired['fresh'] and expired['notices'] == [] and expired['alarm_text'] is None
    assert expired['runtime']['quality'] == 'stale' and expired['runtime']['seconds'] is None
    assert all(item is None for item in expired['thresholds'].values())
    assert expired['values']['ups.status'] == system['values']['ups.status']


def test_missing_metadata_is_unknown_and_nut_success_does_not_prove_usb_reports():
    value = snapshot()
    value.pop('collector')
    value['fresh'] = False
    value['sample'] = None
    result = render(value)
    checks = {item['id']: item for item in result['connection']['checks']}
    assert checks['nut']['status'] == 'ok'
    assert checks['private_report']['status'] == 'waiting'
    assert checks['host']['status'] == checks['usbmon']['status'] == 'unknown'
    assert result['versions']['collector'] is None and result['connection']['pollonly'] is None
    assert result['connection']['association'] == 'unverified'


def test_pollonly_and_capture_reason_are_reported_without_fabricating_settings():
    value = snapshot()
    value['fresh'] = False
    value['nut']['pollonly'] = {'state': 'enabled'}
    value['nut']['association'] = {'status': 'different'}
    value['diagnostics'] = {'capture': {'state': 'no_target_activity',
                                        'recent_counters': {'target_events': 0, 'serial': 1, 'invalid_length': -1}}}
    result = render(value)
    checks = {item['id']: item for item in result['connection']['checks']}
    assert '没有目标设备活动' in checks['private_report']['detail']
    assert 'pollonly' in checks['private_report']['detail']
    assert checks['association']['status'] == 'warning'
    assert result['connection']['counters'] == {'target_events': 0}


@pytest.mark.parametrize('field,bad', [('collector', []), ('collector', {'host': [], 'usb': []}),
                                    ('diagnostics', {'capture': {'state': []}}),
                                    ('nut', {'available': True, 'timestamp': 10 ** 400, 'values': {'ups.status': []}})])
def test_optional_malformed_metadata_does_not_break_diagnostics(field, bad):
    value = snapshot()
    value[field] = bad
    json.dumps(render(value), allow_nan=False)


def test_export_is_allowlisted_and_drops_free_text_and_identifiers():
    value = snapshot()
    secret = 'PRIVATE_PAYLOAD'
    value['nut']['values'].update({'ups.alarm': secret + ' 192.0.2.7', 'ups.serial': secret,
                                  'device.mfr': secret, 'device.model': secret, 'ups.status': 'OL ' + secret,
                                  'driver.name': secret, 'driver.version': '192.0.2.7',
                                  'driver.version.data': secret, 'ups.firmware': secret,
                                  'battery.charge': '98', 'battery.runtime.low': secret})
    value['collector']['build']['revision'] = secret
    value['collector']['host']['kernel_release'] = secret
    value['collector']['usb']['manufacturer'] = secret
    result = render(value)
    assert secret in json.dumps(result)
    exported = diagnostic_export(result)
    encoded = json.dumps(exported, ensure_ascii=False, allow_nan=False)
    for forbidden in (secret, 'PRIVATE_SERIAL', 'private@', '192.0.2.7', '/private/usb/device', FRAME.hex()):
        assert forbidden not in encoded
    assert exported['system']['values']['battery.charge'] == '98'
    assert exported['system']['status_tokens'] == ['OL']
    assert exported['system']['free_text_alarm_present'] is True
    assert exported['versions']['nut_version'] is None
    assert exported['versions']['collector']['revision'] is None
    assert exported['observation']['count'] == 1
    assert exported['observation']['points'][0]['device_alias'].startswith('device-')
    assert set(exported['observation']['points'][0]) == set(export_point({}))
    csv_text = observation_csv(result)
    assert secret not in csv_text and 'PRIVATE_SERIAL' not in csv_text
    rows = list(csv.DictReader(io.StringIO(csv_text)))
    assert len(rows) == 1 and rows[0]['byte_26'].isdigit()


def test_csv_neutralizes_unexpected_point_strings_and_preserves_missing_values():
    result = render(snapshot())
    point = result['observation']['points'][0]
    for key in point:
        point[key] = '=HYPERLINK("https://example.invalid/private")'
    text = observation_csv(result)
    assert 'HYPERLINK' not in text and 'example.invalid' not in text
    assert all(value == '' for value in next(csv.DictReader(io.StringIO(text))).values())


@pytest.mark.parametrize('value', ['192.0.2.7', 'v192.0.2.7', 'v192.0.2.7-rc1', '1.0+nas.local', '3.3-home.example.com',
                                  'private-host.local', '0.8.0 private', '=1+1', None, []])
def test_safe_version_rejects_addresses_and_arbitrary_text(value):
    assert safe_version(value) is None


@pytest.mark.parametrize('value', ['2.8.0-rc1', '3.3_1.2', '0.8.0', 'v0.8.0', '0200'])
def test_known_version_shapes_remain_exportable(value):
    assert safe_version(value) == value


def test_replay_with_legacy_device_metadata_never_confirms_physical_usb():
    value = snapshot()
    value['source'] = 'replay'
    value.pop('collector')
    checks = {item['id']: item for item in render(value)['connection']['checks']}
    assert checks['usb']['status'] == 'warning' and '演示' in checks['usb']['detail']
    assert checks['private_report']['status'] == 'warning'


def test_build_info_identifies_source_without_git_or_host_details(monkeypatch):
    monkeypatch.setenv('UPS_BUILD_REVISION', 'd' * 40)
    build = get_build_info()
    assert build['version'] == '0.11.0' and build['revision'] == 'd' * 40
    assert len(build['source_sha256']) == 64
    assert set(build) == {'version', 'revision', 'source_sha256'}
    monkeypatch.setenv('UPS_BUILD_REVISION', 'private@192.0.2.7')
    assert get_build_info()['revision'] is None
