"""Additional events are recorded prospectively; legacy times remain observed times."""
import json
import re


def initialize(db):
    db.executescript('''CREATE TABLE IF NOT EXISTS timeline_events (
        id INTEGER PRIMARY KEY, occurred_at REAL, observed_at REAL NOT NULL,
        kind TEXT NOT NULL, detail TEXT NOT NULL, source TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS timeline_time ON timeline_events(observed_at);
        CREATE TABLE IF NOT EXISTS timeline_notes (event_id TEXT PRIMARY KEY, note TEXT NOT NULL, updated_at REAL NOT NULL);''')


def append(db, kind, detail, now, occurred_at=None, source='panel'):
    db.execute('INSERT INTO timeline_events(occurred_at,observed_at,kind,detail,source) VALUES(?,?,?,?,?)',
               (occurred_at, now, kind, detail, source))


def observe(db, view, now):
    sample = view.get('sample') or {}
    nut = view.get('nut') or {}
    candidates = []
    revision = sample.get('calibration_revision')
    if view.get('fresh') and isinstance(revision, str) and re.fullmatch('[a-f0-9]{64}', revision):
        candidates.append(('calibration', revision, '采集器已应用新的校准配置', sample.get('timestamp'), 'collector'))
    stamp = nut.get('timestamp')
    if nut.get('available') and type(stamp) in (int, float) and 0 <= now - stamp <= 45:
        raw = (nut.get('values') or {}).get('ups.status')
        if isinstance(raw, str) and re.fullmatch('[A-Z ]{1,160}', raw):
            tokens = ' '.join(sorted(set(raw.split())))
            candidates.append(('nut', tokens, tokens, None, 'nut'))
    for kind, identity, detail, occurred, source in candidates:
        key = 'timeline_' + kind
        old = db.execute('SELECT value FROM meta WHERE key=?', (key,)).fetchone()
        if old is not None and old[0] != identity:
            append(db, kind, detail, now, occurred, source)
        if old is None or old[0] != identity:
            db.execute('INSERT OR REPLACE INTO meta VALUES(?,?)', (key, identity))


def events(db, limit=100):
    rows = db.execute('''SELECT 'legacy-' || id AS event_id, NULL AS occurred_at, timestamp AS observed_at,
                     kind, detail, 'collector_observation' AS source FROM events
                     UNION ALL SELECT 'timeline-' || id, occurred_at, observed_at, kind, detail, source FROM timeline_events
                     ORDER BY observed_at DESC, event_id DESC LIMIT ?''', (limit,)).fetchall()
    notes = dict(db.execute('SELECT event_id,note FROM timeline_notes'))
    return [dict(zip(('id', 'occurred_at', 'observed_at', 'kind', 'detail', 'source'), row), note=notes.get(row[0], '')) for row in rows]


def save_note(db, event_id, note, now):
    if not isinstance(note, str) or len(note) > 300 or any(ord(c) < 32 and c not in '\n\t' for c in note):
        raise ValueError('备注最多 300 字，不支持控制字符。')
    match = re.fullmatch('(legacy|timeline)-([1-9][0-9]{0,18})', event_id)
    if not match:
        raise ValueError('事件不存在。')
    table = 'events' if match[1] == 'legacy' else 'timeline_events'
    if db.execute('SELECT id FROM ' + table + ' WHERE id=?', (int(match[2]),)).fetchone() is None:
        raise ValueError('事件不存在。')
    db.execute('INSERT OR REPLACE INTO timeline_notes VALUES(?,?,?)', (event_id, note.strip(), now))
    return {'id': event_id, 'note': note.strip()}
