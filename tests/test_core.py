import json
from pathlib import Path
import struct
import time

from fastapi.testclient import TestClient
import pytest

from ups_panel.app import create_app, load_snapshot
from ups_panel.collector import atomic_json
from ups_panel.protocol import parse_frame
from ups_panel.storage import Store
from ups_panel.usbmon import decode_event, discover

FRAMES = [bytes.fromhex(x) for x in (Path(__file__).parents[1] / 'fixtures/online.hex').read_text().splitlines()]


def snapshot(timestamp=None):
    timestamp = time.time() if timestamp is None else timestamp
    return {'schema': 1, 'heartbeat': timestamp, 'source': 'usbmon', 'sample': parse_frame(FRAMES[0], timestamp, -2), 'nut': {}}


def test_real_frames():
    samples = [parse_frame(frame) for frame in FRAMES]
    assert len(samples) == 6
    assert samples[0]['cells'] == [4.106, 4.104, 4.103, 4.101]
    assert samples[0]['power_w'] == pytest.approx(37.489)
    assert samples[0]['power_verified'] is False
    assert samples[0]['current_verified'] is False
    assert samples[0]['power_source'] == 'rail18_times_current24_hypothesis_v2'
    assert all(s['soc'] == 100 and s['mode'] == 'online' and s['runtime_sec'] is None for s in samples)


@pytest.mark.parametrize('frame', [FRAMES[0][:-1], b'\0' * 64, bytes([0x71]) + b'\0' * 63])
def test_reject_invalid(frame):
    with pytest.raises(ValueError):
        parse_frame(frame)


def test_mode_semantics():
    b = bytearray(FRAMES[0]); b[7] = 0x21; b[16:18] = (65535).to_bytes(2, 'big')
    s = parse_frame(b)
    assert s['input_voltage'] is None and s['runtime_sec'] is None
    assert s['power_w'] == round(s['output_voltage'] * s['current'], 3)
    assert s['power_verified'] is False
    b[7] = 0x99
    assert parse_frame(b)['power_w'] is None
    b[7] = 0x36; b[29:31] = (600).to_bytes(2, 'big')
    assert parse_frame(b)['charge_current_ma'] is None
    assert parse_frame(b)['load_percent'] is None


def header(status=-2, length=64, cap=64):
    h = bytearray(64); h[8:12] = bytes([67, 1, 129, 2])
    struct.pack_into('=H', h, 12, 3)
    struct.pack_into('=q', h, 16, 100)
    struct.pack_into('=iII', h, 28, status, length, cap)
    return h


def test_cancelled_full_payload_is_accepted():
    assert decode_event(header(), FRAMES[0], 3, 2)['usb_status'] == -2
    assert decode_event(header(0), FRAMES[0], 3, 2)


def test_batched_reports_keep_newest_only():
    payload = FRAMES[0] + FRAMES[1]
    assert decode_event(header(length=128, cap=128), payload, 3, 2)['frame'] == FRAMES[1]
    assert decode_event(header(length=128, cap=128), FRAMES[0] + b'\0' * 64, 3, 2) is None
    assert decode_event(header(length=128, cap=64), FRAMES[0], 3, 2) is None


@pytest.mark.parametrize('h,p,b,d', [(header(cap=32), FRAMES[0][:32], 3, 2), (header(-32), FRAMES[0], 3, 2), (header(), FRAMES[0], 3, 5), (header(), FRAMES[0], 4, 2)])
def test_unrelated_or_partial_transfer_rejected(h, p, b, d):
    assert decode_event(h, p, b, d) is None


def test_discovery_reconnect_and_ambiguity(tmp_path):
    def device(name, devnum, serial):
        d = tmp_path / name; d.mkdir()
        for key, value in {'idVendor': '2b89', 'idProduct': 'ffff', 'busnum': '3', 'devnum': str(devnum), 'serial': serial}.items():
            (d / key).write_text(value)
    device('3-6', 2, 'DC600')
    assert discover(tmp_path)['device'] == 2
    (tmp_path / '3-6/devnum').write_text('7')
    assert discover(tmp_path)['device'] == 7
    device('3-5', 9, 'OTHER')
    with pytest.raises(RuntimeError): discover(tmp_path)
    assert discover(tmp_path, 'DC600')['device'] == 7


def test_atomic_and_stale(tmp_path):
    p = tmp_path / 'latest.json'; atomic_json(p, snapshot(100))
    assert load_snapshot(p, 105)['fresh']
    assert not load_snapshot(p, 111)['fresh']
    assert not load_snapshot(p, 90)['fresh']
    v = snapshot(100); v['heartbeat'] = 80; atomic_json(p, v)
    assert not load_snapshot(p, 101)['fresh']
    p.write_text('{')
    assert load_snapshot(p)['sample'] is None
    assert not list(tmp_path.glob('.snapshot-*'))


def test_aggregation_duplicates_restart_retention(tmp_path):
    store = Store(tmp_path / 'h.db')
    for ts in (100, 102, 102, 104):
        store.ingest({'fresh': True, 'sample': parse_frame(FRAMES[0], ts)}, now=ts)
    store.flush()
    points = store.history(1, now=105)['points']
    assert len(points) == 1 and points[0]['count'] == 3
    assert points[0]['values']['cell_1'] == 4.106
    restarted = Store(store.path)
    restarted.ingest({'fresh': True, 'sample': parse_frame(FRAMES[0], 104)}, now=105)
    restarted.flush()
    assert restarted.history(1, now=105)['points'][0]['count'] == 3
    restarted.ingest({'fresh': False, 'sample': None}, now=120)
    assert restarted.events()[0]['detail'] == 'offline'
    restarted.prune(100 + 8 * 86400)
    with restarted.connect() as db:
        assert db.execute('SELECT count(*) FROM samples WHERE resolution=10').fetchone()[0] == 0
        assert db.execute('SELECT count(*) FROM samples WHERE resolution=60').fetchone()[0] == 1


def test_api_and_csv(tmp_path):
    p = tmp_path / 'latest.json'; atomic_json(p, snapshot())
    app = create_app(p, tmp_path / 'h.db', tmp_path)
    with TestClient(app) as client:
        assert client.get('/api/live').json()['fresh']
        assert client.get('/api/health').status_code == 200
        assert client.get('/api/history?hours=9999').status_code == 422
        assert client.post('/api/live').status_code == 405
        # Wait only for startup background store construction.
        for _ in range(100):
            response = client.get('/api/export.csv')
            if response.status_code == 200: break
            time.sleep(.01)
        assert 'timestamp,mode,count' in response.text
        assert 'power_quality' in response.text
        atomic_json(p, snapshot(time.time() - 20))
        assert not client.get('/api/live').json()['fresh']


def test_storage_failure_does_not_break_live(tmp_path):
    p = tmp_path / 'latest.json'; atomic_json(p, snapshot())
    blocker = tmp_path / 'file'; blocker.write_text('x')
    with TestClient(create_app(p, blocker / 'h.db', tmp_path)) as client:
        assert client.get('/api/live').status_code == 200
        assert client.get('/api/history').status_code == 503


def test_observed_battery_transition_uses_rail_not_pack():
    frame = bytes.fromhex((Path(__file__).parents[1] / 'fixtures/battery-local.hex').read_text().splitlines()[0])
    s = parse_frame(frame)
    assert s['mode'] == 'battery'
    assert s['output_voltage'] == 12.054
    assert s['battery_voltage'] == 16.144
    assert s['dc_power_estimate_w'] == 38.320
    assert s['power_verified'] is False
    assert s['runtime_sec'] is None and s['load_percent'] is None
    assert abs(sum(s['cells']) - s['battery_voltage']) < .05


def test_old_history_has_no_new_formula_metric(tmp_path):
    old = parse_frame(FRAMES[0], 100)
    old.pop('dc_power_estimate_w')
    old.pop('formula_version')
    new = parse_frame(FRAMES[0], 102)
    store = Store(tmp_path / 'h.db')
    for s in (old, new): store.ingest({'fresh': True, 'sample': s}, now=s['timestamp'])
    store.flush()
    with store.connect() as db:
        rows = [json.loads(row[0]) for row in db.execute('SELECT metrics FROM samples WHERE resolution=10')]
    assert len(rows) == 2
    assert sum(v.get('power_w', [0, 0])[1] for v in rows) == 1
    assert sum(v.get('dc_power_estimate_w', [0, 0])[1] for v in rows) == 1


def test_input_decay_and_output_regulation_from_real_transition():
    frames = [bytes.fromhex(x) for x in (Path(__file__).parents[1] / 'fixtures/battery-local.hex').read_text().splitlines()]
    samples = [parse_frame(f) for f in frames]
    assert len(samples) == 17
    assert samples[0]['input_voltage'] == 10.734
    assert samples[-2]['input_voltage'] == 1.007
    assert samples[-1]['input_voltage'] == 19.021
    assert all(12.05 < s['output_voltage'] < 12.06 for s in samples)
    b = bytearray(frames[0]); b[16:18] = b'\0\0'
    assert parse_frame(b)['input_voltage'] == 0
    assert parse_frame(b)['output_voltage'] == 12.054


def test_voltage_history_does_not_mix_old_mislabelled_channels(tmp_path):
    new = parse_frame(FRAMES[0], 102)
    old = dict(new, timestamp=100, decoder_version=2, input_voltage=99)
    old.pop('adapter_input_voltage_v'); old.pop('ups_output_voltage_v')
    store = Store(tmp_path / 'h.db')
    for s in (old, new): store.ingest({'fresh': True, 'sample': s}, now=s['timestamp'])
    store.flush()
    points = store.history(1, now=105)['points']
    assert len(points) == 2
    assert points[0]['values']['input_voltage'] == 99
    assert 'adapter_input_voltage_v' not in points[0]['values']
    assert points[1]['values']['adapter_input_voltage_v'] == new['input_voltage']
    assert points[1]['values']['ups_output_voltage_v'] == new['output_voltage']


def test_real_recharge_frames_and_separate_battery_power():
    frames = [bytes.fromhex(x) for x in (Path(__file__).parents[1] / 'fixtures/charging-local.hex').read_text().splitlines()]
    assert len(frames) == 12
    for f in frames:
        s = parse_frame(f)
        assert s['mode'] == 'charging'
        assert .6 < s['battery_charge_current_candidate_a'] < .7
        assert s['battery_discharge_current_candidate_a'] is None
        assert s['battery_discharge_power_candidate_w'] is None
        assert s['dc_power_estimate_w'] == round(s['output_voltage'] * s['current'], 3)
        assert s['battery_charge_power_candidate_w'] == round(s['battery_voltage'] * s['battery_charge_current_candidate_a'], 3)
        assert not s['battery_current_verified'] and not s['power_verified']
        assert not s['warnings']
    b = bytearray(frames[0]); b[29:31] = b'\xff\xff'
    invalid = parse_frame(b)
    assert invalid['battery_charge_power_candidate_w'] is None
    assert invalid['dc_power_estimate_w'] is not None
    assert invalid['warnings']


def test_battery_candidates_are_mode_gated():
    frame = bytes.fromhex((Path(__file__).parents[1] / 'fixtures/battery-local.hex').read_text().splitlines()[0])
    s = parse_frame(frame)
    assert s['battery_discharge_current_candidate_a'] > 2
    assert s['battery_discharge_power_candidate_w'] > 30
    assert s['battery_charge_current_candidate_a'] is None
    b = bytearray(frame); b[7] = 0x26
    s = parse_frame(b)
    assert s['battery_discharge_current_candidate_a'] is None
    b[7] = 0x99
    s = parse_frame(b)
    assert s['dc_power_estimate_w'] is None
    assert s['battery_charge_power_candidate_w'] is None
    assert s['battery_discharge_power_candidate_w'] is None


@pytest.mark.parametrize('field,value', [('schema', True), ('heartbeat', True), ('heartbeat', float('nan')),
                                       ('timestamp', True), ('soc', 101), ('current', True),
                                       ('power_w', float('inf'))])
def test_malformed_snapshot_is_unavailable_not_an_api_failure(tmp_path, field, value):
    p = tmp_path / 'latest.json'
    payload = snapshot(100)
    if field in ('schema', 'heartbeat'):
        payload[field] = value
    else:
        payload['sample'][field] = value
    p.write_text(json.dumps(payload))
    assert load_snapshot(p, now=101)['sample'] is None
    with TestClient(create_app(p, tmp_path / 'h.db', tmp_path)) as client:
        assert client.get('/api/live').status_code == 200
        assert not client.get('/api/live').json()['fresh']


def test_snapshot_size_overflow_and_invalid_nut_are_handled(tmp_path):
    p = tmp_path / 'latest.json'
    p.write_bytes(b' ' * 65537)
    assert load_snapshot(p, 101)['read_error'] == 'ValueError'
    payload = snapshot(100)
    payload['nut'] = None
    p.write_text(json.dumps(payload))
    assert load_snapshot(p, 101)['fresh']
    assert load_snapshot(p, 101)['nut'] == {'available': False}
    payload['sample']['diagnostic_number'] = 'overflow'
    p.write_text(json.dumps(payload).replace('"overflow"', '1e9999'))
    assert not load_snapshot(p, 101)['fresh']


def test_legacy_database_migrates_without_merging_or_inventing_calibration(tmp_path):
    import sqlite3
    path = tmp_path / 'history.db'
    with sqlite3.connect(path) as db:
        db.execute('''CREATE TABLE samples (resolution INTEGER,bucket INTEGER,first REAL,last REAL,
                   count INTEGER,mode TEXT,metrics TEXT,PRIMARY KEY(resolution,bucket,mode))''')
        db.execute('INSERT INTO samples VALUES(10,100,100,100,1,?,?)',
                   ('online', json.dumps({'ac_input_estimate_w': [60, 1, 60, 60]})))
        db.execute('PRAGMA user_version=1')
    store = Store(path)
    current = parse_frame(FRAMES[0], 102)
    current.update(ac_input_estimate_w=70, calibration_profile='local-19v-v1', ac_estimate_model='us3000_19v_v1')
    store.ingest({'fresh': True, 'sample': current}, now=102)
    store.flush()
    points = store.history(1, 105)['points']
    assert len(points) == 2
    assert points[0]['values']['ac_input_estimate_w'] == 60
    assert points[0]['context'] == {'provenance': 'legacy_unrecorded'}
    assert points[1]['values']['ac_input_estimate_w'] == 70
    assert points[1]['context']['calibration_profile'] == 'local-19v-v1'
    with store.connect() as db:
        assert db.execute('PRAGMA user_version').fetchone()[0] == 2
    with pytest.raises(sqlite3.ProgrammingError):
        db.execute('SELECT 1')
    restarted = Store(path)
    assert restarted.history(1, 105)['points'] == points


def test_distinct_model_contexts_never_get_averaged_in_wide_history(tmp_path):
    store = Store(tmp_path / 'h.db')
    for ts, model, power in ((100, 'model-a', 40), (102, 'model-b', 80)):
        s = parse_frame(FRAMES[0], ts)
        s.update(ac_input_estimate_w=power, ac_estimate_model=model)
        store.ingest({'fresh': True, 'sample': s}, now=ts)
    store.flush()
    points = store.history(168, now=105)['points']
    assert len(points) == 2
    assert [p['values']['ac_input_estimate_w'] for p in points] == [40, 80]
    assert {p['context']['ac_estimate_model'] for p in points} == {'model-a', 'model-b'}


def test_invalid_optional_current_does_not_hide_valid_battery_state():
    frame = bytearray(FRAMES[0])
    frame[24:26] = b'\xff\xff'
    result = parse_frame(frame)
    assert result['mode'] == 'online'
    assert result['soc'] == 100
    assert result['current'] is None and result['dc_power_estimate_w'] is None
    assert result['ups_output_voltage_v'] is not None
    assert result['warnings']
    frame[18:20] = b'\xff\xff'
    result = parse_frame(frame)
    assert result['ups_output_voltage_v'] is None
    assert result['battery_voltage'] > 16


def test_history_buffer_stays_bounded_during_persistent_disk_failure(tmp_path, monkeypatch):
    import ups_panel.storage as storage
    monkeypatch.setattr(storage, 'MAX_PENDING_BUCKETS', 4)
    store = Store(tmp_path / 'h.db')
    store.last_prune = 100
    def unavailable():
        raise OSError('disk unavailable')
    monkeypatch.setattr(store, 'flush', unavailable)
    store.last_flush = -100
    for ts in range(100, 200, 10):
        with pytest.raises(OSError):
            store.ingest({'fresh': True, 'sample': parse_frame(FRAMES[0], ts)}, now=ts)
    assert len(store.pending) <= 4
    assert store.dropped_buckets > 0
    assert max(entry['last'] for entry in store.pending.values()) == 190


def test_csv_exports_recorded_model_context(tmp_path):
    import csv
    import io
    now = time.time()
    database = tmp_path / 'history.db'
    store = Store(database)
    recorded = parse_frame(FRAMES[0], now - 2)
    recorded.update(calibration_profile='local-19v-v1', ac_estimate_model='us3000_19v_v1',
                    ac_estimate_quality='extrapolated', ac_input_estimate_w=90.0)
    store.ingest({'fresh': True, 'sample': recorded}, now=now)
    store.flush()
    snap = tmp_path / 'latest.json'
    atomic_json(snap, snapshot(now))
    with TestClient(create_app(snap, database, tmp_path)) as client:
        for _ in range(100):
            response = client.get('/api/export.csv')
            if response.status_code == 200: break
            time.sleep(.01)
        rows = list(csv.DictReader(io.StringIO(response.text.lstrip('\ufeff'))))
    estimated = [row for row in rows if row['ac_input_estimate_w']]
    assert len(estimated) == 1
    assert estimated[0]['calibration_profile'] == 'local-19v-v1'
    assert estimated[0]['ac_estimate_model'] == 'us3000_19v_v1'
    assert estimated[0]['ac_estimate_quality'] == 'extrapolated'
