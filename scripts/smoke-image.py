#!/usr/bin/env python3
"""Smoke-test a locally built image without a UPS, NUT, or external network.

Usage: python3 scripts/smoke-image.py --image us3000-panel:test [--platform linux/amd64]

Only Python's standard library and the Docker CLI are required on the host. The
image must already exist locally: this script never builds or pulls an image.
All HTTP checks run inside an isolated container against its own loopback port.
"""

from __future__ import annotations

import argparse
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
    return {
        'schema': 1, 'heartbeat': timestamp, 'source': 'replay',
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
            'ac_input_estimate_w': None, 'ac_estimate_model': None,
            'ac_estimate_quality': 'not_configured', 'ac_estimate_window_sec': 8,
            'battery_energy_estimate_w': None, 'battery_estimate_basis': None,
            'battery_estimate_quality': 'not_configured',
            'battery_charge_current_candidate_a': None,
            'battery_discharge_current_candidate_a': None,
            'battery_charge_power_candidate_w': None,
            'battery_discharge_power_candidate_w': None,
            'raw_fields': {'be_u16': {'16': 19010, '18': 18990, '24': 2000},
                           'byte_28': 50, 'frame_hex': ''},
        },
    }


def write_snapshot(capture: Path, timestamp: float):
    temporary = capture / 'latest.json.new'
    temporary.write_text(json.dumps(snapshot(timestamp)), encoding='utf-8')
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
assert live['sample']['ac_input_estimate_w'] is None and live['sample']['battery_energy_estimate_w'] is None
assert live['sample']['ac_estimate_quality'] == 'not_configured'
assert live_headers.get('cache-control') == 'no-store', 'Live responses must disable caching'

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
                  'calibration_profile': 'none', **calibration_checks, 'checks': 'passed'}))
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
            # Confirm stale captures cannot remain labelled as fresh.
            stop.set()
            writer.join(timeout=3)
            if writer.is_alive():
                raise SmokeError('Fixture refresh thread did not stop.')
            write_snapshot(capture, time.time() - 60)
            docker('exec', name, 'python', '-c',
                "import json,urllib.request; o=urllib.request.build_opener(urllib.request.ProxyHandler({})); "
                "v=json.load(o.open('http://127.0.0.1:8080/api/live',timeout=3)); "
                "h=json.load(o.open('http://127.0.0.1:8080/api/health',timeout=3)); "
                "assert not v['fresh'] and not h['capture_fresh']; print('stale capture hidden')")
            report.update(image=image, image_id=metadata['Id'],
                          platform=platform or 'linux/' + {
                              'x86_64': 'amd64', 'aarch64': 'arm64',
                          }.get(report['machine'], metadata['Architecture']),
                          stale_capture='passed')
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
