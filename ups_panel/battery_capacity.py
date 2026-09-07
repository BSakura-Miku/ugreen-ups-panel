"""A fixed, observed SOC-window reference; never nominal capacity or absolute SOH.

Only new continuous 90 -> 80 observations can establish or compare a reference.
The Store owns locking and commits this additive state with its other histories.
Restart deliberately abandons live integration, while retaining every baseline.
"""
import copy
import json
import math
import statistics
import time
from uuid import uuid4

from .battery_energy import evidence
from .power import finite_number


TABLE = 'battery_capacity_state'
ARCHIVE_TABLE = 'battery_capacity_epochs'
MAX_RECENT = 24
MAX_GAP_SEC = 5
FRESH_SEC = 10
CRITERIA = {'soc_start': 90, 'soc_end': 80, 'max_gap_sec': MAX_GAP_SEC,
            'load_tolerance': 0.1, 'max_power_cv': 0.15, 'trend_samples': 3,
            'min_duration_sec': 60}
_UNSET = object()


def _number(value, *, positive=False):
    return value if finite_number(value) and (value > 0 if positive else value >= 0) else None


def _basis(view):
    if not isinstance(view, dict) or not isinstance(view.get('sample'), dict):
        return None, 'invalid_basis'
    _, basis, reason = evidence(view)
    if basis is None:
        return None, reason
    basis = copy.deepcopy(basis)
    device = basis['device']
    # USB addresses and bus paths may change without changing a serialised UPS.
    keys = ('serial', 'vendor', 'product') if device.get('serial') else ('path', 'bus', 'vendor', 'product')
    basis['device'] = {key: device[key] for key in keys if key in device}
    return basis, None


def _empty():
    return {'schema': 1, 'epoch': None, 'baseline': None, 'recent': [],
            'last_ts': None, 'last_mode': None, 'reason': None}


def _soc(sample):
    value = _number(sample.get('soc'))
    return value if value is not None and value <= 100 else None


def _aux(sample):
    voltage = _number(sample.get('battery_voltage'), positive=True)
    delta = _number(sample.get('cell_delta_mv'))
    cells = sample.get('cells')
    if delta is None and isinstance(cells, list) and len(cells) == 4:
        if all(_number(value, positive=True) is not None for value in cells):
            delta = _number((max(cells) - min(cells)) * 1000)
    return voltage, delta


def _comparison(state):
    records = [item for item in reversed(state['recent']) if item['accepted']][:CRITERIA['trend_samples']]
    if not records:
        return None
    index = statistics.median(item['index_pct'] for item in records)
    return {'sample_count': len(records), 'index_pct': index, 'change_pct': index - 100,
            'latest_end_ts': records[0]['end_ts']}


def _prune_recent(records):
    """Keep at most 24 candidates, including the latest three accepted records.

    A run of rejected observations must never erase a previously measured
    comparison and make the display fall back to the baseline's defined 100%.
    Retained records stay chronological so the API and UI select the same trio.
    """
    protected = {item['id'] for item in [record for record in records if record['accepted']]
                 [-CRITERIA['trend_samples']:]}
    while len(records) > MAX_RECENT:
        oldest = next(index for index, item in enumerate(records) if item['id'] not in protected)
        del records[oldest]


class BatteryCapacity:
    def __init__(self, db):
        db.execute('''CREATE TABLE IF NOT EXISTS battery_capacity_state (
                        id INTEGER PRIMARY KEY CHECK(id=1), payload TEXT NOT NULL)''')
        db.execute('''CREATE TABLE IF NOT EXISTS battery_capacity_epochs (
                        epoch_id TEXT PRIMARY KEY, archived_at REAL NOT NULL, payload TEXT NOT NULL)''')
        row = db.execute('SELECT payload FROM battery_capacity_state WHERE id=1').fetchone()
        self.state = json.loads(row[0]) if row else _empty()
        if self.state.get('schema') != 1:
            raise ValueError('Unsupported battery capacity state schema')
        self.active = None
        self.previous = None
        self.blocked = bool(self.state['epoch'])
        self.dirty = False
        self.pending_archives = {}
        if self.blocked:
            # No persisted integration anchor can prove what a downgraded panel
            # or stopped collector observed while this feature was absent.
            self.state['reason'] = 'restart'
            self.dirty = True

    def _break(self, reason):
        self.active = None
        self.previous = None
        self.blocked = bool(self.state['epoch'])
        self.state['reason'] = reason
        self.dirty = True

    def _point(self, sample, power):
        return {'timestamp': sample['timestamp'], 'soc': _soc(sample), 'power': power}

    def _start(self, sample, power):
        voltage, delta = _aux(sample)
        self.active = {'id': uuid4().hex, 'start_ts': sample['timestamp'], 'end_ts': sample['timestamp'],
                       'start_soc': 90, 'end_soc': 90, 'estimate_wh': 0.0, 'duration_sec': 0.0,
                       'avg_power_w': 0.0, '_m2': 0.0, 'min_power_w': power, 'max_power_w': power,
                       'start_battery_voltage_v': voltage, 'end_battery_voltage_v': voltage,
                       'min_battery_voltage_v': voltage, 'max_battery_voltage_v': voltage,
                       'max_cell_delta_mv': delta, 'sample_count': 1, 'interval_count': 0}
        self.state['reason'] = None

    def _integrate(self, sample, power):
        item, previous = self.active, self.previous
        elapsed = sample['timestamp'] - previous['timestamp']
        duration = item['duration_sec'] + elapsed
        interval_mean = previous['power'] / 2 + power / 2
        amount = interval_mean * (elapsed / 3600)
        energy = item['estimate_wh'] + amount
        # Time-weighted variance of the linearly interpolated interval, combined
        # with prior intervals by weighted Welford; sample-count CV would skew
        # the stability gate when report intervals differ.
        try:
            interval_variance = ((power - previous['power']) ** 2) / 12
            difference = interval_mean - item['avg_power_w']
            mean = item['avg_power_w'] + difference * elapsed / duration
            m2 = (item['_m2'] + interval_variance * elapsed +
                  difference ** 2 * item['duration_sec'] * elapsed / duration)
        except OverflowError:
            return False
        if (any(_number(value) is None for value in (duration, mean, m2)) or
                _number(amount, positive=True) is None or _number(energy, positive=True) is None):
            return False
        item.update(end_ts=sample['timestamp'], end_soc=_soc(sample), estimate_wh=energy,
                    duration_sec=duration, avg_power_w=mean, _m2=m2,
                    min_power_w=min(item['min_power_w'], power), max_power_w=max(item['max_power_w'], power),
                    sample_count=item['sample_count'] + 1, interval_count=item['interval_count'] + 1)
        voltage, delta = _aux(sample)
        item['end_battery_voltage_v'] = voltage
        for key, value, function in (('min_battery_voltage_v', voltage, min),
                                     ('max_battery_voltage_v', voltage, max), ('max_cell_delta_mv', delta, max)):
            if value is not None:
                item[key] = value if item[key] is None else function(item[key], value)
        return True

    def _finish(self):
        item = self.active
        variance = max(0.0, item.pop('_m2') / item['duration_sec'])
        item['power_cv'] = math.sqrt(variance) / item['avg_power_w']
        item.update(coverage_ratio=1.0, accepted=True, reason=None, index_pct=None, change_pct=None)
        baseline = self.state['baseline']
        if item['duration_sec'] < CRITERIA['min_duration_sec']:
            item.update(accepted=False, reason='short_window')
        elif item['power_cv'] > CRITERIA['max_power_cv']:
            item.update(accepted=False, reason='unstable_load')
        elif baseline is not None and abs(item['avg_power_w'] / baseline['avg_power_w'] - 1) > CRITERIA['load_tolerance'] + 1e-12:
            item.update(accepted=False, reason='load_mismatch')
        if item['accepted']:
            index = 100.0 if baseline is None else item['estimate_wh'] / baseline['estimate_wh'] * 100
            if not finite_number(index):
                self._break('invalid_energy')
                return
            item.update(index_pct=index, change_pct=index - 100)
        if baseline is None and item['accepted']:
            self.state['baseline'] = item
        else:
            self.state['recent'].append(item)
            _prune_recent(self.state['recent'])
        self.state['reason'] = item['reason']
        self.active = None
        self.previous = None
        self.blocked = True  # One candidate per external-power-separated cycle.

    def ingest(self, view):
        if not isinstance(view, dict) or not view.get('fresh') or not isinstance(view.get('sample'), dict):
            self._break('stale')
            return
        sample = view['sample']
        ts = _number(sample.get('timestamp'))
        if ts is None:
            self._break('invalid_timestamp')
            return
        last_ts = self.state['last_ts']
        if last_ts is not None and ts <= last_ts:
            if ts < last_ts:
                self._break('timestamp_rollback')
            return
        self.dirty = True
        self.state.update(last_ts=ts, last_mode=sample.get('mode'))
        basis, reason = _basis(view)
        if basis is None:
            self._break(reason)
            return
        if self.state['epoch'] is None:
            self.state['epoch'] = {'id': uuid4().hex, 'activated_at': ts, 'basis': basis}
            self.blocked = False
        elif basis != self.state['epoch']['basis']:
            self._break('context_changed')
            return
        mode = sample.get('mode')
        if mode in ('online', 'charging'):
            self.state['reason'] = 'external_before_end' if self.active else self.state['reason']
            self.active = None
            self.previous = None
            self.blocked = False
            return
        if mode != 'battery':
            self._break('unknown_mode')
            return
        if last_ts is not None and ts - last_ts > MAX_GAP_SEC:
            self._break('sample_gap')
            return
        if self.blocked:
            if self.state['reason'] is None:
                self.state['reason'] = 'waiting_external'
            return
        soc = _soc(sample)
        if soc is None:
            self._break('invalid_soc')
            return
        raw = _number(sample.get('battery_discharge_power_candidate_w'), positive=True)
        try:
            power = None if raw is None else _number(raw * basis['battery_gain'], positive=True)
        except OverflowError:
            power = None
        if power is None:
            self._break('invalid_power')
            return
        previous = self.previous
        if previous is None:
            if soc <= 90:
                self._break('missed_start')
            else:
                self.previous = self._point(sample, power)
                self.state['reason'] = 'waiting_start'
            return
        if soc > previous['soc']:
            self._break('soc_rebound')
            return
        if self.active is None:
            if soc < 90:
                self._break('missed_start')
                return
            if soc == 90 and previous['soc'] > 90:
                if previous['soc'] - soc > 1:
                    self._break('soc_jump')
                    return
                self._start(sample, power)
            self.previous = self._point(sample, power)
            return
        if soc < 80:
            self._break('missed_end')
            return
        if previous['soc'] - soc > 1:
            self._break('soc_jump')
            return
        if not self._integrate(sample, power):
            self._break('invalid_energy')
            return
        self.previous = self._point(sample, power)
        if soc == 80:
            self._finish()

    def write(self, db):
        if self.dirty:
            payload = json.dumps(self.state, sort_keys=True, allow_nan=False)
            db.execute('INSERT OR REPLACE INTO battery_capacity_state VALUES(1,?)', (payload,))
        db.executemany('INSERT OR REPLACE INTO battery_capacity_epochs VALUES(?,?,?)',
                       [(key, archived_at, payload) for key, (archived_at, payload) in self.pending_archives.items()])

    def committed(self):
        self.dirty = False
        self.pending_archives.clear()

    def reset(self, view, expected_epoch_id=_UNSET):
        epoch = self.state['epoch']
        if expected_epoch_id is not _UNSET and expected_epoch_id != (epoch['id'] if epoch else None):
            raise ValueError('reference_changed')
        if not isinstance(view, dict) or not view.get('fresh') or not isinstance(view.get('sample'), dict):
            raise ValueError('stale')
        ts = _number(view['sample'].get('timestamp'))
        if ts is None:
            raise ValueError('invalid_timestamp')
        server_time = view.get('server_time', ts)
        if not finite_number(server_time) or not 0 <= server_time - ts <= FRESH_SEC:
            raise ValueError('stale')
        basis, reason = _basis(view)
        if basis is None:
            raise ValueError(reason)
        if epoch is not None:
            archive = {'schema': 1, 'epoch': epoch, 'baseline': self.state['baseline'],
                       'comparison': _comparison(self.state),
                       'recent': [item for item in self.state['recent'] if item['accepted']]}
            self.pending_archives[epoch['id']] = (server_time, json.dumps(archive, sort_keys=True, allow_nan=False))
        self.state = _empty()
        self.active = None
        self.previous = None
        self.blocked = False
        self.ingest(view)
        return self.snapshot(view, now=server_time)

    def snapshot(self, view, now=None):
        now = time.time() if now is None else now
        sample = view.get('sample') if isinstance(view, dict) else None
        sample = sample if isinstance(sample, dict) else {}
        ts = _number(sample.get('timestamp'))
        fresh = bool(isinstance(view, dict) and view.get('fresh') and ts is not None and 0 <= now - ts <= FRESH_SEC)
        basis, basis_reason = _basis(view)
        epoch, baseline = self.state['epoch'], self.state['baseline']
        incompatible = epoch is not None and basis is not None and basis != epoch['basis']
        reason = self.state['reason']
        active = self.active
        if not fresh:
            reason, active = 'stale', None
        elif basis is None:
            reason, active = basis_reason, None
        elif incompatible:
            reason, active = 'context_changed', None
        elif ts != self.state['last_ts']:
            reason, active = 'not_observed', None
        elif active is not None and now - active['end_ts'] > MAX_GAP_SEC:
            reason, active = 'sample_gap', None
        comparison = _comparison(self.state)
        status = ('not_configured' if basis is None and basis_reason == 'not_configured' else
                  'incompatible' if incompatible else 'collecting' if active else
                  'trend' if comparison and comparison['sample_count'] >= CRITERIA['trend_samples'] else
                  'preliminary' if comparison else 'reference_ready' if baseline else 'waiting_reference')
        progress = None if active is None else {
            'start_ts': active['start_ts'], 'current_soc': active['end_soc'],
            'completed_pp': 90 - active['end_soc'], 'target_drop_pp': 10,
            'estimate_wh': active['estimate_wh'], 'duration_sec': active['duration_sec']}
        return copy.deepcopy({'schema': 1, 'status': status, 'reason': reason, 'epoch': epoch,
                              'current_basis': basis, 'baseline': baseline, 'comparison': comparison,
                              'progress': progress, 'recent': list(reversed(self.state['recent'])),
                              'criteria': CRITERIA, 'temperature_known': False, 'capture_fresh': fresh,
                              'sample_timestamp': ts, 'generated_at': now})
