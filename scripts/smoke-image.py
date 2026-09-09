#!/usr/bin/env python3
"""Smoke-test a locally built image without a UPS, NUT, or external network.

Usage: python3 scripts/smoke-image.py --image us3000-panel:test [--platform linux/amd64]

Only Python's standard library and the Docker CLI are required on the host. The
image must already exist locally: this script never builds or pulls an image.
All HTTP checks run inside an isolated container against its own loopback port.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import threading
import time
from urllib.parse import urlsplit
import uuid


LABEL = 'io.us3000.image-smoke-token'


class SmokeError(RuntimeError):
    pass


def docker(*arguments: str, timeout: float = 30, check: bool = True):
    try:
        result = subprocess.run(
            ['docker', *arguments], text=True, capture_output=True, timeout=timeout,
        )
    except FileNotFoundError as exc:
        raise SmokeError('Docker CLI is not installed or is not on PATH.') from exc
    except subprocess.TimeoutExpired as exc:
        raise SmokeError(f'Docker {arguments[0]} timed out after {timeout:g}s.') from exc
    if check and result.returncode:
        detail = (result.stderr or result.stdout).strip()[-3000:]
        raise SmokeError(f'Docker {arguments[0]} failed: {detail}')
    return result


def require_local_daemon():
    """Do not accidentally run a bind-mount test on a saved remote NAS context."""
    if os.environ.get('DOCKER_HOST') and not os.environ.get('DOCKER_CONTEXT'):
        endpoint = os.environ['DOCKER_HOST']
    else:
        context = docker('context', 'show').stdout.strip()
        metadata = json.loads(docker('context', 'inspect', context).stdout)[0]
        endpoint = metadata['Endpoints']['docker']['Host']
    parsed = urlsplit(endpoint)
    local = parsed.scheme in ('unix', 'npipe') or (
        parsed.scheme in ('tcp', 'http', 'https')
        and parsed.hostname in ('localhost', '127.0.0.1', '::1')
    )
    if not local:
        raise SmokeError('Select a local Docker context; remote engines are not used by this smoke test.')


def snapshot(timestamp: float) -> dict:
    """Synthetic UI/storage fixture; no household telemetry or device identity."""
    config = {'schema': 1, 'profile': 'none', 'coefficients': None}
    config['revision'] = hashlib.sha256(json.dumps(config, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    return {
        'schema': 1, 'heartbeat': timestamp, 'source': 'replay',
        'calibration': {'config': config, 'configurable': False, 'error': None},
        'device': {'serial': 'SMOKE-TEST'},
        'nut': {'available': False, 'timestamp': 0, 'values': {}},
        'diagnostics': {'frames': 1, 'dropped': 0, 'rejected': 0},
        'sample': {
            'timestamp': timestamp, 'mode': 'online', 'raw_status': 38,
            'soc': 98, 'battery_voltage': 16.412,
            'cells': [4.103, 4.104, 4.102, 4.103], 'cell_delta_mv': 2,
            'input_voltage': 19.01, 'adapter_input_voltage_v': 19.01,
            'output_voltage': 18.99, 'ups_output_voltage_v': 18.99,
            'current': 2.0, 'current_kind': 'unverified',
            'power_w': 37.98, 'dc_power_estimate_w': 37.98,
            'power_verified': False, 'current_verified': False,
            'decoder_version': 4, 'formula_version': 2, 'warnings': [],
            'calibration_profile': 'none', 'calibration_verified': False,
            'calibration_revision': config['revision'], 'calibration_coefficients': None,
            'ac_input_estimate_w': None, 'ac_estimate_model': None,
            'ac_estimate_quality': 'not_configured', 'ac_estimate_window_sec': 8,
            'battery_energy_estimate_w': None, 'battery_estimate_basis': None,
            'battery_estimate_quality': 'not_configured',
            'battery_charge_current_candidate_a': None,
            'battery_discharge_current_candidate_a': None,
            'battery_charge_power_candidate_w': None,
            'battery_discharge_power_candidate_w': None,
            'raw_fields': {'be_u16': {'16': 19010, '18': 18990, '24': 2000},
                           'byte_26': 41, 'byte_27': 48, 'byte_28': 50,
                           'frame_hex': '71' + '42' * 63},
        },
    }


def write_snapshot(capture: Path, timestamp: float):
    write_capture(capture, snapshot(timestamp))


def write_capture(capture: Path, value: dict):
    temporary = capture / 'latest.json.new'
    temporary.write_text(json.dumps(value), encoding='utf-8')
    temporary.chmod(0o644)
    temporary.replace(capture / 'latest.json')


def refresh_snapshots(capture: Path, stop: threading.Event, errors: list[str]):
    while not stop.wait(1):
        try:
            write_snapshot(capture, time.time() - 0.2)
        except OSError as exc:
            errors.append(str(exc))
            return


def inspect_container(name: str) -> dict:
    return json.loads(docker('inspect', name).stdout)[0]


def check_configuration(container: dict):
    host = container['HostConfig']
    checks = {
        'non-root image user': container['Config']['User'] == '10001:10001',
        'read-only root filesystem': host['ReadonlyRootfs'],
        'unprivileged container': not host['Privileged'],
        'all capabilities dropped': 'ALL' in host.get('CapDrop', []),
        'no new privileges': 'no-new-privileges:true' in host.get('SecurityOpt', []),
        'isolated network': host['NetworkMode'] == 'none',
        'no published ports': not host.get('PortBindings'),
        'no attached devices': not host.get('Devices') and not host.get('DeviceRequests'),
    }
    mounts = {entry['Destination']: entry for entry in container['Mounts']}
    checks['read-only capture mount'] = '/capture' in mounts and not mounts['/capture']['RW']
    checks['writable history mount'] = '/data' in mounts and mounts['/data']['RW']
    failed = [description for description, passed in checks.items() if not passed]
    if failed:
        raise SmokeError('Unsafe or incorrect container configuration: ' + ', '.join(failed))


# Executed as the image's own default user; never invokes a collector or NUT.
PROBE = r'''
import csv, errno, io, json, os, platform, sqlite3, time
from html.parser import HTMLParser
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import ProxyHandler, build_opener

opener = build_opener(ProxyHandler({}))

def get(path):
    with opener.open('http://127.0.0.1:8080' + path, timeout=3) as response:
        return response.read(), {key.lower(): value for key, value in response.headers.items()}

def get_json(path):
    return json.loads(get(path)[0])

deadline = time.monotonic() + 45
while True:
    try:
        health = get_json('/api/health')
        if health.get('service') == 'ok' and health.get('capture_fresh') and not health.get('storage_error'):
            break
    except (URLError, TimeoutError, ConnectionError):
        pass
    assert time.monotonic() < deadline, 'Service did not become healthy with fresh capture'
    time.sleep(.5)

assert os.getuid() == 10001 and os.getgid() == 10001, 'Unexpected runtime UID/GID'
status = dict(line.split(':', 1) for line in Path('/proc/self/status').read_text().splitlines() if ':' in line)
assert int(status['CapEff'].strip(), 16) == 0, 'Effective capabilities were not dropped'
assert status['NoNewPrivs'].strip() == '1', 'NoNewPrivs is disabled'
pid1 = dict(line.split(':', 1) for line in Path('/proc/1/status').read_text().splitlines() if ':' in line)
assert all(int(value) == 10001 for value in pid1['Uid'].split()), 'Server runs as unexpected user'
for location in ('/app/.smoke-write', '/capture/.smoke-write'):
    try:
        Path(location).write_text('must not be writable')
    except OSError as exc:
        assert exc.errno in (errno.EROFS, errno.EACCES, errno.EPERM), str(exc)
    else:
        Path(location).unlink()
        raise AssertionError(location + ' unexpectedly writable')
data_probe = Path('/data/.smoke-write')
data_probe.write_text('temporary writable volume probe')
data_probe.unlink()

live_bytes, live_headers = get('/api/live')
live = json.loads(live_bytes)
assert live['fresh'] and live['source'] == 'replay'
assert live['device']['serial'] == 'SMOKE-TEST' and not live['nut']['available']
assert live['sample']['calibration_profile'] == 'none'
assert live['calibration_validation']['valid'], 'Complete calibration metadata was not accepted'
assert live['sample']['ac_input_estimate_w'] is None and live['sample']['battery_energy_estimate_w'] is None
assert live['sample']['ac_estimate_quality'] == 'not_configured'
assert live_headers.get('cache-control') == 'no-store', 'Live responses must disable caching'
balance = live['cell_balance']
assert balance['schema'] == 1 and balance['reference_only'] is True, 'Cell-balance API contract missing'
assert balance['required_standby_sec'] == 1800 and balance['persistence_sec'] == 120
assert balance['thresholds'] == {'good_below_mv': 20, 'minor_below_mv': 50, 'elevated_below_mv': 100}
assert balance['state'] in ('observing', 'settling') and balance['level'] is None
assert balance['delta_mv'] == 2 and balance['lowest_cells'] == [3]

# Check the actual image's default estimator as well as the fixture/API contract.
from ups_panel.power import PowerEstimator
for mode in ('online', 'charging', 'battery'):
    sample = PowerEstimator().update({'timestamp': time.time(), 'mode': mode})
    assert sample['calibration_profile'] == 'none'
    assert sample['ac_input_estimate_w'] is None and sample['battery_energy_estimate_w'] is None
    assert sample['ac_estimate_quality'] == sample['battery_estimate_quality'] == 'not_configured'
    assert 'calibration_schema' not in sample and 'ac_voltage_nominal_v' not in sample

def check_v2_calibration(directory):
    # Use the image's installed code with a separate disposable config. This does
    # not start USB/NUT collection or change the running API's default-none test.
    import copy
    import json
    from pathlib import Path
    import tempfile
    from ups_panel.app import load_snapshot
    from ups_panel.calibration import load_config, normalize_config, save_config
    from ups_panel.collector import CalibrationState
    from ups_panel.power import PowerEstimator

    def raw_sample(timestamp, mode='online', voltage=12):
        return {'timestamp': timestamp, 'mode': mode, 'soc': 98,
                'battery_voltage': 16.4, 'cells': [4.1, 4.1, 4.1, 4.1],
                'input_voltage': voltage, 'adapter_input_voltage_v': voltage,
                'raw_fields': {'byte_28': 50},
                'battery_charge_power_candidate_w': 8 if mode == 'charging' else None,
                'battery_discharge_power_candidate_w': 30 if mode == 'battery' else None}

    config = normalize_config({'schema': 2, 'profile': 'custom', 'ac_voltage_nominal_v': 12.0,
                               'coefficients': {'base_gain': 1.25, 'charge_gain': None, 'battery_gain': None}})
    assert type(config['ac_voltage_nominal_v']) is int, 'Nominal voltage was not canonicalized'
    with tempfile.TemporaryDirectory(prefix='.smoke-calibration-', dir=directory) as temporary:
        path = Path(temporary) / 'calibration.json'
        assert save_config(path, config) == load_config(path) == config
        assert path.stat().st_mode & 0o777 == 0o640, 'Calibration file permissions changed'
        state = CalibrationState(path)
        assert state.snapshot()['supported_config_schemas'] == [1, 2], 'Collector capability missing'
        assert state.snapshot()['configurable'] and state.refresh(now=99)
        for timestamp in (100, 102, 104, 106):
            raw = raw_sample(timestamp)
            before = copy.deepcopy(raw)
            sample = state.update(raw)
            assert all(sample[key] == value for key, value in before.items()), 'Calibration changed raw values'
        assert sample['ac_input_estimate_w'] == 62.5, '12 V partial calibration did not estimate online power'
        assert sample['ac_estimate_quality'] == 'custom_unverified'
        assert sample['calibration_schema'] == 2 and sample['ac_voltage_nominal_v'] == 12
        assert sample['calibration_coefficients'] == config['coefficients']
        assert sample['ac_estimate_model'] == 'us3000_custom_v2_' + config['revision']
        assert state.update(raw_sample(106))['ac_input_estimate_w'] == 62.5, 'Duplicate cache failed'

        # Exercise the image API's real snapshot validator with nullable v2 gains.
        snapshot_path = Path(temporary) / 'latest.json'
        snapshot_path.write_text(json.dumps({'schema': 1, 'heartbeat': 106, 'sample': sample,
                                             'calibration': state.snapshot()}))
        loaded = load_snapshot(snapshot_path, now=107)
        assert loaded['fresh'] and loaded['sample']['calibration_revision'] == config['revision']
        assert loaded['sample']['calibration_coefficients']['charge_gain'] is None
        assert loaded['sample']['calibration_coefficients']['battery_gain'] is None

        invalid = json.loads(snapshot_path.read_text())
        invalid['sample']['calibration_profile'] = 'unsupported-smoke-profile'
        snapshot_path.write_text(json.dumps(invalid))
        isolated = load_snapshot(snapshot_path, now=107)
        assert isolated['fresh'] and isolated['sample']['soc'] == sample['soc']
        assert isolated['calibration_validation']['reason'] == 'unsupported_profile'
        assert isolated['sample']['ac_input_estimate_w'] is None
        assert 'calibration_profile' not in isolated['sample']

        for timestamp, mode, quality_field, quality in (
                (108, 'charging', 'ac_estimate_quality', 'charge_not_configured'),
                (110, 'battery', 'battery_estimate_quality', 'battery_not_configured')):
            raw = raw_sample(timestamp, mode)
            before = copy.deepcopy(raw)
            sample = state.update(raw)
            assert sample['ac_input_estimate_w'] is None and sample['battery_energy_estimate_w'] is None
            assert sample[quality_field] == quality, 'Unconfigured gain silently borrowed a default'
            assert all(sample[key] == value for key, value in before.items()), 'Partial calibration changed raw values'
        assert state.update(raw_sample(112, voltage=19))['ac_estimate_quality'] == 'unsupported_voltage'
        legacy = PowerEstimator('local-19v-v1').update(raw_sample(114))
        assert legacy['ac_input_estimate_w'] is None and legacy['ac_estimate_quality'] == 'unsupported_voltage'
    return {'partial_calibration_12v': 'passed', 'collector_config_schemas': [1, 2]}

calibration_checks = check_v2_calibration('/data')

def check_battery_observation(directory):
    # Use the installed estimator, store and monitor on disposable synthetic data.
    # Advancing sample timestamps tests the real 30-minute default without sleeping
    # or changing any production thresholds, collector configuration or API data.
    import math
    from pathlib import Path
    import tempfile
    from ups_panel.calibration import normalize_config
    from ups_panel.cell_balance import CellBalanceMonitor
    from ups_panel.power import PowerEstimator
    from ups_panel.storage import Store

    config = normalize_config({'schema': 2, 'profile': 'custom', 'ac_voltage_nominal_v': 12,
                               'coefficients': {'base_gain': 1, 'charge_gain': None, 'battery_gain': 2}})
    estimator = PowerEstimator(config=config)

    def fixture(timestamp, mode='online', power=None, soc=90, cells=None):
        cells = [4.1, 4.11, 4.11, 4.11] if cells is None else cells
        raw = {'timestamp': timestamp, 'mode': mode, 'soc': soc, 'cells': cells,
               'battery_voltage': sum(cells), 'input_voltage': 12, 'adapter_input_voltage_v': 12,
               'decoder_version': 4, 'formula_version': 2, 'raw_fields': {'byte_28': 50},
               'battery_discharge_power_candidate_w': power if mode == 'battery' else None,
               'battery_charge_power_candidate_w': 8 if mode == 'charging' else None}
        return {'fresh': True, 'source': 'replay',
                'device': {'serial': 'SMOKE-OBSERVATION', 'bus': 1, 'device': 2, 'path': 'synthetic'},
                'sample': estimator.update(raw)}

    with tempfile.TemporaryDirectory(prefix='.smoke-observation-', dir=directory) as temporary:
        path = Path(temporary) / 'history.sqlite'
        store = Store(path)
        for timestamp, mode, power, soc in ((98, 'online', None, 90), (100, 'battery', 10, 90),
                                            (102, 'battery', 20, 89), (105, 'battery', 40, 88),
                                            (106, 'charging', None, 92)):
            item = fixture(timestamp, mode, power, soc)
            if mode == 'battery':
                assert item['sample']['battery_energy_estimate_w'] is None, 'Unexpected smoothing warm-up'
            store.ingest(item, now=timestamp)
        store.flush()
        with store.connect() as db:
            columns = [row[1] for row in db.execute('PRAGMA table_info(battery_sessions)')]
            assert columns == ['id', 'start_ts', 'end_ts', 'last_ts', 'start_soc', 'end_soc',
                               'start_known', 'end_reason', 'sample_count'], 'Legacy session schema changed'
            assert db.execute('PRAGMA user_version').fetchone()[0] == 2
            assert db.execute('SELECT COUNT(*) FROM battery_session_energy').fetchone()[0] == 1
            # Older panels still write nine columns and do not invent energy rows.
            db.execute('INSERT INTO battery_sessions VALUES(?,?,?,?,?,?,?,?,?)',
                       ('smoke-legacy', 80, 84, 82, 95, 94, 1, 'external', 2))
        restored = Store(path).battery_history(1, now=107)
        assert len(restored['records']) == 2
        legacy = next(row for row in restored['records'] if row['id'] == 'smoke-legacy')
        assert legacy['energy'] is None, 'Old records unexpectedly acquired energy estimates'
        record = next(row for row in restored['records'] if row['id'] != 'smoke-legacy')
        energy = record['energy']
        assert record['status'] == 'complete' and record['observed_duration_sec'] == 6
        assert energy['schema'] == 1 and energy['status'] == 'available' and energy['reasons'] == []
        # 20 -> 40 W for 2 seconds, then 40 -> 80 W for 3 seconds = 240 W.s.
        assert math.isclose(energy['estimate_wh'], 240 / 3600, rel_tol=1e-12), 'Energy trapezoids are incorrect'
        assert energy['covered_duration_sec'] == energy['observed_duration_sec'] == 5
        assert energy['interval_count'] == 2 and energy['coverage_ratio'] == 1
        assert energy['start_soc'] == 90 and energy['end_soc'] == 88 and record['end_soc'] == 92
        assert energy['basis']['revision'] == config['revision'] and energy['basis']['battery_gain'] == 2
        assert energy['basis']['source'] == 'replay' and energy['basis']['device']['serial'] == 'SMOKE-OBSERVATION'

    monitor = CellBalanceMonitor()
    for timestamp in range(0, 1801, 5):
        observed = fixture(timestamp)
        monitor.ingest(observed)
    assessed = monitor.snapshot(observed)
    assert assessed['state'] == 'assessed' and assessed['level'] == 'good'
    assert assessed['standby_duration_sec'] == 1800 and assessed['reference_only'] is True
    assert assessed['delta_mv'] == 10 and assessed['recent_sample_count'] == 361
    hidden = monitor.snapshot(dict(observed, fresh=False))
    assert hidden['state'] == 'unavailable' and hidden['reason'] == 'no_fresh_sample'
    assert hidden['level'] is hidden['delta_mv'] is None and not hidden['observed']
    assert monitor.snapshot(observed) == assessed, 'Snapshot read mutated the monitor'
    charging = fixture(1805, mode='charging', cells=[4.0, 4.1, 4.1, 4.1])
    monitor.ingest(charging)
    charging_balance = monitor.snapshot(charging)
    assert charging_balance['state'] == 'charging' and charging_balance['delta_mv'] == 100
    assert charging_balance['level'] is charging_balance['candidate_level'] is None
    assert charging_balance['standby_duration_sec'] == charging_balance['recent_sample_count'] == 0
    return {'battery_energy': 'passed', 'cell_balance': 'passed'}

observation_checks = check_battery_observation('/data')

class Assets(HTMLParser):
    def __init__(self):
        super().__init__()
        self.paths = set()
        self.root = False
    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        self.root |= attrs.get('id') == 'root'
        if tag == 'script' and attrs.get('src'):
            self.paths.add(attrs['src'])
        if tag == 'link' and attrs.get('rel') in ('stylesheet', 'icon', 'apple-touch-icon'):
            self.paths.add(attrs['href'])

html_bytes, html_headers = get('/')
html = html_bytes.decode('utf-8')
assets = Assets()
assets.feed(html)
assert assets.root and 'US3000' in html and len(assets.paths) >= 4, 'Frontend or icons are missing'
assert 'text/html' in html_headers.get('content-type', '')
static = Path(os.environ.get('UPS_STATIC', '/app/static'))
asset_files = [path for path in (static / 'assets').iterdir() if path.is_file()]
assert 4 <= len(asset_files) <= 128, 'Unexpected static asset count'
assert any(path.name.startswith('HistoryChart-') and path.suffix == '.js' for path in asset_files), 'Lazy chart bundle missing'
assets.paths.update('/assets/' + quote(path.name) for path in asset_files)
for path in sorted(assets.paths):
    assert not urlsplit(path).netloc and path.startswith('/assets/'), 'External asset dependency found'
    body, headers = get(path)
    assert body and len(body) <= 5_000_000, 'Empty or unexpectedly large asset: ' + path
    assert 'text/html' not in headers.get('content-type', ''), 'Asset returned SPA HTML: ' + path
    if path.endswith('.png'):
        assert body.startswith(b'\x89PNG\r\n\x1a\n'), 'Invalid PNG asset: ' + path

# Require a committed SQLite row, not just the in-memory /history result.
rows = 0
database = Path('/data/history.sqlite')
while time.monotonic() < deadline:
    if database.exists():
        try:
            with sqlite3.connect('file:/data/history.sqlite?mode=ro', uri=True, timeout=2) as db:
                rows = db.execute('SELECT COUNT(*) FROM samples').fetchone()[0]
        except sqlite3.Error:
            rows = 0
    if rows:
        break
    time.sleep(.5)
assert rows > 0, 'History was not committed to SQLite'
history = get_json('/api/history?hours=1')
assert history['points'], 'History endpoint returned no points'
assert all(point['context']['calibration_profile'] == 'none' for point in history['points'])
assert all('ac_input_estimate_w' not in point['values'] and 'battery_energy_estimate_w' not in point['values'] for point in history['points'])
assert any('soc' in point['values'] and 'ups_output_voltage_v' in point['values'] for point in history['points'])
assert get_json('/api/events'), 'Initial connection event was not recorded'
annual = get_json('/api/history?hours=8760')
assert annual['resolution_sec'] == 86400 and annual['points'], 'Daily history was not generated'
sessions = get_json('/api/battery-sessions?days=365')
assert sessions['recording_since'] is not None and sessions['capture_fresh']
assert sessions['records'] == [] and sessions['summary']['confirmed_starts'] == 0
capacity = get_json('/api/battery-capacity')
assert capacity['schema'] == 1 and capacity['capture_fresh']
assert capacity['baseline'] is None and capacity['comparison'] is None
assert capacity['progress'] is None and capacity['recent'] == []
assert capacity['criteria']['soc_start'] == 90 and capacity['criteria']['soc_end'] == 80
assert capacity['temperature_known'] is False
usage = get_json('/api/energy-usage')
assert usage['schema'] == 1 and usage['capture_fresh']
assert usage['timezone'] == 'Asia/Shanghai' and usage['utc_offset'] == '+08:00'
assert usage['summary']['estimate_kwh'] is None and usage['summary']['complete_days'] == 0
assert usage['today']['estimate_kwh'] is None and usage['today']['covered_sec'] == 0
assert usage['tracking_started_at'] is not None and usage['storage_error'] is None
usage_day = get_json('/api/energy-usage/day?date=' + usage['current_date'])
assert usage_day['day']['estimate_kwh'] is None and len(usage_day['hours']) == 24
assert all(hour['estimate_kwh'] is None for hour in usage_day['hours'])
csv_bytes, _ = get('/api/export.csv?hours=1')
csv_rows = list(csv.DictReader(io.StringIO(csv_bytes.decode('utf-8-sig'))))
assert csv_rows and all(row['calibration_profile'] == 'none' for row in csv_rows)
try:
    get('/api/history?hours=0')
except HTTPError as exc:
    assert exc.code == 422
else:
    raise AssertionError('Invalid history range was accepted')
print(json.dumps({'uid': os.getuid(), 'machine': platform.machine(),
                  'assets': len(assets.paths), 'sqlite_rows': rows,
                  'history_points': len(history['points']), 'csv_rows': len(csv_rows),
                  'calibration_profile': 'none', **calibration_checks, **observation_checks, 'checks': 'passed'}))
'''


# Exercise the real HTTP routes in the same isolated image. The host controls
# only the disposable capture mount; neither probe starts a collector or NUT.
DIAGNOSTICS_PROBE = r'''
import csv, io, json, re, sys, time
from urllib.request import ProxyHandler, Request, build_opener
from urllib.error import HTTPError

opener = build_opener(ProxyHandler({}))
mode = sys.argv[1]

def get(path):
    with opener.open('http://127.0.0.1:8080' + path, timeout=3) as response:
        headers = {key.lower(): value for key, value in response.headers.items()}
        assert headers.get('cache-control') == 'no-store', 'Diagnostics must disable caching'
        return response.read(), headers

def diagnostics():
    body, headers = get('/api/diagnostics?minutes=15')
    assert 'application/json' in headers.get('content-type', '')
    data = json.loads(body)
    assert data['schema'] == data['observation']['schema'] == 1
    assert data['versions']['panel']['version'] == '0.12.2', 'Unexpected installed panel version'
    assert data['versions']['collector'] is None, 'Missing collector version was invented'
    assert data['versions']['usb_device_version'] is None, 'Missing USB version was invented'
    assert data['observation']['window_sec'] == 900 and data['observation']['max_samples'] == 4096
    assert data['observation']['count'] == len(data['observation']['points'])
    return data

def wait_for(predicate):
    deadline = time.monotonic() + 8
    while True:
        data = diagnostics()
        if predicate(data):
            return data
        assert time.monotonic() < deadline, 'Diagnostic observer did not accept the expected fixture'
        time.sleep(.1)

def check_no_usb(data):
    checks = {item['id']: item for item in data['connection']['checks']}
    for key in ('host', 'usbmon', 'usb', 'private_report', 'association'):
        assert checks[key]['status'] != 'ok', 'No USB/collector evidence was misreported as confirmed: ' + key
    assert data['connection']['association'] == 'unverified' and data['connection']['pollonly'] is None
    assert data['connection']['counters'] == {}, 'Missing capture counters became measured zeroes'

def check_exports(data, private=False):
    json_body, json_headers = get('/api/diagnostics/export.json?minutes=15')
    csv_body, csv_headers = get('/api/diagnostics/export.csv?minutes=15')
    assert 'application/json' in json_headers.get('content-type', '')
    assert 'text/csv' in csv_headers.get('content-type', '')
    for headers in (json_headers, csv_headers):
        assert headers.get('content-disposition', '').startswith('attachment;'), 'Export is not a download'
    export = json.loads(json_body)
    assert export['schema'] == 1 and export['format'] == 'us3000-diagnostics'
    assert export['versions']['panel']['version'] == '0.12.2'
    assert export['privacy']['mode'] == 'allowlist'
    def check_keys(value):
        if isinstance(value, dict):
            assert not ({'serial', 'target', 'address', 'path', 'frame_hex', 'raw_fields', 'alarm_text'} & set(value)), 'Export included a private field'
            for child in value.values():
                check_keys(child)
        elif isinstance(value, list):
            for child in value:
                check_keys(child)
    check_keys(export)
    assert 'latest' not in export['observation'], 'Export included the unfiltered latest object'
    points = export['observation']['points']
    fields = ('timestamp', 'segment', 'device_alias', 'source', 'mode', 'raw_status',
              'byte_26', 'byte_27', 'byte_28', 'soc', 'battery_voltage',
              'adapter_input_voltage_v', 'ups_output_voltage_v', 'current',
              'battery_charge_current_candidate_a', 'battery_discharge_current_candidate_a',
              'decoder_version', 'formula_version', 'calibration_revision', 'calibration_profile')
    reader = csv.DictReader(io.StringIO(csv_body.decode('utf-8-sig')))
    rows = list(reader)
    assert reader.fieldnames == list(fields), 'Observation CSV columns changed or contain extra fields'
    assert points and rows, 'Diagnostic exports omitted observed replay points'
    assert all(set(point) == set(fields) and re.fullmatch(r'device-[a-f0-9]{8,32}', point['device_alias']) for point in points)
    assert all(point['byte_26'] == 41 and point['byte_27'] == 48 and point['source'] == 'replay' for point in points)
    assert all(row['byte_26'] == '41' and row['byte_27'] == '48' for row in rows)
    for marker in ('SMOKE-TEST', 'SMOKE-NUT-TARGET', '192.0.2.123', '/SMOKE-USB-PATH',
                   'SMOKE-FREE-ALARM', 'SMOKE-FREE-STATUS', 'SMOKE-FREE-VERSION', '71' + '42' * 63):
        assert marker.encode() not in json_body and marker.encode() not in csv_body, 'Export leaked fixture marker: ' + marker
    assert 'alarm_text' not in export['system'] and 'ups.alarm' not in export['system']['values']
    if private:
        # Prove the canaries reached the live route before accepting their absence
        # in exports. Do not make this a vacuous check of unavailable NUT data.
        assert data['system']['fresh'] and data['system']['alarm_text'] == 'SMOKE-FREE-ALARM'
        assert 'SMOKE-FREE-STATUS' in data['system']['status_raw']
        assert data['versions']['ups_firmware'] == 'SMOKE-FREE-VERSION'
        assert data['system']['runtime']['quality'] == 'sentinel'
        assert data['system']['runtime']['seconds'] is None and data['system']['runtime']['raw'] == '65535'
        assert export['system']['free_text_alarm_present'] is True
        assert export['system']['status_tokens'] == ['OL', 'ALARM']
        assert export['versions']['ups_firmware'] is None
        assert len(points) == len(rows) == data['observation']['count'], 'Frozen export window gained or lost points'
    return len(points)

def check_optional_updater():
    body, _ = get('/api/collector-update')
    status = json.loads(body)
    assert status['schema'] == 1 and status['installed'] is False
    assert status['availability'] == 'not_installed' and status['operation'] is None
    assert status['auth_required'] is True
    assert not status['update_available'] and status['latest'] is None
    for headers, expected in (({'Content-Type': 'application/json'}, 403),
        ({'Content-Type': 'application/json', 'X-UPS-Update': '1', 'X-UPS-Update-Key': 'A' * 43}, 503)):
        try:
            opener.open(Request('http://127.0.0.1:8080/api/collector-update/check', data=b'{}', headers=headers), timeout=3)
        except HTTPError as error:
            assert error.code == expected
            response = error.read()
            assert b'A' * 43 not in response
        else:
            raise AssertionError('Unavailable updater accepted an action')

check_optional_updater()

if mode == 'growing':
    first = wait_for(lambda data: data['observation']['count'] >= 1)
    data = wait_for(lambda data: data['observation']['count'] > first['observation']['count'])
    check_no_usb(data)
    assert data['capture_fresh'] and data['observation']['latest']['fresh']
    assert data['system']['available'] is False and data['system']['fresh'] is False
    assert data['system']['runtime']['quality'] == 'unavailable'
    assert data['system']['runtime']['raw'] is None and data['system']['thresholds']['charge_low'] is None
    assert {item['id']: item for item in data['connection']['checks']}['nut']['status'] != 'ok'
    count = check_exports(data)
elif mode == 'frozen':
    expected = float(sys.argv[2])
    data = wait_for(lambda data: data['observation']['latest']['timestamp'] == expected and data['observation']['latest']['observed'])
    check_no_usb(data)
    baseline = data['observation']
    for _ in range(6):
        current = diagnostics()['observation']
        for key in ('count', 'points', 'gap_count', 'conflict_count', 'rejected_count'):
            assert current[key] == baseline[key], 'GET requests manufactured or changed observations: ' + key
    count = check_exports(data, private=True)
else:
    assert mode in ('stale', 'missing'), 'Unknown diagnostic smoke phase'
    data = diagnostics()
    check_no_usb(data)
    assert data['capture_fresh'] is False and data['system']['fresh'] is False
    latest = data['observation']['latest']
    assert latest['fresh'] is False and latest['observed'] is False
    assert all(latest[key] is None for key in ('timestamp', 'byte_26', 'byte_27', 'byte_28', 'mode', 'source', 'device_alias'))
    assert data['system']['alarm_text'] is None and data['system']['thresholds']['charge_low'] is None
    assert {item['id']: item for item in data['connection']['checks']}['nut']['status'] != 'ok'
    count = check_exports(data)
print(json.dumps({'phase': mode, 'diagnostic_points': count, 'checks': 'passed'}))
'''


def cleanup(name: str, token: str):
    """Remove only a container carrying the exact token created by this run."""
    result = docker('inspect', name, check=False, timeout=15)
    if result.returncode:
        if 'No such object' in result.stderr or 'No such container' in result.stderr:
            return
        raise SmokeError(f'Could not verify cleanup of {name}; check the local Docker engine.')
    container = json.loads(result.stdout)[0]
    if container['Config'].get('Labels', {}).get(LABEL) != token:
        raise SmokeError(f'Refusing to remove an unrelated container named {name}.')
    docker('stop', '--time', '5', container['Id'], check=False, timeout=15)
    docker('rm', '--force', container['Id'], timeout=15)


def run(image: str, platform: str | None):
    require_local_daemon()
    metadata = json.loads(docker('image', 'inspect', image).stdout)[0]
    if metadata['Config'].get('User') != '10001:10001':
        raise SmokeError('Image must declare USER 10001:10001; the test will not override its user.')
    if not metadata['Config'].get('Healthcheck', {}).get('Test'):
        raise SmokeError('Image does not define a health check.')

    token = uuid.uuid4().hex
    name = 'us3000-smoke-' + token[:16]
    stop = threading.Event()
    writer_errors: list[str] = []
    with tempfile.TemporaryDirectory(prefix='us3000-smoke-') as directory:
        root = Path(directory).resolve()
        capture, data = root / 'capture', root / 'data'
        capture.mkdir(mode=0o755)
        data.mkdir(mode=0o700)
        # The parent directory remains private. Only this disposable mounted
        # directory needs to accept writes from container UID 10001 on any host.
        data.chmod(0o777)
        write_snapshot(capture, time.time() - 0.2)
        writer = threading.Thread(target=refresh_snapshots, args=(capture, stop, writer_errors), daemon=True)
        writer.start()
        attempted = False
        try:
            arguments = [
                'create', '--pull', 'never', '--name', name, '--label', f'{LABEL}={token}',
                '--network', 'none', '--read-only', '--cap-drop', 'ALL',
                '--security-opt', 'no-new-privileges:true', '--memory', '256m',
                '--cpus', '0.50', '--pids-limit', '64',
                '--tmpfs', '/tmp:rw,nosuid,nodev,noexec,size=16m,mode=1777',
                '--mount', f'type=bind,src={capture},dst=/capture,readonly',
                '--mount', f'type=bind,src={data},dst=/data',
                '--env', 'UPS_SNAPSHOT=/capture/latest.json',
                '--env', 'UPS_DATABASE=/data/history.sqlite', '--env', 'TZ=UTC',
            ]
            if platform:
                arguments += ['--platform', platform]
            arguments.append(image)
            attempted = True
            docker(*arguments)
            check_configuration(inspect_container(name))
            print(f'Created {name}: no network, no published ports, UID 10001, read-only root.', flush=True)
            docker('start', name)
            result = docker('exec', name, 'python', '-c', PROBE, timeout=60)
            if writer_errors:
                raise SmokeError('Fixture refresh failed: ' + writer_errors[0])
            report = json.loads(result.stdout)
            expected_machine = {'linux/amd64': 'x86_64', 'linux/arm64': 'aarch64'}.get(platform)
            if expected_machine and report['machine'] != expected_machine:
                raise SmokeError(f"Requested {platform}, but container runs on {report['machine']}.")
            diagnostic_growth = json.loads(docker('exec', name, 'python', '-c',
                                                  DIAGNOSTICS_PROBE, 'growing', timeout=30).stdout)
            # Stop host updates before checking read-only and stale diagnostics.
            stop.set()
            writer.join(timeout=3)
            if writer.is_alive():
                raise SmokeError('Fixture refresh thread did not stop.')
            # Freeze one real timestamp so background polls and repeated HTTP
            # reads cannot claim new observations. NUT canaries are synthetic.
            frozen_at = time.time() - .2
            frozen = snapshot(frozen_at)
            frozen['device'].update(path='/SMOKE-USB-PATH', address='192.0.2.123')
            frozen['nut'] = {
                'available': True, 'timestamp': frozen_at, 'target': 'SMOKE-NUT-TARGET@192.0.2.123',
                'values': {'ups.status': 'OL ALARM SMOKE-FREE-STATUS', 'ups.alarm': 'SMOKE-FREE-ALARM',
                           'battery.runtime': '65535', 'battery.charge.low': '20',
                           'driver.name': 'usbhid-ups', 'driver.version': '2.8.1',
                           'ups.firmware': 'SMOKE-FREE-VERSION'},
            }
            write_capture(capture, frozen)
            diagnostic_frozen = json.loads(docker('exec', name, 'python', '-c',
                                                  DIAGNOSTICS_PROBE, 'frozen', str(frozen_at), timeout=30).stdout)
            write_snapshot(capture, time.time() - 60)
            docker('exec', name, 'python', '-c',
                "import json,urllib.request; o=urllib.request.build_opener(urllib.request.ProxyHandler({})); "
                "v=json.load(o.open('http://127.0.0.1:8080/api/live',timeout=3)); "
                "h=json.load(o.open('http://127.0.0.1:8080/api/health',timeout=3)); "
                "assert not v['fresh'] and not h['capture_fresh']; b=v['cell_balance']; "
                "assert b['state']=='unavailable' and b['reason']=='no_fresh_sample'; "
                "assert b['level'] is None and b['delta_mv'] is None and not b['observed']; "
                "assert b['recent_sample_count']==0 and b['sample_timestamp'] is None; "
                "print('stale capture and cell grade hidden')")
            docker('exec', name, 'python', '-c', DIAGNOSTICS_PROBE, 'stale', timeout=30)
            (capture / 'latest.json').unlink()
            docker('exec', name, 'python', '-c', DIAGNOSTICS_PROBE, 'missing', timeout=30)
            report.update(image=image, image_id=metadata['Id'],
                          platform=platform or 'linux/' + {
                              'x86_64': 'amd64', 'aarch64': 'arm64',
                          }.get(report['machine'], metadata['Architecture']),
                          stale_capture='passed', stale_cell_balance='passed',
                          diagnostic_api='passed', diagnostic_exports='passed',
                          diagnostic_growth=diagnostic_growth['diagnostic_points'],
                          diagnostic_frozen_points=diagnostic_frozen['diagnostic_points'],
                          diagnostic_readonly='passed', diagnostic_privacy='passed',
                          stale_diagnostics='passed', missing_capture_diagnostics='passed')
            print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)
        except BaseException:
            if attempted:
                logs = docker('logs', '--tail', '30', name, check=False, timeout=10)
                detail = (logs.stdout + logs.stderr).strip()
                if detail:
                    print('Container diagnostics:\n' + detail[-5000:], file=sys.stderr)
            raise
        finally:
            stop.set()
            writer.join(timeout=3)
            if attempted:
                cleanup(name, token)
    print('Cleaned up the smoke-test container and temporary capture/history directories.', flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--image', required=True, help='Existing local image tag or digest; never pulled automatically')
    parser.add_argument('--platform', help='Optional Docker platform, e.g. linux/amd64 or linux/arm64')
    options = parser.parse_args()
    if not options.image or options.image.startswith('-'):
        parser.error('--image must be an image reference, not a Docker option')
    if options.platform and not options.platform.startswith('linux/'):
        parser.error('--platform must be a Linux platform supported by the local Docker engine')

    def interrupt(signum, frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, interrupt)
    try:
        run(options.image, options.platform)
        return 0
    except KeyboardInterrupt:
        print('Smoke test interrupted; cleanup requested.', file=sys.stderr)
        return 130
    except (SmokeError, OSError, ValueError, KeyError, TypeError, IndexError) as exc:
        print(f'Smoke test failed: {exc}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
