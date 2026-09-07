"""History queries keep a bounded working set and a coherent WAL snapshot."""
from concurrent.futures import ThreadPoolExecutor
import json
from threading import Event, current_thread
import tracemalloc

import pytest

from ups_panel.calibration import normalize_config
from ups_panel.storage import DAY, Store


def test_wide_minute_query_memory_tracks_output_groups_not_encoded_source_rows(tmp_path):
    store = Store(tmp_path / 'history.db')
    now = 20000 * DAY
    count = 12000
    context = json.dumps({'calibration_revision': 'retained-model', 'formula_version': 2,
                          'calibration_coefficients': {'scale': 1.25}, 'decoder_version': 4})
    metrics = json.dumps({f'metric_{index}': [30.0, 3, 9.0, 11.0] for index in range(20)})
    with store.connect() as db:
        db.executemany('INSERT INTO samples VALUES(?,?,?,?,?,?,?,?)',
                       ((60, now - index * 60, now - index * 60 + 1,
                         now - index * 60 + 5, 3, 'online', metrics, context)
                        for index in range(1, count + 1)))
    encoded_size = count * (len(metrics) + len(context))
    tracemalloc.start()
    try:
        history = store.history(2160, now=now)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    # A range with over ten MB of encoded source produces only ~112 groups.
    # Allow ample interpreter overhead, while rejecting a second complete copy
    # of source JSON in the reader's working set. No wall-clock assertion.
    assert peak < encoded_size / 2
    assert len(history['points']) <= 113
    assert sum(point['count'] for point in history['points']) == count * 3
    assert all(point['values']['metric_0'] == 10 and point['min']['metric_0'] == 9
               and point['max']['metric_0'] == 11 for point in history['points'])
    with store.connect() as db:
        assert db.execute('SELECT COUNT(*),SUM(count) FROM samples').fetchone() == (count, count * 3)


def test_history_reader_does_not_block_flush_and_keeps_one_committed_snapshot(tmp_path, monkeypatch):
    store = Store(tmp_path / 'history.db')
    now = 20000 * DAY
    config = normalize_config({'schema': 2, 'profile': 'custom', 'ac_voltage_nominal_v': 12,
                               'coefficients': {'base_gain': 1, 'charge_gain': None, 'battery_gain': 2}})

    def ingest(timestamp, mode='online', power=10):
        store.ingest({'fresh': True, 'source': 'usbmon', 'device': {'serial': 'SYNTHETIC-WAL'},
                      'sample': {
            'timestamp': timestamp, 'mode': mode, 'soc': 90, 'cell_delta_mv': 4,
            'formula_version': 2, 'decoder_version': 4, 'calibration_schema': 2,
            'ac_voltage_nominal_v': 12, 'calibration_profile': 'custom',
            'calibration_coefficients': config['coefficients'], 'calibration_revision': config['revision'],
            'battery_estimate_basis': 'us3000_battery_custom_' + config['revision'],
            'battery_discharge_power_candidate_w': power}}, now=timestamp)

    ingest(now - 8)
    ingest(now - 6, 'battery')
    store.flush()
    reading, resume = Event(), Event()
    original = json.loads

    def pause_reader(value, *args, **kwargs):
        if current_thread().name.startswith('history-reader') and not reading.is_set():
            reading.set()
            assert resume.wait(5), 'Reader was not resumed after concurrent flush'
        return original(value, *args, **kwargs)

    monkeypatch.setattr(json, 'loads', pause_reader)
    with ThreadPoolExecutor(max_workers=1, thread_name_prefix='history-reader') as executor:
        future = executor.submit(store.history, 2160, now)
        try:
            assert reading.wait(5), 'Reader did not begin its SQLite snapshot'
            ingest(now - 4, 'battery', 20)
            ingest(now - 2, 'battery', 30)
            ingest(now - 1)
            store.flush()
            assert not store.pending and not store.battery_sessions.pending
            assert not store.battery_sessions.energy.pending
            with store.connect() as db:
                assert db.execute('SELECT resolution,SUM(count) FROM samples GROUP BY resolution').fetchall() == [
                    (10, 5), (60, 5), (DAY, 5)]
                assert float(db.execute("SELECT value FROM meta WHERE key='last_ts'").fetchone()[0]) == now - 1
                assert float(db.execute("SELECT value FROM meta WHERE key='daily_rollup_cursor'").fetchone()[0]) == now - 1
                assert db.execute('SELECT COUNT(*) FROM battery_sessions').fetchone()[0] == 1
                assert db.execute('SELECT COUNT(*) FROM battery_session_energy').fetchone()[0] == 1
        finally:
            resume.set()
        previous = future.result(timeout=5)
    current = store.history(2160, now=now)
    assert sum(point['count'] for point in previous['points']) == 2
    assert previous['available_end'] == now - 6
    assert sum(point['count'] for point in current['points']) == 5
    assert current['available_end'] == now - 1
    record = store.battery_history(1, now=now)['records'][0]
    assert record['status'] == 'complete'
    assert record['energy']['interval_count'] == 2
    assert record['energy']['estimate_wh'] == pytest.approx(160 / 3600)
