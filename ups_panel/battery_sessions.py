"""Observed battery-use sessions, independent of polling-time state events."""
import json
from uuid import uuid4

from .power import finite_number

MAX_GAP_SEC = 10
STATE_KEY = 'battery_sessions_state'
FIELDS = ('id', 'start_ts', 'end_ts', 'last_ts', 'start_soc', 'end_soc',
          'start_known', 'end_reason', 'sample_count')


def soc(sample):
    value = sample.get('soc')
    return value if finite_number(value) and 0 <= value <= 100 else None


class BatterySessions:
    """Store holds the lock; writes join its aggregate transaction.

    Pending records remain available to readers, and are only cleared after a
    successful commit. Persistent sample time/mode allow a short restart to
    continue an observed session without counting another start.
    """
    def __init__(self, db):
        db.execute('''CREATE TABLE IF NOT EXISTS battery_sessions (
            id TEXT PRIMARY KEY, start_ts REAL NOT NULL, end_ts REAL,
            last_ts REAL NOT NULL, start_soc REAL, end_soc REAL,
            start_known INTEGER NOT NULL, end_reason TEXT, sample_count INTEGER NOT NULL)''')
        db.execute('CREATE INDEX IF NOT EXISTS battery_sessions_start ON battery_sessions(start_ts)')
        saved = db.execute('SELECT value FROM meta WHERE key=?', (STATE_KEY,)).fetchone()
        self.state = json.loads(saved[0]) if saved else {
            'last_ts': None, 'last_mode': None, 'active_id': None, 'recording_since': None}
        self.pending = {}
        self.active = None
        if self.state['active_id']:
            row = db.execute('SELECT * FROM battery_sessions WHERE id=?', (self.state['active_id'],)).fetchone()
            if row:
                self.active = dict(zip(FIELDS, row))
            else:
                self.state['active_id'] = None
                self.state['last_mode'] = None

    def interrupt(self, reason):
        if self.active is not None:
            self.active['end_reason'] = reason
            self.pending[self.active['id']] = dict(self.active)
            self.active = None
            self.state['active_id'] = None
        self.state['last_mode'] = None

    def ingest(self, view):
        if not view.get('fresh') or not isinstance(view.get('sample'), dict):
            self.interrupt('offline')
            return
        sample = view['sample']
        ts = sample.get('timestamp')
        if not finite_number(ts) or ts < 0:
            self.interrupt('unknown')
            return
        previous_ts = self.state['last_ts']
        # A stale duplicate must not create a transition or revive continuity.
        if previous_ts is not None and ts <= previous_ts:
            return
        if self.state['recording_since'] is None:
            self.state['recording_since'] = ts
        if previous_ts is not None and ts - previous_ts > MAX_GAP_SEC:
            self.interrupt('gap')
        previous_mode = self.state['last_mode']
        mode = sample.get('mode')
        self.state['last_ts'] = ts
        if mode not in ('battery', 'online', 'charging'):
            self.interrupt('unknown')
            return
        if mode == 'battery':
            if self.active is None:
                self.active = {
                    'id': uuid4().hex, 'start_ts': ts, 'end_ts': None, 'last_ts': ts,
                    'start_soc': soc(sample), 'end_soc': soc(sample),
                    'start_known': previous_mode in ('online', 'charging'),
                    'end_reason': None, 'sample_count': 1}
                self.state['active_id'] = self.active['id']
            else:
                self.active.update(last_ts=ts, end_soc=soc(sample),
                                   sample_count=self.active['sample_count'] + 1)
            self.pending[self.active['id']] = dict(self.active)
        elif self.active is not None:
            self.active.update(end_ts=ts, end_soc=soc(sample), end_reason='external')
            self.pending[self.active['id']] = dict(self.active)
            self.active = None
            self.state['active_id'] = None
        self.state['last_mode'] = mode

    def write(self, db):
        for record in self.pending.values():
            db.execute('INSERT OR REPLACE INTO battery_sessions VALUES(?,?,?,?,?,?,?,?,?)',
                       tuple(record[key] for key in FIELDS))
        db.execute('INSERT OR REPLACE INTO meta VALUES(?,?)', (STATE_KEY, json.dumps(self.state)))

    def committed(self):
        self.pending.clear()

    @staticmethod
    def prune(db, now):
        db.execute('''DELETE FROM battery_sessions WHERE end_reason IS NOT NULL
                      AND COALESCE(end_ts,last_ts) < ?''', (now - 365 * 86400,))

    def history(self, db, days, limit, now, capture_fresh):
        start = now - days * 86400
        rows = db.execute('''SELECT * FROM battery_sessions
                             WHERE COALESCE(end_ts,last_ts)>=? AND start_ts<=?''', (start, now))
        records = {row[0]: dict(zip(FIELDS, row)) for row in rows}
        records.update({key: dict(value) for key, value in self.pending.items()})
        result = []
        summary = {'confirmed_starts': 0, 'complete_count': 0, 'incomplete_count': 0,
                   'ongoing_count': 0, 'observed_duration_sec': 0,
                   'complete_duration_sec': 0, 'soc_drop_pp': None, 'soc_records': 0}
        for item in records.values():
            end = item['end_ts'] if item['end_ts'] is not None else item['last_ts']
            if end < start or item['start_ts'] > now:
                continue
            item['start_known'] = bool(item['start_known'])
            # A reader may see the collector stop before the writer's next poll.
            # Mark the active fragment interrupted in this response immediately;
            # persistence happens through normal ingest, never by a GET request.
            if item['end_reason'] is None and (not capture_fresh or now - item['last_ts'] > MAX_GAP_SEC):
                item['end_reason'] = 'offline' if not capture_fresh else 'gap'
            complete = item['start_known'] and item['end_reason'] == 'external'
            item['status'] = 'complete' if complete else 'ongoing' if item['end_reason'] is None else 'incomplete'
            item['observed_duration_sec'] = max(0, end - item['start_ts'])
            item['duration_sec'] = item['observed_duration_sec'] if complete else None
            item['soc_drop_pp'] = (round(item['start_soc'] - item['end_soc'], 4)
                                   if item['start_soc'] is not None and item['end_soc'] is not None else None)
            item['overlaps_boundary'] = item['start_ts'] < start or end > now
            summary[item['status'] + '_count'] += 1
            if item['start_known'] and start <= item['start_ts'] <= now:
                summary['confirmed_starts'] += 1
            summary['observed_duration_sec'] += max(0, min(end, now) - max(item['start_ts'], start))
            if complete and not item['overlaps_boundary']:
                summary['complete_duration_sec'] += item['observed_duration_sec']
                if item['soc_drop_pp'] is not None:
                    summary['soc_drop_pp'] = (summary['soc_drop_pp'] or 0) + item['soc_drop_pp']
                    summary['soc_records'] += 1
            result.append(item)
        if summary['soc_drop_pp'] is not None:
            summary['soc_drop_pp'] = round(summary['soc_drop_pp'], 4)
        result.sort(key=lambda item: item['start_ts'], reverse=True)
        return {'schema': 1, 'requested_start': start, 'requested_end': now,
                'recording_since': self.state['recording_since'], 'capture_fresh': bool(capture_fresh),
                'summary': summary, 'records': result[:limit], 'total_records': len(result),
                'has_more': len(result) > limit}
