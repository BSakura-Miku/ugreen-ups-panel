"""Bounded SQLite aggregates: 10 seconds/7 days, 60 seconds/90 days."""
import json
import math
from contextlib import contextmanager
from functools import wraps
from pathlib import Path
import sqlite3
from threading import RLock
import time

from .power import finite_number

METRICS = ('battery_energy_estimate_w', 'ac_input_estimate_w', 'battery_charge_current_candidate_a', 'battery_discharge_current_candidate_a', 'battery_charge_power_candidate_w', 'battery_discharge_power_candidate_w', 'soc', 'power_w', 'dc_power_estimate_w', 'input_voltage', 'output_voltage', 'adapter_input_voltage_v', 'ups_output_voltage_v', 'current', 'battery_voltage', 'cell_delta_mv')
CONTEXT_FIELDS = ('calibration_profile', 'calibration_revision', 'calibration_coefficients', 'ac_estimate_model', 'battery_estimate_basis',
                  'ac_estimate_quality', 'battery_estimate_quality', 'formula_version', 'decoder_version')
MAX_PENDING_BUCKETS = 4096


def synchronized(method):
    @wraps(method)
    def wrapped(self, *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)
    return wrapped


def sample_context(sample):
    return {key: sample[key] for key in CONTEXT_FIELDS if sample.get(key) is not None}


def metrics(sample):
    result = {k: sample.get(k) for k in METRICS}
    # Freeze legacy history; do not combine different power formulas.
    if sample.get('formula_version', 1) >= 2:
        result.pop('power_w', None)
    if sample.get('decoder_version', 1) >= 3:
        result.pop('input_voltage', None)
        result.pop('output_voltage', None)
    result.update({f'cell_{i + 1}': v for i, v in enumerate(sample.get('cells', []))})
    return {k: v for k, v in result.items() if finite_number(v)}


def combine(old, new):
    result = dict(old)
    for key, value in new.items():
        if key in result:
            a = result[key]
            result[key] = [a[0] + value[0], a[1] + value[1], min(a[2], value[2]), max(a[3], value[3])]
        else:
            result[key] = value
    return result


class Store:
    def __init__(self, path):
        self._lock = RLock()
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript('''
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS events (
                  id INTEGER PRIMARY KEY, timestamp REAL, kind TEXT, detail TEXT);
                CREATE INDEX IF NOT EXISTS events_time ON events(timestamp);
                CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
            ''')
            self._migrate(db)
        self.pending = {}
        self.dropped_buckets = 0
        self.last_ts = float(self.get_meta('last_ts') or 0)
        self.last_state = self.get_meta('state')
        self.last_flush = time.monotonic()
        self.last_prune = 0

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=3)
        try:
            with db:
                yield db
        finally:
            db.close()

    def _migrate(self, db):
        db.execute('BEGIN IMMEDIATE')
        version = db.execute('PRAGMA user_version').fetchone()[0]
        if version > 2:
            raise ValueError('History database was created by a newer version')
        columns = {row[1] for row in db.execute('PRAGMA table_info(samples)')}
        if columns and 'context' not in columns:
            db.execute('ALTER TABLE samples RENAME TO samples_v1')
        db.execute('''CREATE TABLE IF NOT EXISTS samples (
            resolution INTEGER, bucket INTEGER, first REAL, last REAL, count INTEGER,
            mode TEXT, metrics TEXT, context TEXT NOT NULL,
            PRIMARY KEY(resolution,bucket,mode,context))''')
        if columns and 'context' not in columns:
            # Previous releases did not persist model provenance. Keep those rows
            # separate and explicitly unidentified instead of inventing metadata.
            db.execute('''INSERT INTO samples SELECT resolution,bucket,first,last,count,
                mode,metrics,'{"provenance":"legacy_unrecorded"}' FROM samples_v1''')
            db.execute('DROP TABLE samples_v1')
        db.execute('PRAGMA user_version=2')

    def get_meta(self, key):
        with self.connect() as db:
            row = db.execute('SELECT value FROM meta WHERE key=?', (key,)).fetchone()
            return row[0] if row else None

    @synchronized
    def ingest(self, view, now=None):
        now = time.time() if now is None else now
        sample = view.get('sample')
        state = ('online:' + sample['mode']) if view['fresh'] else 'offline'
        if state != self.last_state:
            with self.connect() as db:
                db.execute('INSERT INTO events(timestamp,kind,detail) VALUES(?,?,?)',
                           (now, 'connection' if state == 'offline' or self.last_state in (None, 'offline') else 'power', state))
                db.execute('INSERT OR REPLACE INTO meta VALUES(?,?)', ('state', state))
            self.last_state = state
        if view['fresh'] and sample['timestamp'] > self.last_ts:
            ts = sample['timestamp']
            values = {k: [v, 1, v, v] for k, v in metrics(sample).items()}
            context = json.dumps(sample_context(sample), sort_keys=True, separators=(',', ':'))
            for resolution in (10, 60):
                key = (resolution, int(ts // resolution * resolution), sample['mode'], context)
                aggregate = self.pending.get(key)
                if aggregate:
                    aggregate['metrics'] = combine(aggregate['metrics'], values)
                    aggregate['count'] += 1
                    aggregate['last'] = ts
                else:
                    self.pending[key] = {'first': ts, 'last': ts, 'count': 1, 'metrics': values}
            self.last_ts = ts
            # A full/unavailable disk must not turn the history queue into an
            # unbounded memory leak. Retain recent buckets and report any loss.
            while len(self.pending) > MAX_PENDING_BUCKETS:
                self.pending.pop(next(iter(self.pending)))
                self.dropped_buckets += 1
        if time.monotonic() - self.last_flush >= 10:
            self.flush()
        if now - self.last_prune >= 3600:
            self.prune(now)
            self.last_prune = now

    @synchronized
    def flush(self):
        with self.connect() as db:
            for (resolution, bucket, mode, context), entry in self.pending.items():
                row = db.execute('SELECT first,last,count,metrics FROM samples WHERE resolution=? AND bucket=? AND mode=? AND context=?',
                                 (resolution, bucket, mode, context)).fetchone()
                values = combine(json.loads(row[3]), entry['metrics']) if row else entry['metrics']
                db.execute('INSERT OR REPLACE INTO samples VALUES(?,?,?,?,?,?,?,?)',
                           (resolution, bucket, min(row[0], entry['first']) if row else entry['first'],
                            entry['last'], (row[2] if row else 0) + entry['count'], mode, json.dumps(values), context))
            db.execute('INSERT OR REPLACE INTO meta VALUES(?,?)', ('last_ts', str(self.last_ts)))
        self.pending.clear()
        self.last_flush = time.monotonic()

    @synchronized
    def prune(self, now):
        with self.connect() as db:
            db.execute('DELETE FROM samples WHERE (resolution=10 AND bucket<?) OR (resolution=60 AND bucket<?)',
                       (now - 7 * 86400, now - 90 * 86400))
            db.execute('DELETE FROM events WHERE timestamp<?', (now - 180 * 86400,))

    def history(self, hours, now=None):
        now = time.time() if now is None else now
        resolution = 10 if hours <= 24 else 60
        stride = max(resolution, math.ceil(hours * 3600 / 1200 / resolution) * resolution)
        with self.connect() as db:
            rows = db.execute('SELECT bucket,first,last,count,mode,metrics,context FROM samples WHERE resolution=? AND bucket>=? AND bucket<=? ORDER BY bucket,first',
                              (resolution, now - hours * 3600, now)).fetchall()
        merged = {}
        for bucket, first, last, count, mode, values, context in rows:
            key = (int(bucket // stride * stride), mode, context)
            item = merged.setdefault(key, {'timestamp': key[0], 'first': first, 'last': last, 'count': 0,
                                          'mode': mode, 'context': json.loads(context), 'metrics': {}})
            item['last'] = max(item['last'], last)
            item['count'] += count
            item['metrics'] = combine(item['metrics'], json.loads(values))
        result = []
        for item in merged.values():
            item['min'] = {k: v[2] for k, v in item['metrics'].items()}
            item['max'] = {k: v[3] for k, v in item['metrics'].items()}
            item['values'] = {k: round(v[0] / v[1], 4) for k, v in item.pop('metrics').items()}
            result.append(item)
        return {'resolution_sec': stride, 'points': sorted(result, key=lambda item: (item['timestamp'], item['first']))}

    def events(self, limit=100):
        with self.connect() as db:
            return [dict(zip(('timestamp', 'kind', 'detail'), row)) for row in db.execute(
                'SELECT timestamp,kind,detail FROM events ORDER BY id DESC LIMIT ?', (limit,))]
