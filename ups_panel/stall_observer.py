"""Opt-in, bounded stall evidence; never controls USB, NUT or host services."""
import argparse
from collections import deque
import json
import math
import os
from pathlib import Path
import secrets
import struct
import threading
import time
from urllib.request import ProxyHandler, build_opener


def number(value):
    return value if type(value) in (int, float) and math.isfinite(value) else None


def read_json(path):
    with Path(path).open('rb') as stream:
        raw = stream.read(65537)
    if len(raw) > 65536:
        raise ValueError('snapshot too large')
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError('invalid snapshot')
    return value


def process(pid):
    result = {'pid': pid}
    try:
        root = Path('/proc') / str(pid)
        fields = (root / 'stat').read_text().rsplit(')', 1)[1].split()
        result.update(state=fields[0], cpu_ticks=int(fields[11]) + int(fields[12]), start_ticks=int(fields[19]),
                      schedstat=[int(v) for v in (root / 'schedstat').read_text().split()[:3]])
    except (OSError, ValueError, IndexError):
        result['state'] = 'unavailable'
    return result


def snapshot_row(path, pid, wall=None, mono=None):
    row = {'wall': time.time() if wall is None else wall, 'mono': time.monotonic() if mono is None else mono,
           'collector': process(pid)}
    try:
        value = read_json(path)
        sample = value.get('sample') or {}
        diag = value.get('diagnostics') or {}
        row.update(heartbeat=number(value.get('heartbeat')), sample_time=number(sample.get('timestamp')),
                   frames=number(diag.get('frames')), dropped=number(diag.get('dropped')))
    except (OSError, ValueError, TypeError, AttributeError):
        row['snapshot_error'] = True
    return row


def reasons(row, previous=None):
    """Evidence labels are observations, not a claim of a hardware root cause."""
    result = []
    if previous and abs((row['wall'] - previous['wall']) - (row['mono'] - previous['mono'])) > 2:
        result.append('wall_clock_changed')
    for key in ('heartbeat', 'sample_time'):
        stamp = number(row.get(key))
        if stamp is None:
            result.append(key + '_missing')
        elif row['wall'] - stamp > 10:
            result.append(key + '_stale')
        elif stamp - row['wall'] > 2:
            result.append(key + '_future')
    api = row.get('api') or {}
    if api.get('error'):
        result.append('api_unavailable')
    if api.get('duration', 0) > 1:
        result.append('api_slow')
    if number(api.get('last_success')) is not None and row['wall'] - api['last_success'] > 10:
        result.append('writer_stale')
    usb = row.get('usb') or {}
    if usb.get('error') or usb.get('dropped', 0):
        result.append('usb_observer_incomplete')
    if number(usb.get('accepted_mono')) is not None and row['mono'] - usb['accepted_mono'] > 10:
        result.append('usb_completion_gap')
    if previous and row['collector'] != previous['collector'] and (
            row['collector'].get('start_ticks') != previous['collector'].get('start_ticks')):
        result.append('collector_identity_changed')
    return result


class EvidenceWindow:
    def __init__(self, directory, before=30, after=15, cooldown=60, max_files=20, max_bytes=2 * 1024 * 1024):
        self.directory = Path(directory)
        self.before = deque(maxlen=before)
        self.after, self.cooldown = after, cooldown
        self.max_files, self.max_bytes = max_files, max_bytes
        self.pending = None
        self.remaining = 0
        self.next_allowed = 0
        self.previous = None

    def ingest(self, row):
        row = dict(row, reasons=reasons(row, self.previous))
        self.previous = row
        if self.pending is not None:
            self.pending.append(row)
            self.remaining -= 1
            if self.remaining <= 0:
                self.flush()
        elif row['reasons'] and row['mono'] >= self.next_allowed:
            self.pending = [*self.before, row]
            self.remaining = self.after
            self.next_allowed = row['mono'] + self.cooldown
        self.before.append(row)

    def flush(self):
        if self.pending is None:
            return
        raw = json.dumps({'schema': 1, 'rows': self.pending}, allow_nan=False, separators=(',', ':')).encode()
        if len(raw) > self.max_bytes:
            raise ValueError('evidence size limit exceeded')
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        # Only this tool's uniquely named files are eligible for retention cleanup.
        files = sorted(self.directory.glob('stall-*.json'), key=lambda p: p.lstat().st_mtime)
        for path in files:
            if not path.is_symlink() and (len(files) >= self.max_files or time.time() - path.stat().st_mtime > 7 * 86400):
                path.unlink()
                files = [item for item in files if item != path]
        path = self.directory / ('stall-' + str(time.time_ns()) + '-' + secrets.token_hex(4) + '.json')
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'wb') as stream:
            stream.write(raw)
        os.chmod(path, 0o600)
        self.pending = None


class Probes:
    def __init__(self, port, bus=None, device=None):
        self.port, self.bus, self.device = port, bus, device
        self.stop = threading.Event()
        self.lock = threading.Lock()
        self.api, self.usb = {}, {}
        self.events = deque(maxlen=128)
        self.threads = []

    def start(self):
        for target in ([self.poll_api, self.poll_usb] if self.bus is not None else [self.poll_api]):
            thread = threading.Thread(target=target, daemon=True)
            thread.start()
            self.threads.append(thread)

    def poll_api(self):
        opener = build_opener(ProxyHandler({}))
        while not self.stop.is_set():
            started = time.monotonic()
            try:
                with opener.open(f'http://127.0.0.1:{self.port}/api/health', timeout=2) as response:
                    raw = response.read(65537)
                if len(raw) > 65536:
                    raise ValueError('response too large')
                data = json.loads(raw)
                value = {'last_success': number(data['storage']['last_success'])}
            except (OSError, ValueError, KeyError, TypeError):
                value = {'error': True}
            value.update(duration=time.monotonic() - started, checked_mono=time.monotonic())
            with self.lock:
                self.api = value
            self.stop.wait(5)

    def poll_usb(self):
        from .usbmon import Reader, classify_event
        reader = None
        try:
            reader = Reader(self.bus)
            while not self.stop.is_set():
                packet = reader.read(0.2)
                if packet:
                    header, payload = packet
                    if len(header) == 64 and header[11] == self.device and struct.unpack_from('=H', header, 12)[0] == self.bus:
                        accepted = classify_event(header, payload, self.bus, self.device) == 'accepted'
                        event = {'mono': time.monotonic(), 'kind': header[8], 'transfer_type': header[9],
                                 'endpoint': header[10], 'kernel_sec': struct.unpack_from('=q', header, 16)[0],
                                 'kernel_usec': struct.unpack_from('=i', header, 24)[0],
                                 'status': struct.unpack_from('=i', header, 28)[0], 'accepted': accepted}
                        with self.lock:
                            if accepted:
                                self.usb['accepted_mono'] = event['mono']
                            if len(self.events) == self.events.maxlen:
                                self.usb['observer_evicted'] = self.usb.get('observer_evicted', 0) + 1
                            self.events.append(event)
                queued, dropped = reader.stats()
                with self.lock:
                    self.usb.update(queued=queued, dropped=self.usb.get('dropped', 0) + dropped)
        except OSError:
            with self.lock:
                self.usb['error'] = True
        finally:
            if reader:
                reader.close()

    def attach(self, row):
        with self.lock:
            row.update(api=dict(self.api), usb=dict(self.usb), usb_events=list(self.events))
            self.events.clear()

    def close(self):
        self.stop.set()
        for thread in self.threads:
            thread.join(timeout=3)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--enable', action='store_true', help='explicitly start observation')
    parser.add_argument('--snapshot', default='/run/ugreen-ups-panel/latest.json')
    parser.add_argument('--output', default='stall-evidence')
    parser.add_argument('--pid', type=int, required=True, help='collector PID; identity changes are recorded')
    parser.add_argument('--port', type=int, default=8080, help='local panel HTTP port')
    parser.add_argument('--duration', type=int, default=3600, help='seconds, at most 72 hours')
    parser.add_argument('--usb', action='store_true', help='observe only snapshot device via passive usbmon')
    args = parser.parse_args()
    if not args.enable or not 1 <= args.duration <= 259200 or not 1 <= args.port <= 65535 or args.pid <= 0:
        parser.error('--enable, a positive PID, valid port and duration 1..259200 are required')
    bus = device = None
    if args.usb:
        identity = read_json(args.snapshot)['device']
        bus, device = identity['bus'], identity['device']
        if type(bus) is not int or type(device) is not int or not 1 <= bus <= 65535 or not 1 <= device <= 127:
            parser.error('invalid snapshot USB identity')
    probes = Probes(args.port, bus, device)
    window = EvidenceWindow(args.output)
    probes.start()
    started, cpu = time.monotonic(), time.process_time()
    try:
        while time.monotonic() - started < args.duration:
            row = snapshot_row(args.snapshot, args.pid)
            row['observer_cpu_sec'] = time.process_time() - cpu
            probes.attach(row)
            window.ingest(row)
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        probes.close()
        window.flush()
    print(json.dumps({'elapsed_sec': time.monotonic() - started, 'cpu_sec': time.process_time() - cpu}))


if __name__ == '__main__':
    main()
