import csv
import io
import json
import sqlite3
import time

from fastapi.testclient import TestClient
import pytest

from ups_panel.app import create_app
from ups_panel.storage import DAY, Store


def context(**values):
    return json.dumps(values, sort_keys=True, separators=(',', ':'))


def minute(bucket, first, last, count, values, mode='online', identity='{}'):
    return (60, bucket, first, last, count, mode, json.dumps(values), identity)


def legacy_rows(path, rows):
    """Write the same persisted minute format as pre-daily releases, without rolling it up."""
    with sqlite3.connect(path) as db:
        db.execute('''CREATE TABLE IF NOT EXISTS samples (
            resolution INTEGER,bucket INTEGER,first REAL,last REAL,count INTEGER,
            mode TEXT,metrics TEXT,context TEXT NOT NULL,
            PRIMARY KEY(resolution,bucket,mode,context))''')
        db.execute('CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY,value TEXT)')
        db.executemany('INSERT OR REPLACE INTO samples VALUES(?,?,?,?,?,?,?,?)', rows)
        latest = db.execute('SELECT MAX(last) FROM samples WHERE resolution=60').fetchone()[0]
        db.execute('INSERT OR REPLACE INTO meta VALUES(?,?)', ('last_ts', str(latest)))
        db.execute('PRAGMA user_version=2')


def sample(timestamp, delta=4, mode='online', **metadata):
    return {'timestamp': timestamp, 'mode': mode, 'cell_delta_mv': delta, 'soc': 90,
            'cells': [4.1, 4.1 + delta / 1000, 4.1, 4.1],
            'formula_version': 2, 'decoder_version': 4, **metadata}


def ingest(store, value):
    store.ingest({'fresh': True, 'sample': value}, now=value['timestamp'])


def test_migration_preserves_weighted_means_extrema_and_metric_specific_counts(tmp_path):
    path = tmp_path / 'history.db'
    day = 20000 * DAY
    identity = context(calibration_profile='custom', calibration_revision='installation-a')
    rows = [
        minute(day + 60, day + 61, day + 63, 2,
               {'cell_delta_mv': [14, 2, 2, 12], 'ac_input_estimate_w': [60, 1, 60, 60],
                'cell_1': [8.1, 2, 3.9, 4.2], 'cell_2': [8.114, 2, 3.902, 4.212]}, identity=identity),
        minute(day + 120, day + 121, day + 139, 10,
               {'cell_delta_mv': [30, 10, 3, 3], 'ac_input_estimate_w': [80, 1, 80, 80]}, identity=identity),
    ]
    legacy_rows(path, rows)
    store = Store(path)
    history = store.history(8760, now=day + 200)
    assert history['resolution_sec'] == DAY
    assert len(history['points']) == 1
    point = history['points'][0]
    assert point['count'] == 12
    assert point['values']['cell_delta_mv'] == 3.6667
    assert point['values']['ac_input_estimate_w'] == 70
    assert point['min']['cell_delta_mv'] == 2 and point['max']['cell_delta_mv'] == 12
    assert point['max']['cell_2'] - point['min']['cell_1'] > .3
    assert point['max']['cell_delta_mv'] != pytest.approx((point['max']['cell_2'] - point['min']['cell_1']) * 1000)
    assert point['context'] == json.loads(identity)
    with store.connect() as db:
        persisted = json.loads(db.execute('SELECT metrics FROM samples WHERE resolution=?', (DAY,)).fetchone()[0])
        assert persisted['cell_delta_mv'] == [44, 12, 2, 12]
        assert persisted['ac_input_estimate_w'] == [140, 2, 60, 80]
        assert db.execute('PRAGMA user_version').fetchone()[0] == 2
        assert db.execute('SELECT COUNT(*) FROM daily_rollup_checkpoint').fetchone()[0] == 1
        assert db.execute('SELECT metrics FROM samples WHERE resolution=60 ORDER BY bucket').fetchall() == [(row[6],) for row in rows]
    for _ in range(3):
        store = Store(path)
        assert store.history(8760, now=day + 200) == history


def test_reupgrade_after_older_release_appends_same_minute_without_double_counting(tmp_path):
    path = tmp_path / 'history.db'
    day = 20000 * DAY
    identity = context(calibration_revision='retained-model')
    original = minute(day + 60, day + 61, day + 63, 2,
                      {'cell_delta_mv': [14, 2, 2, 12], 'ac_input_estimate_w': [60, 1, 60, 60]}, identity=identity)
    legacy_rows(path, [original])
    initial = Store(path)
    assert initial.history(8760, now=day + 200)['points'][0]['count'] == 2

    # A rolled-back release sees schema 2 and updates only the 60-second rows.
    # The first replacement includes the two previously counted observations.
    legacy_rows(path, [
        minute(day + 60, day + 61, day + 65, 3,
               {'cell_delta_mv': [44, 3, 2, 30], 'ac_input_estimate_w': [130, 2, 60, 70]}, identity=identity),
        minute(day + 120, day + 121, day + 125, 3,
               {'cell_delta_mv': [18, 3, 5, 7], 'ac_input_estimate_w': [80, 1, 80, 80]}, identity=identity),
    ])
    upgraded = Store(path)
    point = upgraded.history(8760, now=day + 200)['points'][0]
    assert point['count'] == 6
    assert point['values']['cell_delta_mv'] == 10.3333
    assert point['min']['cell_delta_mv'] == 2 and point['max']['cell_delta_mv'] == 30
    assert point['values']['ac_input_estimate_w'] == 70
    assert point['first'] == day + 61 and point['last'] == day + 125
    assert Store(path).history(8760, now=day + 200)['points'] == [point]


def test_checkpoint_keeps_each_mode_and_context_in_its_last_minute(tmp_path):
    path = tmp_path / 'history.db'
    day = 20000 * DAY
    first, second = context(calibration_revision='first'), context(calibration_revision='second')
    legacy_rows(path, [
        minute(day + 60, day + 61, day + 61, 1, {'cell_delta_mv': [2, 1, 2, 2]}, identity=first),
        minute(day + 60, day + 62, day + 62, 1, {'cell_delta_mv': [4, 1, 4, 4]}, mode='charging', identity=first),
    ])
    Store(path)
    legacy_rows(path, [
        minute(day + 60, day + 61, day + 63, 2, {'cell_delta_mv': [12, 2, 2, 10]}, identity=first),
        minute(day + 60, day + 64, day + 64, 1, {'cell_delta_mv': [8, 1, 8, 8]}, identity=second),
    ])
    points = Store(path).history(8760, now=day + 200)['points']
    assert len(points) == 3
    by_identity = {(point['mode'], point['context']['calibration_revision']): point for point in points}
    assert by_identity[('online', 'first')]['count'] == 2
    assert by_identity[('online', 'first')]['values']['cell_delta_mv'] == 6
    assert by_identity[('charging', 'first')]['count'] == 1
    assert by_identity[('online', 'second')]['count'] == 1


def test_reupgrade_keeps_old_daily_data_when_its_minute_source_has_already_expired(tmp_path):
    path = tmp_path / 'history.db'
    old_day, new_day = 20000 * DAY, 20200 * DAY
    legacy_rows(path, [minute(old_day, old_day + 2, old_day + 2, 1, {'cell_delta_mv': [6, 1, 6, 6]})])
    Store(path)
    # Simulate the old release's 90-day pruning during a long rollback period.
    with sqlite3.connect(path) as db:
        db.execute('DELETE FROM samples WHERE resolution=60')
    legacy_rows(path, [minute(new_day, new_day + 2, new_day + 2, 1, {'cell_delta_mv': [8, 1, 8, 8]})])
    points = Store(path).history(8760, now=new_day + 100)['points']
    assert len(points) == 2
    assert [(point['bucket_start'], point['count'], point['values']['cell_delta_mv']) for point in points] == [
        (old_day, 1, 6), (new_day, 1, 8)]


def test_failed_migration_rolls_back_daily_totals_and_cursor(tmp_path, monkeypatch):
    path = tmp_path / 'history.db'
    legacy_rows(path, [minute(60, 61, 63, 2, {'cell_delta_mv': [14, 2, 2, 12]})])
    original = Store._rollup_daily

    def interrupt(db):
        original(db)
        raise sqlite3.OperationalError('simulated interrupted upgrade')

    monkeypatch.setattr(Store, '_rollup_daily', staticmethod(interrupt))
    with pytest.raises(sqlite3.OperationalError, match='interrupted upgrade'):
        Store(path)
    with sqlite3.connect(path) as db:
        assert db.execute('SELECT COUNT(*) FROM samples WHERE resolution=?', (DAY,)).fetchone()[0] == 0
        assert db.execute('SELECT value FROM meta WHERE key=?', ('daily_rollup_cursor',)).fetchone() is None
        assert db.execute('SELECT count FROM samples WHERE resolution=60').fetchone()[0] == 2
    monkeypatch.setattr(Store, '_rollup_daily', staticmethod(original))
    assert Store(path).history(8760, now=100)['points'][0]['count'] == 2


def test_failed_flush_retries_source_and_daily_rows_as_one_transaction(tmp_path, monkeypatch):
    store = Store(tmp_path / 'history.db')
    ingest(store, sample(100, 7))
    original = Store._rollup_daily

    def interrupt(db):
        original(db)
        raise sqlite3.OperationalError('simulated failed commit')

    monkeypatch.setattr(Store, '_rollup_daily', staticmethod(interrupt))
    with pytest.raises(sqlite3.OperationalError, match='failed commit'):
        store.flush()
    assert store.pending
    with store.connect() as db:
        assert db.execute('SELECT COUNT(*) FROM samples').fetchone()[0] == 0
        assert db.execute('SELECT value FROM meta WHERE key=?', ('daily_rollup_cursor',)).fetchone() is None
    monkeypatch.setattr(Store, '_rollup_daily', staticmethod(original))
    store.flush()
    assert not store.pending
    with store.connect() as db:
        assert db.execute('SELECT resolution,count FROM samples ORDER BY resolution').fetchall() == [(10, 1), (60, 1), (DAY, 1)]


def test_daily_flush_uses_utc_boundaries_and_preserves_configuration_groups(tmp_path):
    store = Store(tmp_path / 'history.db')
    day = 20000 * DAY
    for value in (sample(day - 2, 3, calibration_revision='a'),
                  sample(day + 2, 5, calibration_revision='a'),
                  sample(day + 4, 7, mode='charging', calibration_revision='a'),
                  sample(day + 6, 9, calibration_revision='b'),
                  sample(day + 8, 11, calibration_revision='a')):
        ingest(store, value)
        store.flush()
    history = store.history(8760, now=day + 100)
    points = history['points']
    assert len(points) == 4
    assert {point['bucket_start'] for point in points} == {day - DAY, day}
    assert all(point['bucket_start'] % DAY == 0 and point['bucket_end'] - point['bucket_start'] == DAY for point in points)
    online = next(point for point in points if point['bucket_start'] == day
                  and point['mode'] == 'online' and point['context']['calibration_revision'] == 'a')
    assert online['count'] == 2 and online['values']['cell_delta_mv'] == 8
    assert online['min']['cell_delta_mv'] == 5 and online['max']['cell_delta_mv'] == 11
    assert online['first'] == day + 2 and online['last'] == day + 8
    assert history['available_start'] == day - 2 and history['available_end'] == day + 8
    assert points[0]['partial_range'] is False and online['partial_range'] is True


def test_daily_retention_keeps_intersecting_oldest_day_and_survives_minute_pruning(tmp_path):
    path = tmp_path / 'history.db'
    now = 500 * DAY + DAY / 2
    oldest = 135 * DAY
    legacy_rows(path, [minute(day + 60, day + 60, day + 60, 1, {'cell_delta_mv': [value, 1, value, value]})
                       for day, value in ((oldest - DAY, 2), (oldest, 7), (oldest + DAY, 9), (499 * DAY, 4))])
    store = Store(path)
    store.prune(now)
    with store.connect() as db:
        buckets = [row[0] for row in db.execute('SELECT bucket FROM samples WHERE resolution=? ORDER BY bucket', (DAY,))]
        assert buckets == [oldest, oldest + DAY, 499 * DAY]
        assert db.execute('SELECT COUNT(*) FROM samples WHERE resolution=60').fetchone()[0] == 1
    history = store.history(8760, now=now)
    edge = history['points'][0]
    assert history['requested_start'] == oldest + DAY / 2
    assert history['requested_end'] == now
    assert history['available_start'] == oldest + 60
    assert edge['partial_range'] is True
    assert edge['bucket_start'] < history['requested_start'] < edge['bucket_end']
    assert edge['values']['cell_delta_mv'] == 7  # Full UTC day, not a fabricated half-day mean.
    assert Store(path).history(8760, now=now) == history
    store.prune(501 * DAY)
    with store.connect() as db:
        assert db.execute('SELECT COUNT(*) FROM samples WHERE resolution=? AND bucket=?', (DAY, oldest)).fetchone()[0] == 0


def test_new_resolution_does_not_change_short_retention_and_queries(tmp_path):
    store = Store(tmp_path / 'history.db')
    day = 20000 * DAY
    ingest(store, sample(day + 2, 5))
    store.flush()
    assert store.history(1, now=day + 100)['resolution_sec'] == 10
    assert store.history(24, now=day + 100)['resolution_sec'] == 80
    assert store.history(2160, now=day + 100)['resolution_sec'] == 6480
    assert store.history(2161, now=day + 100)['resolution_sec'] == DAY
    store.prune(day + 8 * DAY)
    with store.connect() as db:
        assert db.execute('SELECT resolution,count FROM samples ORDER BY resolution').fetchall() == [(60, 1), (DAY, 1)]
    store.prune(day + 91 * DAY)
    with store.connect() as db:
        assert db.execute('SELECT resolution,count FROM samples ORDER BY resolution').fetchall() == [(DAY, 1)]
    assert store.history(8760, now=day + 91 * DAY)['points'][0]['count'] == 1


def test_empty_history_reports_requested_window_without_inventing_available_records(tmp_path):
    history = Store(tmp_path / 'empty.db').history(8760, now=1000 * DAY)
    assert history['requested_start'] == 635 * DAY
    assert history['requested_end'] == 1000 * DAY
    assert history['available_start'] is None and history['available_end'] is None
    assert history['points'] == []


def test_api_year_limit_and_csv_keeps_mean_and_exports_real_extrema(tmp_path):
    now = time.time()
    recorded = now - 120 * DAY
    bucket = int(recorded // 60 * 60)
    recorded = bucket + 10
    database = tmp_path / 'history.db'
    identity = context(calibration_profile='custom', calibration_revision='old-recorded-model')
    legacy_rows(database, [minute(bucket, recorded, recorded + 2, 2,
                                 {'cell_delta_mv': [22, 2, 2, 20], 'cell_1': [8.2, 2, 4.0, 4.2]}, identity=identity)])
    snapshot = tmp_path / 'latest.json'
    snapshot.write_text(json.dumps({'schema': 1, 'heartbeat': now, 'sample': None,
                                    'source': 'replay', 'nut': {'available': False, 'timestamp': now}}))
    with TestClient(create_app(snapshot, database, tmp_path)) as client:
        for _ in range(100):
            response = client.get('/api/history?hours=8760')
            if response.status_code == 200:
                break
            time.sleep(.01)
        assert response.status_code == 200
        history = response.json()
        assert history['resolution_sec'] == DAY and len(history['points']) == 1
        assert history['available_start'] == recorded and history['available_end'] == recorded + 2
        assert history['requested_end'] - history['requested_start'] == 8760 * 3600
        assert client.get('/api/history?hours=8761').status_code == 422
        assert client.get('/api/history?hours=0').status_code == 422
        exported = client.get('/api/export.csv?hours=8760')
        assert exported.status_code == 200
        assert client.get('/api/export.csv?hours=8761').status_code == 422
    rows = list(csv.DictReader(io.StringIO(exported.text.lstrip('\ufeff'))))
    assert len(rows) == 1
    row = rows[0]
    assert float(row['cell_delta_mv']) == 11
    assert float(row['cell_delta_mv_min']) == 2 and float(row['cell_delta_mv_max']) == 20
    assert float(row['cell_1']) == 4.1
    assert float(row['cell_1_min']) == 4 and float(row['cell_1_max']) == 4.2
    assert row['calibration_revision'] == 'old-recorded-model'
    assert float(row['first']) == recorded and float(row['last']) == recorded + 2
    assert float(row['bucket_end']) - float(row['bucket_start']) == DAY
