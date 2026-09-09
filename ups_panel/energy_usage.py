"""Observed AC-input energy, booked into a permanent UTC+08:00 hourly ledger.

The existing AC estimate is integrated without applying any new coefficients.
Hours retain their original measurement basis; all day/month totals read that
same ledger. Missing observations never become a zero reading or extrapolation.
Store owns the lock and the transaction around write -> commit -> committed.
"""
import calendar
import copy
from datetime import date as Date, datetime, time as DayTime, timedelta, timezone
import hashlib
import json
import re
import time
from uuid import uuid4

from .battery_energy import calibration_rejected, device_identity
from .calibration import CalibrationError, normalize_config
from .power import finite_number


TZ = timezone(timedelta(hours=8))
MAX_GAP_SEC = 5
FRESH_SEC = 10
MAX_PENDING_BUCKETS = 4096
STATE_TABLE = 'energy_usage_state'
HOUR_TABLE = 'energy_usage_hours'


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)


def _nonnegative(value):
    return finite_number(value) and value >= 0


def _timestamp(value):
    if not _nonnegative(value):
        return False
    try:
        return 1970 <= datetime.fromtimestamp(value, TZ).year <= 9998
    except (OSError, OverflowError, ValueError):
        return False


def _sum(a, b):
    value = a + b
    if not _nonnegative(value):
        raise ValueError('energy_overflow')
    return value


def _date(value):
    if not isinstance(value, str) or not re.fullmatch(r'[0-9]{4}-[0-9]{2}-[0-9]{2}', value):
        raise ValueError('invalid_date')
    try:
        result = Date.fromisoformat(value)
    except ValueError:
        raise ValueError('invalid_date') from None
    if not 1970 <= result.year <= 9998:
        raise ValueError('invalid_date')
    return result


def _month(value):
    if not isinstance(value, str) or not re.fullmatch(r'[0-9]{4}-[0-9]{2}', value):
        raise ValueError('invalid_month')
    try:
        return _date(value + '-01')
    except ValueError:
        raise ValueError('invalid_month') from None


def _start(day):
    return datetime.combine(day, DayTime.min, TZ).timestamp()


def _basis(view):
    """Validate the collector's AC model provenance, independently of battery_gain."""
    if calibration_rejected(view):
        return None, None, 'invalid_basis'
    sample = view['sample']
    source = view.get('source')
    device = device_identity(view.get('device'))
    if not isinstance(source, str) or not 0 < len(source) <= 128 or device is None:
        return None, None, 'invalid_basis'
    for field in ('decoder_version', 'formula_version'):
        if type(sample.get(field)) is not int or sample[field] < 1:
            return None, None, 'invalid_basis'
    keys = ('serial', 'vendor', 'product') if device.get('serial') else ('path', 'bus', 'vendor', 'product')
    stable_device = {key: device[key] for key in keys if key in device}
    config = {'schema': sample.get('calibration_schema', 1),
              'profile': sample.get('calibration_profile'),
              'coefficients': sample.get('calibration_coefficients')}
    if 'ac_voltage_nominal_v' in sample:
        config['ac_voltage_nominal_v'] = sample['ac_voltage_nominal_v']
    if ('calibration_coefficients' not in sample or 'ac_estimate_model' not in sample or
            config['profile'] != 'none' and not isinstance(config['coefficients'], dict)):
        return None, None, 'invalid_basis'
    try:
        config = normalize_config(config)
    except (CalibrationError, TypeError, ValueError, OverflowError):
        return None, None, 'invalid_basis'
    if sample.get('calibration_revision') != config['revision']:
        return None, None, 'invalid_basis'
    profile, revision = config['profile'], config['revision']
    expected_model = (f'us3000_custom_v2_{revision}' if config['schema'] == 2 else
                      f'us3000_19v_custom_{revision}' if profile == 'custom' else
                      'us3000_19v_v1' if profile == 'local-19v-v1' else None)
    if sample['ac_estimate_model'] != expected_model:
        return None, None, 'invalid_basis'
    mode = sample.get('mode')
    if mode not in ('online', 'charging', 'battery'):
        return None, None, 'unknown_mode'
    if mode == 'battery':
        # The observed supply mode establishes zero AC input even when no
        # empirical power model is configured. Battery Wh is never added here.
        power = 0.0
    else:
        if profile == 'none':
            return None, None, 'not_configured'
        if mode == 'charging' and config['coefficients']['charge_gain'] is None:
            return None, None, 'charge_not_configured'
        quality = sample.get('ac_estimate_quality')
        allowed = ('custom_unverified',) if profile == 'custom' else ('calibrated_range', 'extrapolated')
        if quality not in allowed:
            reason = quality if quality in ('warming_up', 'unsupported_voltage', 'invalid_data', 'unavailable') else 'invalid_basis'
            return None, None, reason
        power = sample.get('ac_input_estimate_w')
        if not _nonnegative(power):
            return None, None, 'invalid_power'
    basis = {'kind': 'battery_zero' if mode == 'battery' else 'ac_estimate', 'mode': mode,
             'profile': profile, 'revision': revision, 'coefficients': config['coefficients'],
             'calibration_schema': config['schema'], 'ac_voltage_nominal_v': config.get('ac_voltage_nominal_v'),
             'source': source, 'device': stable_device, 'decoder_version': sample['decoder_version'],
             'formula_version': sample['formula_version'], 'ac_model': expected_model}
    # Quality describes the validated model's operating range, not a different
    # formula. calibrated_range <-> extrapolated must not break integration.
    return basis, power, None


def _empty():
    return {'schema': 1, 'commit_id': None, 'last_ts': None, 'tracking_started_at': None,
            'last_recorded_at': None, 'anchor': None, 'reason': None, 'dropped_intervals': 0}


def _merge(old, new):
    if old is None:
        return copy.deepcopy(new)
    if old['basis'] != new['basis']:
        raise ValueError('basis_hash_collision')
    return {**old, 'estimate_wh': _sum(old['estimate_wh'], new['estimate_wh']),
            'covered_sec': _sum(old['covered_sec'], new['covered_sec']),
            'first_ts': min(old['first_ts'], new['first_ts']), 'last_ts': max(old['last_ts'], new['last_ts']),
            'interval_count': old['interval_count'] + new['interval_count']}


class EnergyUsage:
    def __init__(self, db):
        db.execute('''CREATE TABLE IF NOT EXISTS energy_usage_state (
                        id INTEGER PRIMARY KEY CHECK(id=1), payload TEXT NOT NULL)''')
        db.execute('''CREATE TABLE IF NOT EXISTS energy_usage_hours (
                        hour_start INTEGER NOT NULL, basis_id TEXT NOT NULL, basis TEXT NOT NULL,
                        estimate_wh REAL NOT NULL, covered_sec REAL NOT NULL,
                        first_ts REAL NOT NULL, last_ts REAL NOT NULL, interval_count INTEGER NOT NULL,
                        PRIMARY KEY(hour_start,basis_id))''')
        saved = db.execute('SELECT payload FROM energy_usage_state WHERE id=1').fetchone()
        self.state = json.loads(saved[0]) if saved else _empty()
        if self.state.get('schema') != 1:
            raise ValueError('Unsupported energy usage schema')
        for field in ('last_ts', 'tracking_started_at', 'last_recorded_at'):
            if self.state[field] is not None and not _timestamp(self.state[field]):
                raise ValueError('Invalid energy usage state timestamp')
        if type(self.state['dropped_intervals']) is not int or self.state['dropped_intervals'] < 0:
            raise ValueError('Invalid energy usage drop counter')
        self.pending = {}
        self.inflight = []
        self.dirty = False
        anchor = self.state['anchor']
        if anchor is not None and (anchor.get('timestamp') != self.state['last_ts'] or
                                   not _nonnegative(anchor.get('power')) or not isinstance(anchor.get('basis'), dict)):
            self._break('continuity_lost')
        if saved and db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='meta'").fetchone():
            external = db.execute("SELECT value FROM meta WHERE key='last_ts'").fetchone()
            if external is not None:
                try:
                    external_ts = float(external[0])
                except (TypeError, ValueError, OverflowError):
                    external_ts = None
                if not _timestamp(external_ts) or external_ts != self.state['last_ts']:
                    self._break('continuity_lost')
                    if _timestamp(external_ts):
                        self.state['last_ts'] = max(external_ts, self.state['last_ts'] or 0)

    def _break(self, reason):
        self.state['anchor'] = None
        self.state['reason'] = reason
        self.dirty = True

    def ingest(self, view):
        if not isinstance(view, dict) or view.get('fresh') is not True or not isinstance(view.get('sample'), dict):
            self._break('stale')
            return
        if calibration_rejected(view):
            # Do this before timestamp deduplication: rejection can arrive for
            # the same raw sample after the active configuration changes.
            self._break('invalid_basis')
        sample = view['sample']
        ts = sample.get('timestamp')
        if not _timestamp(ts):
            self._break('invalid_timestamp')
            return
        server_time = view.get('server_time', time.time())
        if not _timestamp(server_time) or not 0 <= server_time - ts <= FRESH_SEC:
            self._break('future_sample' if _timestamp(server_time) and ts > server_time else 'stale')
            return
        last_ts = self.state['last_ts']
        if last_ts is not None and ts <= last_ts:
            if ts < last_ts:
                self._break('timestamp_rollback')
            return
        self.dirty = True
        self.state['last_ts'] = ts
        if self.state['tracking_started_at'] is None:
            self.state['tracking_started_at'] = ts
        basis, power, reason = _basis(view)
        if basis is None:
            self._break(reason)
            return
        encoded = _json(basis)
        basis_id = hashlib.sha256(encoded.encode('utf-8')).hexdigest()
        previous = self.state['anchor']
        self.state['anchor'] = {'timestamp': ts, 'power': power, 'basis': basis, 'basis_id': basis_id}
        if previous is None:
            self.state['reason'] = 'waiting_samples'
            return
        elapsed = ts - previous['timestamp']
        if elapsed > MAX_GAP_SEC:
            self.state['reason'] = 'sample_gap'
            return
        if previous['basis_id'] != basis_id:
            self.state['reason'] = 'mode_changed' if previous['basis']['mode'] != basis['mode'] else 'context_changed'
            return
        pieces = {}
        left = previous['timestamp']
        while left < ts:
            hour = int(left // 3600) * 3600
            right = min(ts, hour + 3600)
            a, b = (left - previous['timestamp']) / elapsed, (right - previous['timestamp']) / elapsed
            p_left = previous['power'] * (1 - a) + power * a
            p_right = previous['power'] * (1 - b) + power * b
            amount = (p_left / 2 + p_right / 2) * ((right - left) / 3600)
            if not _nonnegative(amount) or (amount == 0 and (p_left > 0 or p_right > 0)):
                self._break('invalid_energy')
                return
            pieces[(hour, basis_id)] = {'hour_start': hour, 'basis_id': basis_id, 'basis': basis,
                'estimate_wh': amount, 'covered_sec': right - left, 'first_ts': left, 'last_ts': right,
                'interval_count': 1}
            left = right
        # Freeze applied batches, preserve accumulated data, and reject the
        # entire new interval if the bounded outstanding buffer has no room.
        buffered = len(self.pending) + sum(max(1, len(batch['rows'])) for batch in self.inflight)
        additional = sum(key not in self.pending for key in pieces)
        if buffered + additional > MAX_PENDING_BUCKETS:
            self.state['dropped_intervals'] += 1
            self.state['reason'] = 'pending_limit'
            return
        try:
            combined = {key: _merge(self.pending.get(key), value) for key, value in pieces.items()}
        except ValueError:
            self._break('invalid_energy')
            return
        self.pending.update(combined)
        self.state['last_recorded_at'] = ts
        self.state['reason'] = None

    @staticmethod
    def _stored_state(db):
        row = db.execute('SELECT payload FROM energy_usage_state WHERE id=1').fetchone()
        return json.loads(row[0]) if row else _empty()

    @staticmethod
    def _decode(row):
        hour, identity, basis, amount, covered, first, last, count = row
        if (not _nonnegative(amount) or not _nonnegative(covered) or covered > 3600 + 1e-6 or
                not finite_number(first) or not finite_number(last) or not hour <= first < last <= hour + 3600):
            raise ValueError('Invalid energy usage hour')
        return {'hour_start': hour, 'basis_id': identity, 'basis': json.loads(basis), 'estimate_wh': amount,
                'covered_sec': covered, 'first_ts': first, 'last_ts': last, 'interval_count': count}

    def _applied_position(self, commit_id):
        if not self.inflight:
            if commit_id != self.state['commit_id']:
                raise ValueError('energy_ledger_changed')
            return -1
        if commit_id == self.inflight[0]['parent']:
            return -1
        for index, batch in enumerate(self.inflight):
            if commit_id == batch['id']:
                return index
        raise ValueError('energy_ledger_changed')

    def write(self, db):
        if not self.dirty and not self.inflight:
            return
        current = self._stored_state(db)['commit_id']
        position = self._applied_position(current)
        if self.dirty:
            # A failed transaction may be followed by fresh samples. Compact
            # every still-unapplied delta with those new samples and write the
            # latest cursor in this same transaction, matching Store.meta.
            rows = {}
            for buffer in [*(batch['rows'] for batch in self.inflight[position + 1:]), self.pending]:
                for key, value in buffer.items():
                    rows[key] = _merge(rows.get(key), value)
            snapshot = copy.deepcopy(self.state)
            generation = uuid4().hex
            snapshot['commit_id'] = generation
            batch = {'id': generation, 'parent': current, 'state': snapshot, 'rows': rows}
            prefix = self.inflight[:position + 1]
            if sum(max(1, len(item['rows'])) for item in [*prefix, batch]) > MAX_PENDING_BUCKETS:
                raise ValueError('Unacknowledged energy writes exceed pending limit')
            # Already-applied prefixes remain available until committed(), so
            # even multiple writes inside a rolled-back transaction are safe.
            self.inflight = [*prefix, batch]
            self.pending = {}
            self.dirty = False
        for batch in self.inflight[position + 1:]:
            for (hour, basis_id), delta in batch['rows'].items():
                row = db.execute('SELECT * FROM energy_usage_hours WHERE hour_start=? AND basis_id=?', (hour, basis_id)).fetchone()
                value = _merge(self._decode(row) if row else None, delta)
                if value['covered_sec'] > 3600 + 1e-6:
                    raise ValueError('Overlapping energy usage intervals')
                db.execute('INSERT OR REPLACE INTO energy_usage_hours VALUES(?,?,?,?,?,?,?,?)',
                           (hour, basis_id, _json(value['basis']), value['estimate_wh'], value['covered_sec'],
                            value['first_ts'], value['last_ts'], value['interval_count']))
            db.execute('INSERT OR REPLACE INTO energy_usage_state VALUES(1,?)', (_json(batch['state']),))

    def committed(self):
        if self.inflight:
            self.state['commit_id'] = self.inflight[-1]['id']
            self.inflight = []

    def _rows(self, db, start, end, now):
        result = {}
        for row in db.execute('''SELECT * FROM energy_usage_hours
                                  WHERE hour_start>=? AND hour_start<? ORDER BY hour_start,basis_id''', (start, end)):
            item = self._decode(row)
            result[(item['hour_start'], item['basis_id'])] = item
        buffers = [self.pending]
        if self.inflight:
            position = self._applied_position(self._stored_state(db)['commit_id'])
            buffers.extend(batch['rows'] for batch in self.inflight[position + 1:])
        for buffer in buffers:
            for key, value in buffer.items():
                if start <= key[0] < end:
                    result[key] = _merge(result.get(key), value)
        visible = []
        overlap = False
        for item in result.values():
            if item['last_ts'] <= now:
                visible.append(item)
            elif item['first_ts'] < now:
                # Aggregates cannot be accurately cut inside their recorded
                # span. Never proportionally invent Wh before an earlier now.
                overlap = True
        return visible, overlap

    @staticmethod
    def _totals(rows, expected):
        amount, covered = 0.0, 0.0
        bases = set()
        for row in rows:
            amount = _sum(amount, row['estimate_wh'])
            covered = _sum(covered, row['covered_sec'])
            bases.add(row['basis_id'])
        if covered > expected + 1e-6:
            raise ValueError('Overlapping energy usage coverage')
        covered = min(covered, expected)
        average = amount * (3600 / covered) if covered else None
        return {'estimate_kwh': amount / 1000 if covered else None, 'covered_sec': covered,
                'expected_sec': expected, 'coverage_ratio': covered / expected if expected > 0 else None,
                'average_power_w': average if finite_number(average) else None, 'basis_count': len(bases)}

    @classmethod
    def _day_summary(cls, day, rows, now, current_date):
        expected = min(86400.0, max(0.0, now - _start(day)))
        summary = cls._totals(rows, expected)
        covered = summary['covered_sec']
        status = ('future' if day > current_date else 'no_data' if not covered else
                  'today' if day == current_date else 'complete' if expected - covered <= MAX_GAP_SEC else 'partial')
        return {'date': day.isoformat(), 'status': status, **summary}

    def _metadata(self, now, overlap):
        if not _timestamp(now):
            raise ValueError('invalid_now')
        tracking = self.state['tracking_started_at']
        recorded = self.state['last_recorded_at']
        tracking = tracking if tracking is not None and tracking <= now else None
        recorded = recorded if recorded is not None and recorded <= now else None
        return {'schema': 1, 'timezone': 'Asia/Shanghai', 'utc_offset': '+08:00', 'generated_at': now,
                'current_date': datetime.fromtimestamp(now, TZ).date().isoformat(),
                'tracking_started_at': tracking,
                'first_month': datetime.fromtimestamp(tracking, TZ).strftime('%Y-%m') if tracking is not None else None,
                'last_recorded_at': recorded, 'reason': 'query_cutoff_overlap' if overlap else self.state['reason'],
                'dropped_intervals': self.state['dropped_intervals']}

    def month(self, db, month, now):
        if not _timestamp(now):
            raise ValueError('invalid_now')
        month = datetime.fromtimestamp(now, TZ).strftime('%Y-%m') if month is None else month
        first = _month(month)
        current_date = datetime.fromtimestamp(now, TZ).date()
        count = calendar.monthrange(first.year, first.month)[1]
        end = first + timedelta(days=count)
        rows, overlap = self._rows(db, _start(first), _start(end), now)
        days = []
        by_date = {}
        for row in rows:
            key = datetime.fromtimestamp(row['hour_start'], TZ).date()
            by_date.setdefault(key, []).append(row)
        for offset in range(count):
            day = first + timedelta(days=offset)
            days.append(self._day_summary(day, by_date.get(day, []), now, current_date))
        if first <= current_date < end:
            today = days[(current_date - first).days]
        else:
            today_rows, today_overlap = self._rows(db, _start(current_date), _start(current_date + timedelta(days=1)), now)
            today = self._day_summary(current_date, today_rows, now, current_date)
            overlap = overlap or today_overlap
        total = self._totals(rows, sum(day['expected_sec'] for day in days))
        complete = [day for day in days if day['status'] == 'complete']
        summary = {key: value for key, value in total.items() if key != 'average_power_w'}
        summary.update(complete_days=len(complete),
                       complete_day_average_kwh=(sum(day['estimate_kwh'] for day in complete) / len(complete)) if complete else None,
                       recorded_days=sum(day['covered_sec'] > 0 for day in days))
        return {**self._metadata(now, overlap), 'month': month, 'today': copy.deepcopy(today),
                'summary': summary, 'days': days}

    def day(self, db, date, now):
        day = _date(date)
        if not _timestamp(now):
            raise ValueError('invalid_now')
        current_date = datetime.fromtimestamp(now, TZ).date()
        start = _start(day)
        rows, overlap = self._rows(db, start, start + 86400, now)
        hours, bases = [], {}
        for hour in range(24):
            begin = start + hour * 3600
            hourly = [row for row in rows if row['hour_start'] == begin]
            hours.append({'hour': hour, 'start_ts': begin, 'end_ts': begin + 3600,
                          **self._totals(hourly, min(3600.0, max(0.0, now - begin)))})
        for row in rows:
            identity, basis = row['basis_id'], row['basis']
            item = bases.setdefault(identity, {'id': identity, 'profile': basis['profile'], 'revision': basis['revision'],
                                               'estimate_kwh': 0.0, 'covered_sec': 0.0, 'source': basis['source'],
                                               'ac_model': basis['ac_model']})
            item['estimate_kwh'] = _sum(item['estimate_kwh'], row['estimate_wh'] / 1000)
            item['covered_sec'] = _sum(item['covered_sec'], row['covered_sec'])
        return {**self._metadata(now, overlap), 'date': date,
                'day': self._day_summary(day, rows, now, current_date), 'hours': hours,
                'bases': sorted(bases.values(), key=lambda item: item['id'])}
