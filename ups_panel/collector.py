"""Passive collector with atomic snapshot publishing and optional read-only NUT lookup."""
import argparse
import json
import logging
import os
from pathlib import Path
import signal
import subprocess
import tempfile
import time

from .protocol import parse_frame
from .power import CALIBRATION_PROFILES, PowerEstimator
from .usbmon import Reader, decode_event, discover

LOG = logging.getLogger('collector')


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


def nut_snapshot(target):
    try:
        result = subprocess.run(['/usr/bin/upsc', target], capture_output=True, text=True, timeout=2)
        if result.returncode:
            return {'available': False, 'timestamp': time.time(), 'error': 'NUT 查询失败'}
        allow = {'ups.status', 'battery.charge', 'battery.runtime', 'input.voltage', 'output.voltage', 'ups.load', 'driver.version', 'driver.version.data'}
        values = dict(line.split(': ', 1) for line in result.stdout.splitlines() if ': ' in line)
        return {'available': True, 'timestamp': time.time(), 'values': {k: v for k, v in values.items() if k in allow}}
    except (OSError, subprocess.TimeoutExpired):
        return {'available': False, 'timestamp': time.time(), 'error': 'NUT 查询不可用'}


def run(output, serial='', duration=0, replay=None, nut='ups0@localhost', calibration_profile='none'):
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
    estimator = PowerEstimator(calibration_profile)
    frame_count = rejected = dropped = 0
    next_discovery = next_publish = next_nut = next_replay = 0
    nut_data = {'available': False}
    error = None
    fixtures = [bytes.fromhex(line) for line in Path(replay).read_text().splitlines() if line.strip()] if replay else []
    if replay and not fixtures:
        raise ValueError('Replay fixture contains no reports')
    if duration < 0:
        raise ValueError('Duration must be non-negative')
    try:
        while not stop and (not duration or time.monotonic() - started < duration):
            now = time.monotonic()
            try:
                if not replay and now >= next_discovery:
                    found = discover(serial=serial)
                    if found != device:
                        if reader:
                            reader.close()
                        reader = None
                        estimator = PowerEstimator(calibration_profile)
                        device = found
                        latest = None
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
                        event = decode_event(*packet, device['bus'], device['device'])
                else:
                    time.sleep(.5)
                if event:
                    try:
                        sample = parse_frame(**event)
                        if abs(time.time() - sample['timestamp']) > 10:
                            raise ValueError('USB event timestamp is stale')
                        latest = estimator.update(sample)
                        frame_count += 1
                        error = None
                    except ValueError:
                        rejected += 1
                if now >= next_nut and not replay:
                    nut_data = nut_snapshot(nut)
                    next_nut = now + 30
                if now >= next_publish:
                    if reader:
                        _, lost = reader.stats()
                        dropped += lost
                    atomic_json(output, {'schema': 1, 'source': 'replay' if replay else 'usbmon',
                        'heartbeat': time.time(), 'device': device, 'sample': latest, 'nut': nut_data,
                        'diagnostics': {'frames': frame_count, 'rejected': rejected, 'dropped': dropped,
                                        'uptime_sec': round(now - started), 'error': error}})
                    next_publish = now + 2
            except (OSError, RuntimeError) as exc:
                error = str(exc)
                LOG.warning('Capture unavailable: %s', exc)
                if reader:
                    reader.close()
                reader = None
                atomic_json(output, {'schema': 1, 'source': 'usbmon', 'heartbeat': time.time(),
                    'device': device, 'sample': latest, 'nut': nut_data,
                    'diagnostics': {'frames': frame_count, 'rejected': rejected, 'dropped': dropped, 'error': error}})
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
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    run(args.output, args.serial, args.duration, args.replay, args.nut, args.calibration_profile)


if __name__ == '__main__':
    main()
