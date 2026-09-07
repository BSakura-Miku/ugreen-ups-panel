"""Read-only collector diagnosis. No service, module, USB or NUT configuration writes."""
import argparse
import ipaddress
import json
import math
import os
from pathlib import Path
import re
import shlex
import time

from .build_info import get_build_info
from .collector import (CAPTURE_COUNTERS, COUNTER_MAX, CaptureDiagnostics,
                        host_metadata, nut_snapshot, usb_metadata)
from .protocol import parse_frame
from .usbmon import Reader, decode_event, discover

SNAPSHOT_LIMIT = 65536
MODES = ('online', 'charging', 'battery', 'unknown')
STATUS_TOKENS = frozenset(('OL', 'OB', 'LB', 'HB', 'RB', 'CHRG', 'DISCHRG', 'BYPASS',
                           'CAL', 'OFF', 'OVER', 'TRIM', 'BOOST', 'FSD', 'ALARM'))
CAPTURE_STATES = frozenset(('waiting', 'unavailable', 'no_target_activity', 'no_interrupt_in',
                            'no_complete_report', 'invalid_reports', 'fresh', 'stale', 'replay'))
NUT_ERRORS = frozenset(('invalid_target', 'query_failed', 'response_too_large', 'upsc_missing',
                       'query_timeout', 'query_unavailable'))


def finite(value):
    try:
        return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
    except OverflowError:
        return False


def numeric_version(value):
    """Export numeric version components, not arbitrary vendor/host suffixes."""
    if not isinstance(value, str):
        return None
    try:
        ipaddress.ip_address(value.strip())
        return None
    except ValueError:
        pass
    match = re.match(r'^[0-9]{1,5}(?:\.[0-9]{1,5}){0,3}(?:_[0-9]{1,5}(?:\.[0-9]{1,5})*)?', value[:64])
    if not match or value[match.end():].startswith(':'):
        return None
    candidate = match.group()
    try:
        ipaddress.ip_address(candidate)
        return None
    except ValueError:
        return candidate


def choice(value, allowed, default=None):
    return value if isinstance(value, str) and value in allowed else default


def safe_build(value):
    if not isinstance(value, dict):
        return None
    result = {'version': numeric_version(value.get('version')), 'revision': None, 'source_sha256': None}
    for key, pattern in (('revision', r'[0-9a-f]{7,40}'), ('source_sha256', r'[0-9a-f]{64}')):
        raw = value.get(key)
        if isinstance(raw, str) and re.fullmatch(pattern, raw):
            result[key] = raw
    return result


def read_settings(path=Path('/etc/ugreen-ups-panel.env')):
    """Read only this project's two settings; never source a shell or read ups.conf."""
    try:
        with Path(path).open() as stream:
            text = stream.read(SNAPSHOT_LIMIT + 1)
        if len(text) > SNAPSHOT_LIMIT:
            return {}
    except (OSError, UnicodeError):
        return {}
    values = {}
    for line in text.splitlines():
        key, separator, raw = line.partition('=')
        key = key.strip()
        if not separator or key not in ('UPS_NUT_TARGET', 'UPS_SERIAL'):
            continue
        try:
            parsed = shlex.split(raw, comments=True)
            if len(parsed) == 1 and len(parsed[0]) <= 256 and all(c.isprintable() for c in parsed[0]):
                values[key] = parsed[0]
        except ValueError:
            continue
    return values


def read_snapshot(path):
    def reject_constant(_):
        raise ValueError('Invalid JSON constant')
    try:
        with Path(path).open('rb') as source:
            encoded = source.read(SNAPSHOT_LIMIT + 1)
        if len(encoded) > SNAPSHOT_LIMIT:
            return None, 'snapshot_too_large'
        value = json.loads(encoded, parse_constant=reject_constant)
        if not isinstance(value, dict):
            return None, 'snapshot_invalid'
        return value, None
    except FileNotFoundError:
        return None, 'snapshot_missing'
    except (OSError, ValueError, UnicodeError, RecursionError):
        return None, 'snapshot_unreadable_or_invalid'


def private_status(snapshot, device, now, error=None):
    result = {'status': 'missing', 'source': 'snapshot', 'reason': error or 'snapshot_missing',
              'sample_age_sec': None, 'heartbeat_age_sec': None, 'mode': None,
              'device_association': 'unverified'}
    if not isinstance(snapshot, dict):
        return result
    if type(snapshot.get('schema')) is not int or snapshot['schema'] != 1:
        return {**result, 'status': 'invalid', 'reason': 'snapshot_schema_invalid'}
    if snapshot.get('source') != 'usbmon':
        return {**result, 'status': 'not_live', 'reason': 'snapshot_not_usbmon'}
    sample = snapshot.get('sample')
    if sample is None:
        return {**result, 'status': 'missing', 'reason': 'snapshot_has_no_sample'}
    if not isinstance(sample, dict):
        return {**result, 'status': 'invalid', 'reason': 'snapshot_sample_invalid'}
    timestamp, heartbeat = sample.get('timestamp'), snapshot.get('heartbeat')
    cells = sample.get('cells')
    if (not finite(timestamp) or timestamp < 0 or not finite(heartbeat) or heartbeat < 0
            or sample.get('mode') not in MODES or not finite(sample.get('soc'))
            or not 0 <= sample['soc'] <= 100 or not isinstance(cells, list) or len(cells) != 4
            or any(not finite(v) or not 1 <= v <= 5 for v in cells)):
        return {**result, 'status': 'invalid', 'reason': 'snapshot_sample_invalid'}
    sample_age, heartbeat_age = now - timestamp, now - heartbeat
    result.update(mode=sample['mode'],
                  sample_age_sec=round(sample_age, 1) if sample_age >= 0 else None,
                  heartbeat_age_sec=round(heartbeat_age, 1) if heartbeat_age >= 0 else None)
    stored_device = snapshot.get('device')
    if device and isinstance(stored_device, dict):
        keys = ('bus', 'device', 'serial', 'path')
        if all(stored_device.get(key) == device.get(key) for key in keys):
            result['device_association'] = 'matched'
        else:
            return {**result, 'status': 'device_mismatch', 'reason': 'snapshot_device_changed',
                    'device_association': 'different'}
    if sample_age < 0 or heartbeat_age < 0:
        return {**result, 'status': 'stale', 'reason': 'snapshot_future_timestamp'}
    if sample_age > 10 or heartbeat_age > 10:
        return {**result, 'status': 'stale', 'reason': 'snapshot_stale'}
    return {**result, 'status': 'fresh', 'reason': None}


def safe_capture(value):
    if not isinstance(value, dict) or type(value.get('schema')) is not int or value['schema'] != 1:
        return None
    result = {'schema': 1, 'state': choice(value.get('state'), CAPTURE_STATES, 'unavailable'),
              'window_sec': 60, 'counters': {}, 'recent_counters': {}}
    for section in ('counters', 'recent_counters'):
        raw = value.get(section)
        raw = raw if isinstance(raw, dict) else {}
        result[section] = {key: raw[key] if type(raw.get(key)) is int and 0 <= raw[key] <= COUNTER_MAX else 0
                           for key in CAPTURE_COUNTERS}
    return result


def safe_nut(value, now):
    """Shareable fields only: no target, serial, paths, alarms or complete raw values."""
    value = value if isinstance(value, dict) else {}
    values = value.get('values') if isinstance(value.get('values'), dict) else {}
    def raw(key):
        item = values.get(key)
        return item if isinstance(item, str) and len(item) <= 256 else ''
    stamp = value.get('timestamp')
    age = now - stamp if finite(stamp) and 0 <= stamp <= now else None
    association = value.get('association')
    association = association if isinstance(association, dict) else {}
    pollonly = value.get('pollonly')
    pollonly = pollonly if isinstance(pollonly, dict) else {}
    source = raw('driver.parameter.subdriver') or raw('driver.version.data')
    subdriver = 'Arduino' if re.fullmatch(r'Arduino(?: HID [0-9]+(?:\.[0-9]+)*)?', source, re.I) else None
    return {'available': value.get('available') is True,
            'error_code': choice(value.get('error_code'), NUT_ERRORS),
            'timestamp_kind': 'query_completed',
            'query_age_sec': round(age, 1) if age is not None else None,
            'fresh': value.get('available') is True and age is not None and age <= 45,
            'association': choice(association.get('status'), ('matched', 'different', 'unverified'), 'unverified'),
            'pollonly': choice(pollonly.get('state'), ('enabled', 'disabled', 'unknown'), 'unknown'),
            'driver_name': 'usbhid-ups' if raw('driver.name') == 'usbhid-ups' else None,
            'driver_version': numeric_version(raw('driver.version')), 'subdriver': subdriver,
            'ups_firmware': numeric_version(raw('ups.firmware')),
            'ups_firmware_aux': numeric_version(raw('ups.firmware.aux')),
            'status_tokens': sorted(set(raw('ups.status').split()) & STATUS_TOKENS),
            'alarm_present': bool(raw('ups.alarm')),
            'runtime_is_65535': raw('battery.runtime').strip() == '65535'}


def passive_observation(device, seconds):
    """Opt-in, bounded reading of an existing usbmon node. Never opens USB devices."""
    capture = CaptureDiagnostics()
    started = time.monotonic()
    reader, latest, code = None, None, None
    events = 0
    try:
        reader = Reader(device['bus'])
        deadline = started + seconds
        while time.monotonic() < deadline and events < 12000:
            packet = reader.read(min(.5, max(0, deadline - time.monotonic())))
            if not packet:
                continue
            events += 1
            if capture.observe(*packet, device['bus'], device['device']) != 'accepted':
                continue
            event = decode_event(*packet, device['bus'], device['device'])
            if abs(time.time() - event['timestamp']) > 10:
                capture.count('stale_sample')
                continue
            try:
                latest = parse_frame(**event)
                capture.valid_sample(latest['timestamp'])
            except ValueError:
                capture.count('invalid_sample')
        if events >= 12000:
            code = 'observation_event_limit'
    except (OSError, RuntimeError):
        code = 'usbmon_read_unavailable'
    finally:
        if reader:
            reader.close()
    return latest, capture.snapshot(available=code != 'usbmon_read_unavailable'), {
        'status': 'error' if code == 'usbmon_read_unavailable' else 'completed',
        'duration_sec': round(time.monotonic() - started, 2), 'code': code}


def diagnose(snapshot_path=Path('/run/ugreen-ups-panel/latest.json'), target=None, serial=None,
             observe_seconds=0, settings_path=Path('/etc/ugreen-ups-panel.env')):
    if not finite(observe_seconds) or not 0 <= observe_seconds <= 60:
        raise ValueError('Observation duration must be between 0 and 60 seconds')
    snapshot, snapshot_error = read_snapshot(snapshot_path)
    settings = read_settings(settings_path)
    snapshot_nut = snapshot.get('nut', {}) if isinstance(snapshot, dict) else {}
    snapshot_nut = snapshot_nut if isinstance(snapshot_nut, dict) else {}
    target = (target if target is not None else os.getenv('UPS_NUT_TARGET')
              or settings.get('UPS_NUT_TARGET') or snapshot_nut.get('target') or 'ups0@localhost')
    serial = serial if serial is not None else os.getenv('UPS_SERIAL') or settings.get('UPS_SERIAL', '')
    host = host_metadata()
    device, discovery = None, 'unavailable'
    try:
        device = discover(serial=serial)
        discovery = 'found' if device else 'not_found'
    except RuntimeError:
        discovery = 'ambiguous'
    except OSError:
        pass
    usb = usb_metadata(device, discovery)
    nut = nut_snapshot(target, device)
    now = time.time()
    private = private_status(snapshot, device, now, snapshot_error)
    diagnostics = snapshot.get('diagnostics', {}) if isinstance(snapshot, dict) else {}
    diagnostics = diagnostics if isinstance(diagnostics, dict) else {}
    capture = safe_capture(diagnostics.get('capture'))
    observation = {'status': 'not_requested', 'duration_sec': 0, 'code': None}
    if observe_seconds:
        if private['status'] == 'fresh':
            observation['status'] = 'used_existing_snapshot'
        elif not device or not usb['usbmon_readable']:
            observation.update(status='unavailable', code='usbmon_not_ready')
        else:
            latest, raw_capture, observation = passive_observation(device, observe_seconds)
            capture = safe_capture(raw_capture)
            if latest:
                now = time.time()
                private = private_status({'schema': 1, 'source': 'usbmon', 'heartbeat': now,
                                          'sample': latest, 'device': device}, device, now)
                private['source'] = 'passive_observation'
    collector = snapshot.get('collector', {}) if isinstance(snapshot, dict) else {}
    collector = collector if isinstance(collector, dict) else {}
    bcd = usb.get('bcd_device')
    usb_version = bcd if isinstance(bcd, str) and re.fullmatch('[0-9a-fA-F]{4}', bcd) else None
    safe_host = {'system': host['system'] if host['system'] in ('Linux', 'Darwin', 'Windows') else 'Other',
                 'kernel_version': numeric_version(host['kernel_release']),
                 'python_version': numeric_version(host['python_version']),
                 **{key: host[key] is True for key in ('python_supported', 'systemd_available',
                                                     'usbmon_module_loaded', 'upsc_available')}}
    return {'schema': 1, 'read_only': True, 'generated_at': time.time(),
            'build': safe_build(get_build_info()), 'collector_build': safe_build(collector.get('build')),
            'host': safe_host,
            'usb': {'discovery': discovery, 'vendor_id': '2b89', 'product_id': 'ffff',
                    'bcd_device': usb_version, 'usbmon_node_exists': usb['usbmon_node_exists'],
                    'usbmon_readable': usb['usbmon_readable']},
            'nut': safe_nut(nut, time.time()), 'private_telemetry': private,
            'capture': capture, 'observation': observation,
            'checks': {'linux_host': 'ok' if host['system'] == 'Linux' else 'unavailable',
                       'python': 'ok' if host['python_supported'] else 'unavailable',
                       'systemd': 'ok' if host['systemd_available'] else 'unavailable',
                       'usb': discovery, 'usbmon': 'ok' if usb['usbmon_readable'] else 'unavailable',
                       'nut_query': 'ok' if nut.get('available') is True else 'unavailable',
                       'private_report': private['status']}}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--json', action='store_true', help='JSON is the default output format')
    parser.add_argument('--snapshot', type=Path, default=Path('/run/ugreen-ups-panel/latest.json'))
    parser.add_argument('--nut', help='Existing NUT target to query, without changing configuration')
    parser.add_argument('--serial', help='Select an already connected UPS; omitted from the report')
    parser.add_argument('--observe-seconds', type=float, default=0,
                        help='If the snapshot is not fresh, passively observe an existing usbmon node for 0–60 seconds')
    args = parser.parse_args(argv)
    if not finite(args.observe_seconds) or not 0 <= args.observe_seconds <= 60:
        parser.error('--observe-seconds must be between 0 and 60')
    report = diagnose(args.snapshot, args.nut, args.serial, args.observe_seconds)
    print(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
