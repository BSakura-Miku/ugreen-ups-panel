"""Passive collector with atomic snapshot publishing and optional read-only NUT lookup."""
import argparse
from collections import deque
import json
import logging
import os
from pathlib import Path
import platform
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time

from .build_info import get_build_info
from .protocol import parse_frame
from .calibration import CalibrationError, PROFILES, SUPPORTED_CONFIG_SCHEMAS, default_config, load_config
from .config_target import config_file_state, target_identity
from .power import CALIBRATION_PROFILES, PowerEstimator
from .usbmon import Reader, classify_event, decode_event, discover

LOG = logging.getLogger('collector')
NUT_UPSC = '/usr/bin/upsc'
NUT_VALUE_LIMIT = 256
NUT_RESPONSE_LIMIT = 65536
NUT_ALLOWED = frozenset({
    'ups.status', 'ups.alarm', 'battery.charge', 'battery.charge.low',
    'battery.runtime', 'battery.runtime.low', 'input.voltage', 'output.voltage',
    'ups.load', 'driver.name', 'driver.version', 'driver.version.data',
    'driver.version.internal', 'driver.version.usb', 'driver.parameter.subdriver',
    'driver.parameter.pollinterval', 'driver.parameter.pollfreq', 'driver.flag.pollonly',
    'ups.firmware', 'ups.firmware.aux', 'device.mfr', 'device.model',
    'ups.mfr', 'ups.model', 'ups.vendorid', 'ups.productid',
})
CAPTURE_COUNTERS = (
    'target_events', 'not_completion', 'not_interrupt_in', 'invalid_status',
    'missing_payload', 'invalid_length', 'invalid_report_id', 'accepted_reports',
    'invalid_sample', 'stale_sample',
)
COUNTER_MAX = 2147483647


def bounded_text(value, limit=128):
    """Bound local diagnostics and strip control characters; never run their text."""
    if not isinstance(value, str):
        return None
    return ''.join(c for c in value[:limit * 4] if c.isprintable()).strip()[:limit]


def host_metadata():
    return {'system': bounded_text(platform.system()),
            'kernel_release': bounded_text(platform.release()),
            'python_version': platform.python_version(),
            'python_supported': sys.version_info >= (3, 10),
            'systemd_available': bool(shutil.which('systemctl') and Path('/run/systemd/system').is_dir()),
            'usbmon_module_loaded': Path('/sys/module/usbmon').is_dir(),
            'upsc_available': os.path.isfile(NUT_UPSC) and os.access(NUT_UPSC, os.X_OK)}


def usb_metadata(device, discovery='found', root=Path('/sys/bus/usb/devices'), dev_root=Path('/dev')):
    """Display-only descriptors live outside device/session identity."""
    result = {'discovery': discovery, 'vendor_id': '2b89', 'product_id': 'ffff',
              'manufacturer': None, 'product': None, 'bcd_device': None,
              'usbmon_node_exists': False, 'usbmon_readable': False}
    if not device:
        return result
    directory = root / device['path']
    for source, target in (('manufacturer', 'manufacturer'), ('product', 'product'), ('bcdDevice', 'bcd_device')):
        try:
            with (directory / source).open() as stream:
                result[target] = bounded_text(stream.read(513))
        except (OSError, UnicodeError):
            pass
    node = dev_root / f"usbmon{device['bus']}"
    result['usbmon_node_exists'] = node.exists()
    result['usbmon_readable'] = result['usbmon_node_exists'] and os.access(node, os.R_OK)
    return result


def valid_nut_target(target):
    return (isinstance(target, str) and len(target) <= 256
            and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.+-]*(?:@[A-Za-z0-9.\[\]:_+-]+)?', target) is not None)


def local_nut_target(target):
    if '@' not in target:
        return True
    address = target.split('@', 1)[1].lower()
    return re.fullmatch(r'(?:localhost|127\.0\.0\.1|\[::1\])(?::[0-9]{1,5})?', address) is not None


def nut_association(values, target, device):
    """Use exact, untruncated identifiers internally; never return their contents."""
    def result(status, reason):
        return {'status': status, 'reason': reason}
    if not device:
        return result('unverified', 'device_unavailable')
    if not local_nut_target(target):
        return result('unverified', 'remote_target')
    serials = {values[k].strip() for k in ('device.serial', 'ups.serial') if values.get(k)}
    if len(serials) > 1:
        return result('unverified', 'conflicting_identity')
    serial = next(iter(serials), '')
    actual = device.get('serial', '')
    if (not isinstance(actual, str) or len(actual) > 256 or len(serial) > 256
            or any(not c.isprintable() for c in serial + actual)):
        return result('unverified', 'invalid_identity')
    vendor = values.get('ups.vendorid', '').strip().lower()
    product = values.get('ups.productid', '').strip().lower()
    if (re.fullmatch('[0-9a-f]{4}', vendor) and vendor != '2b89'
            or re.fullmatch('[0-9a-f]{4}', product) and product != 'ffff'):
        return result('different', 'different_usb_ids')
    if serial and actual and serial != actual:
        return result('different', 'different_serial')
    if serial and actual and vendor == '2b89' and product == 'ffff':
        return result('matched', 'local_serial_and_usb_ids')
    return result('unverified', 'missing_identity')


def pollonly_fact(values):
    raw = values.get('driver.flag.pollonly')
    if raw is None:
        return {'state': 'unknown', 'source': 'not_reported'}
    normalized = raw.strip().lower()
    state = ('enabled' if normalized in ('enabled', 'true', 'yes', 'on', '1')
             else 'disabled' if normalized in ('disabled', 'false', 'no', 'off', '0')
             else 'unknown')
    return {'state': state, 'source': 'nut_reported_flag'}


class CaptureDiagnostics:
    """Fixed counters plus at most 60 second buckets; no USB payload retention."""
    window_sec = 60

    def __init__(self):
        self.started_at = time.time()
        self.started_mono = time.monotonic()
        self.counters = dict.fromkeys(CAPTURE_COUNTERS, 0)
        self.buckets = deque(maxlen=self.window_sec)
        self.last_target_seen_at = None
        self.last_valid_sample_at = None

    def _prune(self, mono):
        while self.buckets and self.buckets[0][0] <= int(mono) - self.window_sec:
            self.buckets.popleft()

    def count(self, name):
        if name not in self.counters:
            return
        mono = time.monotonic()
        self._prune(mono)
        if not self.buckets or self.buckets[-1][0] != int(mono):
            self.buckets.append((int(mono), dict.fromkeys(CAPTURE_COUNTERS, 0)))
        bucket = self.buckets[-1][1]
        bucket[name] = min(COUNTER_MAX, bucket[name] + 1)
        self.counters[name] = min(COUNTER_MAX, self.counters[name] + 1)

    def observe(self, header, payload, bus, device):
        reason = classify_event(header, payload, bus, device)
        if reason in ('unrelated', 'invalid_header'):
            return reason
        self.last_target_seen_at = time.time()
        self.count('target_events')
        self.count('accepted_reports' if reason == 'accepted' else reason)
        return reason

    def valid_sample(self, timestamp):
        self.last_valid_sample_at = timestamp

    def snapshot(self, available=True, replay=False):
        now, mono = time.time(), time.monotonic()
        self._prune(mono)
        recent = {key: min(COUNTER_MAX, sum(bucket[key] for _, bucket in self.buckets)) for key in CAPTURE_COUNTERS}
        age = now - self.last_valid_sample_at if self.last_valid_sample_at is not None else None
        if replay:
            state = 'replay'
        elif not available:
            state = 'unavailable'
        elif age is not None and 0 <= age <= 10:
            state = 'fresh'
        elif recent['invalid_sample'] or recent['stale_sample']:
            state = 'invalid_reports'
        elif recent['invalid_status'] or recent['missing_payload'] or recent['invalid_length'] or recent['invalid_report_id']:
            state = 'no_complete_report'
        elif recent['not_interrupt_in'] and recent['not_interrupt_in'] + recent['not_completion'] == recent['target_events']:
            state = 'no_interrupt_in'
        elif self.last_valid_sample_at is not None:
            state = 'stale'
        elif recent['target_events']:
            state = 'no_complete_report'
        elif mono - self.started_mono < 10:
            state = 'waiting'
        else:
            state = 'no_target_activity'
        return {'schema': 1, 'state': state, 'window_sec': self.window_sec,
                'window_started_at': max(self.started_at, now - self.window_sec),
                'last_target_seen_at': self.last_target_seen_at,
                'last_valid_sample_at': self.last_valid_sample_at,
                'counters': dict(self.counters), 'recent_counters': recent}


def atomic_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='.snapshot-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as f:
            json.dump(data, f, ensure_ascii=False, allow_nan=False)
            f.flush()
            os.fchmod(f.fileno(), 0o644)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def nut_snapshot(target, device=None):
    def unavailable(code):
        return {'available': False, 'timestamp': time.time(), 'timestamp_kind': 'query_completed',
                'target': bounded_text(target, 256), 'error': 'NUT 查询不可用', 'error_code': code,
                'association': {'status': 'unverified', 'reason': 'query_unavailable'},
                'pollonly': {'state': 'unknown', 'source': 'not_reported'}}
    if not valid_nut_target(target):
        return unavailable('invalid_target')
    try:
        result = subprocess.run([NUT_UPSC, target], capture_output=True, text=True, timeout=2)
        if result.returncode:
            return unavailable('query_failed')
        if len(result.stdout) > NUT_RESPONSE_LIMIT:
            return unavailable('response_too_large')
        values = dict(line.split(': ', 1) for line in result.stdout.splitlines() if ': ' in line)
        return {'available': True, 'timestamp': time.time(), 'timestamp_kind': 'query_completed',
                'target': bounded_text(target, 256),
                'values': {k: bounded_text(v, NUT_VALUE_LIMIT) for k, v in values.items() if k in NUT_ALLOWED},
                'association': nut_association(values, target, device), 'pollonly': pollonly_fact(values)}
    except FileNotFoundError:
        return unavailable('upsc_missing')
    except subprocess.TimeoutExpired:
        return unavailable('query_timeout')
    except (OSError, UnicodeError):
        return unavailable('query_unavailable')


class CalibrationState:
    """Keep the last valid configuration and never reuse estimates across revisions."""

    def __init__(self, path=None, profile='none'):
        self.path = path or None
        self.fallback = default_config(profile)
        self.config = self.fallback
        self.error = None
        self.file_state = config_file_state(self.path)
        self.changed_at = None
        self.reset_estimator()

    def reset_estimator(self):
        self.estimator = PowerEstimator(config=self.config)

    def refresh(self, now=None):
        self.file_state = config_file_state(self.path)
        try:
            config = (load_config(self.path) if self.path else None) or self.fallback
        except CalibrationError as exc:
            self.file_state = 'unreadable' if exc.code == 'file_unreadable' else 'invalid'
            message = str(exc)
            if message != self.error:
                LOG.warning('Calibration configuration: %s', message)
            self.error = message
            return False
        self.error = None
        if config['revision'] == self.config['revision']:
            return False
        self.config = config
        self.changed_at = time.time() if now is None else now
        self.reset_estimator()
        return True

    def update(self, sample):
        # The kernel may have queued an older report while the file was being changed.
        # Keep its original model identity by waiting for a report after this switch.
        if self.changed_at is not None and sample['timestamp'] <= self.changed_at:
            raise ValueError('USB report predates calibration change')
        result = self.estimator.update(sample)
        # The transition barrier only filters reports already queued at the switch.
        # Afterwards, clock corrections must use the estimator's normal reset logic.
        self.changed_at = None
        return result

    def snapshot(self):
        return {'config': self.config, 'configurable': bool(self.path), 'error': self.error,
                'config_target': target_identity(self.path), 'file_state': self.file_state,
                'supported_profiles': list(PROFILES),
                'supported_config_schemas': list(SUPPORTED_CONFIG_SCHEMAS)}


def run(output, serial='', duration=0, replay=None, nut='ups0@localhost', calibration_profile='none',
        calibration_config=None):
    stop = False
    def terminate(*_):
        nonlocal stop
        stop = True
    signal.signal(signal.SIGTERM, terminate)
    signal.signal(signal.SIGINT, terminate)
    started = time.monotonic()
    reader = None
    device = None
    latest = None
    capture = CaptureDiagnostics()
    metadata = {'schema': 1, 'build': get_build_info(), 'host': host_metadata(),
                'usb': usb_metadata(None, 'replay' if replay else 'not_found')}
    calibration = CalibrationState(calibration_config, calibration_profile)
    frame_count = rejected = dropped = 0
    next_discovery = next_publish = next_nut = next_replay = next_calibration_check = 0
    nut_data = {'available': False}
    error = None
    fixtures = [bytes.fromhex(line) for line in Path(replay).read_text().splitlines() if line.strip()] if replay else []
    if replay and not fixtures:
        raise ValueError('Replay fixture contains no reports')
    if duration < 0:
        raise ValueError('Duration must be non-negative')

    def publish():
        nonlocal dropped, next_publish
        if reader:
            _, lost = reader.stats()
            dropped = min(COUNTER_MAX, dropped + lost)
        atomic_json(output, {'schema': 1, 'source': 'replay' if replay else 'usbmon',
            'heartbeat': time.time(), 'device': device, 'sample': latest, 'nut': nut_data,
            'collector': metadata,
            'calibration': calibration.snapshot(),
            'diagnostics': {'frames': frame_count, 'rejected': rejected, 'dropped': dropped,
                            'uptime_sec': round(time.monotonic() - started), 'error': error,
                            'capture': capture.snapshot(available=reader is not None, replay=bool(replay))}})
        next_publish = time.monotonic() + 2

    def refresh_calibration(force=False):
        nonlocal latest, next_calibration_check
        checked_at = time.monotonic()
        if not force and checked_at < next_calibration_check:
            return
        next_calibration_check = checked_at + 1
        previous_error = calibration.error
        changed = calibration.refresh()
        if changed:
            latest = None
        if changed or previous_error != calibration.error:
            # Publish the cleared sample immediately, before a blocking USB/NUT read.
            publish()

    try:
        while not stop and (not duration or time.monotonic() - started < duration):
            now = time.monotonic()
            try:
                refresh_calibration()
                if not replay and now >= next_discovery:
                    try:
                        found = discover(serial=serial)
                    except RuntimeError:
                        metadata['usb'] = usb_metadata(None, 'ambiguous')
                        device = None
                        latest = None
                        calibration.reset_estimator()
                        capture = CaptureDiagnostics()
                        nut_data = {**nut_data, 'association': {'status': 'unverified', 'reason': 'device_unavailable'}}
                        next_nut = 0
                        raise
                    except OSError:
                        metadata['usb'] = usb_metadata(None, 'unavailable')
                        device = None
                        latest = None
                        calibration.reset_estimator()
                        capture = CaptureDiagnostics()
                        nut_data = {**nut_data, 'association': {'status': 'unverified', 'reason': 'device_unavailable'}}
                        next_nut = 0
                        raise
                    metadata['usb'] = usb_metadata(found, 'found' if found else 'not_found')
                    if found != device:
                        if reader:
                            reader.close()
                        reader = None
                        calibration.reset_estimator()
                        capture = CaptureDiagnostics()
                        device = found
                        latest = None
                        nut_data = {**nut_data, 'association': {'status': 'unverified', 'reason': 'device_changed'}}
                        next_nut = 0
                    if device and not reader:
                        reader = Reader(device['bus'])
                        error = None
                    if not device:
                        error = '未找到 US3000 USB 设备'
                    next_discovery = now + 3
                event = None
                if replay:
                    if now >= next_replay:
                        event = {'frame': fixtures[frame_count % len(fixtures)], 'timestamp': time.time(), 'usb_status': -2}
                        next_replay = now + 2
                    else:
                        time.sleep(.1)
                elif reader:
                    packet = reader.read(.5)
                    if packet:
                        if capture.observe(*packet, device['bus'], device['device']) == 'accepted':
                            event = decode_event(*packet, device['bus'], device['device'])
                else:
                    time.sleep(.5)
                # Recheck after blocking reads, including a change made during a read.
                refresh_calibration()
                if event:
                    rejected_reason = 'invalid_sample'
                    try:
                        if abs(time.time() - event['timestamp']) > 10:
                            rejected_reason = 'stale_sample'
                            raise ValueError('USB event timestamp is stale')
                        sample = parse_frame(**event)
                        latest = calibration.update(sample)
                        capture.valid_sample(sample['timestamp'])
                        frame_count = min(COUNTER_MAX, frame_count + 1)
                        error = None
                    except ValueError:
                        capture.count(rejected_reason)
                        rejected = min(COUNTER_MAX, rejected + 1)
                if now >= next_nut and not replay:
                    # This lookup may block for two seconds. Check immediately before
                    # it, then again afterwards, without polling for every USB event.
                    refresh_calibration(force=True)
                    nut_data = nut_snapshot(nut, device)
                    next_nut = now + 30
                    refresh_calibration()
                if time.monotonic() >= next_publish:
                    publish()
            except (OSError, RuntimeError) as exc:
                error = bounded_text(str(exc), 256)
                LOG.warning('Capture unavailable: %s', exc)
                if reader:
                    reader.close()
                reader = None
                publish()
                refresh_calibration(force=True)
                time.sleep(2)
    finally:
        if reader:
            reader.close()
        LOG.info('Stopped; frames=%s rejected=%s dropped=%s', frame_count, rejected, dropped)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', default='/run/ugreen-ups-panel/latest.json')
    parser.add_argument('--serial', default=os.getenv('UPS_SERIAL', ''))
    parser.add_argument('--duration', type=float, default=0)
    parser.add_argument('--replay', help='Explicit demonstration mode; never used in production service')
    parser.add_argument('--nut', default=os.getenv('UPS_NUT_TARGET', 'ups0@localhost'))
    parser.add_argument('--calibration-profile', choices=CALIBRATION_PROFILES,
                        default=os.getenv('UPS_CALIBRATION_PROFILE', 'none'),
                        help='Explicit local empirical calibration; default disables estimates')
    parser.add_argument('--calibration-config', default=os.getenv('UPS_CALIBRATION_CONFIG') or None,
                        help='Optional coefficient configuration file, reloaded without restarting')
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    run(args.output, args.serial, args.duration, args.replay, args.nut, args.calibration_profile,
        args.calibration_config)


if __name__ == '__main__':
    main()
