import sqlite3
import time

from fastapi.testclient import TestClient
import pytest

from ups_panel.app import create_app
from ups_panel.battery_sessions import BatterySessions, STATE_KEY
from ups_panel.collector import atomic_json
from ups_panel.storage import DAY, Store


def view(ts, mode='battery', soc=90, fresh=True):
    return {'fresh': fresh, 'sample': {'timestamp': ts, 'mode': mode, 'soc': soc,
                                      'cells': [4.1, 4.1, 4.1, 4.1]}}


@pytest.fixture
def db():
    connection = sqlite3.connect(':memory:')
    connection.execute('CREATE TABLE meta (key TEXT PRIMARY KEY,value TEXT)')
    yield connection
    connection.close()


def commit(sessions, db):
    with db:
        sessions.write(db)
    sessions.committed()


def history(sessions, db, now, days=1, limit=50, fresh=True):
    return sessions.history(db, days, limit, now, fresh)


def test_duplicate_and_older_samples_cannot_create_or_end_a_session(db):
    sessions = BatterySessions(db)
    sessions.ingest(view(98, 'online', 95))
    sessions.ingest(view(100, soc=90))
    identity = sessions.active['id']
    sessions.ingest(view(100, 'charging', 99))
    sessions.ingest(view(99, 'online', 100))
    sessions.ingest(view(100, soc=40))
    assert sessions.active['id'] == identity and sessions.active['sample_count'] == 1
    sessions.ingest(view(102, soc=89))
    sessions.ingest(view(104, 'charging', 89))
    result = history(sessions, db, 105)
    record = result['records'][0]
    assert record['id'] == identity and record['status'] == 'complete'
    assert record['start_ts'] == 100 and record['last_ts'] == 102 and record['end_ts'] == 104
    assert record['start_soc'] == 90 and record['end_soc'] == 89
    assert record['sample_count'] == 2 and record['duration_sec'] == 4
    assert result['summary']['confirmed_starts'] == result['summary']['complete_count'] == 1
    assert result['summary']['soc_drop_pp'] == 1


def test_first_seen_on_battery_has_unknown_start_even_after_external_power_returns(db):
    sessions = BatterySessions(db)
    sessions.ingest(view(100, soc=80))
    sessions.ingest(view(104, 'online', 79))
    result = history(sessions, db, 105)
    record = result['records'][0]
    assert record['start_known'] is False and record['status'] == 'incomplete'
    assert record['end_reason'] == 'external' and record['duration_sec'] is None
    assert record['observed_duration_sec'] == 4 and record['soc_drop_pp'] == 1
    assert result['summary']['confirmed_starts'] == 0
    assert result['summary']['incomplete_count'] == 1
    assert result['summary']['soc_records'] == 0 and result['summary']['soc_drop_pp'] is None


@pytest.mark.parametrize('interruption,reason', [
    ({'fresh': False, 'sample': None}, 'offline'), (view(104, 'unknown'), 'unknown')])
def test_missing_observation_breaks_continuity_and_does_not_invent_a_second_start(db, interruption, reason):
    sessions = BatterySessions(db)
    for item in (view(98, 'online'), view(100), view(102, soc=89), interruption,
                 view(106, soc=88), view(108, 'charging', 88)):
        sessions.ingest(item)
    result = history(sessions, db, 109)
    newer, older = result['records']
    assert older['end_reason'] == reason and older['end_ts'] is None
    assert older['last_ts'] == 102 and older['observed_duration_sec'] == 2
    assert newer['start_known'] is False and newer['status'] == 'incomplete'
    assert result['summary']['confirmed_starts'] == 1
    assert result['summary']['incomplete_count'] == 2
    assert result['summary']['observed_duration_sec'] == 4


@pytest.mark.parametrize('timestamp', [True, '104', -1, float('nan'), float('inf')])
def test_invalid_timestamp_interrupts_without_advancing_sample_cursor(db, timestamp):
    sessions = BatterySessions(db)
    sessions.ingest(view(98, 'online'))
    sessions.ingest(view(100))
    sessions.ingest(view(timestamp))
    assert sessions.active is None and sessions.state['last_ts'] == 100
    assert history(sessions, db, 102)['records'][0]['end_reason'] == 'unknown'


def test_short_restart_continues_one_session_and_persists_sample_deduplication(db):
    sessions = BatterySessions(db)
    for item in (view(98, 'online'), view(100, soc=90), view(102, soc=89)):
        sessions.ingest(item)
    identity = sessions.active['id']
    commit(sessions, db)
    resumed = BatterySessions(db)
    resumed.ingest(view(102, 'online'))
    resumed.ingest(view(104, soc=88))
    resumed.ingest(view(106, 'charging', 88))
    commit(resumed, db)
    result = history(BatterySessions(db), db, 107)
    assert result['total_records'] == 1 and result['recording_since'] == 98
    record = result['records'][0]
    assert record['id'] == identity and record['sample_count'] == 3
    assert record['status'] == 'complete' and record['duration_sec'] == 6
    assert result['summary']['confirmed_starts'] == 1 and result['summary']['soc_drop_pp'] == 2


@pytest.mark.parametrize('next_mode', ['battery', 'online'])
def test_long_restart_preserves_only_observed_duration_and_never_bridges_the_gap(db, next_mode):
    sessions = BatterySessions(db)
    for item in (view(98, 'online'), view(100), view(102, soc=89)):
        sessions.ingest(item)
    identity = sessions.active['id']
    commit(sessions, db)
    resumed = BatterySessions(db)
    resumed.ingest(view(200, next_mode, 80))
    result = history(resumed, db, 201)
    old = next(record for record in result['records'] if record['id'] == identity)
    assert old['end_reason'] == 'gap' and old['end_ts'] is None
    assert old['observed_duration_sec'] == 2 and old['duration_sec'] is None
    assert result['summary']['observed_duration_sec'] == 2
    assert result['summary']['confirmed_starts'] == 1
    if next_mode == 'battery':
        assert result['records'][0]['id'] != identity
        assert result['records'][0]['start_known'] is False
        assert result['records'][0]['status'] == 'ongoing'
    else:
        assert result['total_records'] == 1


@pytest.mark.parametrize('start_soc,end_soc,expected', [
    (80, 82, -2), (None, 79, None), (80, None, None), (True, 79, None),
    (float('nan'), 79, None), (80, 101, None), (80, -1, None)])
def test_soc_rebound_is_not_clamped_and_missing_endpoints_are_not_filled(db, start_soc, end_soc, expected):
    sessions = BatterySessions(db)
    sessions.ingest(view(98, 'online'))
    sessions.ingest(view(100, soc=start_soc))
    sessions.ingest(view(102, soc=79))
    sessions.ingest(view(104, 'charging', end_soc))
    result = history(sessions, db, 105)
    assert result['records'][0]['soc_drop_pp'] == expected
    assert result['summary']['soc_drop_pp'] == expected
    assert result['summary']['soc_records'] == (0 if expected is None else 1)


def test_range_summaries_clip_duration_but_do_not_prorate_soc_or_count_old_starts(db):
    sessions = BatterySessions(db)
    for item in (view(98, 'online'), view(100, soc=90), view(110, soc=85),
                 view(120, 'online', 80), view(130, soc=75), view(134, 'charging', 74)):
        sessions.ingest(item)
    result = history(sessions, db, DAY + 110, limit=1)
    assert result['requested_start'] == 110
    assert result['total_records'] == 2 and result['has_more'] is True
    assert len(result['records']) == 1 and result['records'][0]['start_ts'] == 130
    summary = result['summary']
    assert summary['confirmed_starts'] == 1 and summary['complete_count'] == 2
    assert summary['observed_duration_sec'] == 14 and summary['complete_duration_sec'] == 4
    assert summary['soc_records'] == 1 and summary['soc_drop_pp'] == 1
    right_edge = history(sessions, db, 132)
    assert right_edge['records'][0]['overlaps_boundary'] is True
    assert right_edge['summary']['observed_duration_sec'] == 22
    assert right_edge['summary']['complete_duration_sec'] == 20
    assert right_edge['summary']['soc_drop_pp'] == 10


def test_pending_is_readable_and_stale_get_does_not_mutate_persisted_continuity(db):
    sessions = BatterySessions(db)
    sessions.ingest(view(98, 'online'))
    sessions.ingest(view(100))
    assert db.execute('SELECT COUNT(*) FROM battery_sessions').fetchone()[0] == 0
    assert history(sessions, db, 101)['records'][0]['status'] == 'ongoing'
    stale = history(sessions, db, 102, fresh=False)['records'][0]
    assert stale['status'] == 'incomplete' and stale['end_reason'] == 'offline'
    assert sessions.active['end_reason'] is None
    assert history(sessions, db, 101)['records'][0]['status'] == 'ongoing'
    commit(sessions, db)
    assert not sessions.pending and db.execute('SELECT COUNT(*) FROM battery_sessions').fetchone()[0] == 1
    assert db.execute('SELECT value FROM meta WHERE key=?', (STATE_KEY,)).fetchone()


def test_store_exposes_pending_and_restarts_without_recounting(tmp_path):
    store = Store(tmp_path / 'history.db')
    for item in (view(98, 'online'), view(100), view(102, soc=89)):
        store.ingest(item, now=item['sample']['timestamp'])
    pending = store.battery_history(1, now=103)
    assert pending['summary']['confirmed_starts'] == 1
    with store.connect() as db:
        assert db.execute('SELECT COUNT(*) FROM battery_sessions').fetchone()[0] == 0
    store.flush()
    restored = Store(store.path)
    restored.ingest(view(102, 'online'), now=103)
    restored.ingest(view(104, 'charging', 89), now=104)
    restored.flush()
    result = restored.battery_history(1, now=105)
    assert result['total_records'] == 1
    assert result['records'][0]['id'] == pending['records'][0]['id']
    assert result['records'][0]['status'] == 'complete'


def test_store_failed_commit_keeps_pending_for_one_exact_retry(tmp_path, monkeypatch):
    store = Store(tmp_path / 'history.db')
    for item in (view(98, 'online'), view(100), view(102, 'charging', 89)):
        store.ingest(item, now=item['sample']['timestamp'])
    original = store.battery_sessions.write

    def fail_after_write(db):
        original(db)
        raise sqlite3.OperationalError('simulated transaction failure')

    monkeypatch.setattr(store.battery_sessions, 'write', fail_after_write)
    with pytest.raises(sqlite3.OperationalError, match='transaction failure'):
        store.flush()
    assert store.battery_sessions.pending and store.pending
    with store.connect() as db:
        assert db.execute('SELECT COUNT(*) FROM battery_sessions').fetchone()[0] == 0
        assert db.execute('SELECT COUNT(*) FROM samples').fetchone()[0] == 0
        assert db.execute('SELECT value FROM meta WHERE key=?', (STATE_KEY,)).fetchone() is None
    assert store.battery_history(1, now=103)['summary']['complete_count'] == 1
    monkeypatch.setattr(store.battery_sessions, 'write', original)
    store.flush()
    assert not store.battery_sessions.pending
    with store.connect() as db:
        assert db.execute('SELECT COUNT(*) FROM battery_sessions').fetchone()[0] == 1
    store.flush()
    assert Store(store.path).battery_history(1, now=103)['summary']['complete_count'] == 1


def write_snapshot(path, timestamp, mode='online'):
    atomic_json(path, {'schema': 1, 'heartbeat': timestamp, 'source': 'usbmon',
                       'sample': view(timestamp, mode)['sample'], 'nut': {}})


def ready_response(client, path):
    # The API lifespan starts the writer asynchronously; wait only for its store.
    for _ in range(100):
        response = client.get(path)
        if response.status_code == 200:
            break
        time.sleep(.01)
    assert response.status_code == 200
    return response


def test_api_bounds_freshness_and_no_sessions_invented_from_legacy_history(tmp_path):
    now = time.time()
    database = tmp_path / 'history.db'
    old = Store(database)
    for item in (view(now - 120), view(now - 118, 'charging')):
        old.ingest(item, now=item['sample']['timestamp'])
    old.flush()
    # Reproduce a pre-session database: telemetry and state events already exist.
    with old.connect() as connection:
        assert connection.execute('SELECT COUNT(*) FROM samples').fetchone()[0] > 0
        assert connection.execute('SELECT COUNT(*) FROM events').fetchone()[0] > 0
        connection.execute('DROP TABLE battery_sessions')
        connection.execute('DELETE FROM meta WHERE key=?', (STATE_KEY,))
    snapshot = tmp_path / 'latest.json'
    write_snapshot(snapshot, now)
    with TestClient(create_app(snapshot, database, tmp_path)) as client:
        result = ready_response(client, '/api/battery-sessions?days=365&limit=500').json()
        assert result['requested_end'] - result['requested_start'] == 365 * DAY
        assert result['capture_fresh'] is True
        assert result['records'] == [] and result['total_records'] == 0
        assert result['summary']['confirmed_starts'] == 0
        assert result['summary']['soc_drop_pp'] is None
        for query in ('days=366', 'days=0', 'limit=501', 'limit=0'):
            assert client.get('/api/battery-sessions?' + query).status_code == 422
        write_snapshot(snapshot, time.time() - 20)
        stale = client.get('/api/battery-sessions').json()
        assert stale['capture_fresh'] is False
        assert stale['records'] == [] and stale['summary']['confirmed_starts'] == 0


@pytest.mark.parametrize('failure', ['initialization', 'query'])
def test_api_storage_failure_returns_503_while_live_remains_available(tmp_path, monkeypatch, failure):
    snapshot = tmp_path / 'latest.json'
    write_snapshot(snapshot, time.time())
    database = tmp_path / 'history.db'
    if failure == 'initialization':
        blocker = tmp_path / 'not-a-directory'
        blocker.write_text('x')
        database = blocker / 'history.db'
    with TestClient(create_app(snapshot, database, tmp_path)) as client:
        if failure == 'query':
            ready_response(client, '/api/battery-sessions')

            def unavailable(*args, **kwargs):
                raise sqlite3.OperationalError('simulated unreadable database')

            monkeypatch.setattr(Store, 'battery_history', unavailable)
        assert client.get('/api/battery-sessions').status_code == 503
        live = client.get('/api/live')
        assert live.status_code == 200 and live.json()['fresh'] is True
        assert live.json()['sample']['mode'] == 'online'


def test_retention_removes_only_closed_records_before_the_year_boundary(db):
    sessions = BatterySessions(db)
    now = 500 * DAY
    cutoff = now - 365 * DAY
    records = [
        ('old-complete', cutoff - 20, cutoff - 1, cutoff - 3, 'external'),
        ('old-interrupted', cutoff - 20, None, cutoff - 1, 'offline'),
        ('boundary-complete', cutoff - 8, cutoff, cutoff - 2, 'external'),
        ('crossing-complete', cutoff - 8, cutoff + 2, cutoff, 'external'),
        ('boundary-interrupted', cutoff - 8, None, cutoff, 'offline'),
        ('crossing-interrupted', cutoff - 8, None, cutoff + 2, 'gap'),
        ('old-active', cutoff - 20, None, cutoff - 12, None),
    ]
    db.executemany('''INSERT INTO battery_sessions
                      (id,start_ts,end_ts,last_ts,start_soc,end_soc,start_known,end_reason,sample_count)
                      VALUES(?,?,?,?,90,89,1,?,2)''', records)
    sessions.prune(db, now)
    retained = {row[0] for row in db.execute('SELECT id FROM battery_sessions')}
    assert retained == {'boundary-complete', 'crossing-complete', 'boundary-interrupted',
                        'crossing-interrupted', 'old-active'}
    result = history(sessions, db, now, days=365)
    assert {record['id'] for record in result['records']} == retained - {'old-active'}
    assert all(record['overlaps_boundary'] for record in result['records'])
