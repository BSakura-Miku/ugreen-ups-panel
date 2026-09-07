"""Persistent reference transactions and browser-write boundaries."""
import copy
import importlib
import json
from pathlib import Path
import sqlite3
from threading import Event
from concurrent.futures import ThreadPoolExecutor
import time

import pytest
from fastapi.testclient import TestClient

from ups_panel.calibration import normalize_config
from ups_panel.collector import atomic_json
from ups_panel.protocol import parse_frame
from ups_panel.storage import Store

app_module = importlib.import_module('ups_panel.app')
FRAME = bytes.fromhex((Path(__file__).parents[1] / 'fixtures/online.hex').read_text().splitlines()[0])


def view(ts, *, mode='online', soc=95):
    config = normalize_config({'schema': 1, 'profile': 'local-19v-v1'})
    sample = parse_frame(FRAME, ts)
    sample.update(mode=mode, soc=soc, battery_discharge_power_candidate_w=40,
                  calibration_profile=config['profile'], calibration_revision=config['revision'],
                  calibration_coefficients=config['coefficients'],
                  battery_estimate_basis='nominal_43_2wh_soc_v1')
    return {'schema': 1, 'source': 'usbmon', 'device': {'serial': 'SYNTHETIC-CAPACITY-API'},
            'heartbeat': ts, 'server_time': ts, 'fresh': True, 'sample': sample, 'nut': {}}


def baseline(store, start):
    store.ingest(view(start), now=start)
    # 60 seconds for the complete 10-point interval, with every sample observed.
    for offset in range(0, 65, 2):
        item = view(start + 2 + offset, mode='battery', soc=91 if offset == 0 else 90 - (offset - 2) // 6)
        store.ingest(item, now=item['server_time'])
    return item


def test_reference_and_legacy_history_commit_together_and_survive_prune(tmp_path):
    path = tmp_path / 'history.sqlite'
    store = Store(path)
    item = baseline(store, 1000)
    before = store.capacity_reference(item)
    assert before['baseline'] and before['baseline']['estimate_wh'] > 0
    store.flush()
    store.prune(item['server_time'] + 370 * 86400)
    restored = Store(path).capacity_reference(item)
    assert restored['baseline'] == before['baseline']
    assert restored['epoch'] == before['epoch']
    with store.connect() as db:
        assert db.execute('PRAGMA user_version').fetchone()[0] == 2
        assert db.execute('PRAGMA quick_check').fetchone()[0] == 'ok'


def test_failed_reset_commit_restores_original_reference_and_archives_nothing(tmp_path, monkeypatch):
    store = Store(tmp_path / 'history.sqlite')
    item = baseline(store, 1000)
    store.flush()
    before = store.capacity_reference(item)
    write = store.battery_capacity.write

    def fail_after_writes(db):
        write(db)
        raise sqlite3.OperationalError('synthetic full disk')

    monkeypatch.setattr(store.battery_capacity, 'write', fail_after_writes)
    with pytest.raises(sqlite3.OperationalError):
        store.reset_capacity_reference(item, before['epoch']['id'])

    assert store.capacity_reference(item) == before
    restored = Store(store.path).capacity_reference(item)
    assert restored['epoch'] == before['epoch'] and restored['baseline'] == before['baseline']
    with store.connect() as db:
        assert db.execute('SELECT COUNT(*) FROM battery_capacity_epochs').fetchone()[0] == 0
    monkeypatch.setattr(store.battery_capacity, 'write', write)
    after = store.reset_capacity_reference(item, before['epoch']['id'])
    assert after['epoch']['id'] != before['epoch']['id'] and after['baseline'] is None
    with store.connect() as db:
        archive = json.loads(db.execute('SELECT payload FROM battery_capacity_epochs').fetchone()[0])
        assert archive['baseline'] == before['baseline']
    with pytest.raises(ValueError, match='reference_changed'):
        store.reset_capacity_reference(item, before['epoch']['id'])


def test_reset_reads_current_capture_only_after_acquiring_writer_lock(tmp_path):
    store = Store(tmp_path / 'history.sqlite')
    initial = view(1000)
    store.ingest(initial, now=1000)
    old = store.capacity_reference(initial)['epoch']['id']
    captured = [initial]
    requested, read = Event(), Event()
    def latest():
        read.set()
        return captured[0]
    def reset():
        requested.set()
        return store.reset_capacity_reference(latest, old)
    with ThreadPoolExecutor(max_workers=1) as executor:
        with store._lock:
            future = executor.submit(reset)
            assert requested.wait(2) and not read.is_set()
            captured[0] = view(1002)
            store.ingest(captured[0], now=1002)
        result = future.result(timeout=3)
    assert read.is_set() and result['epoch']['activated_at'] == 1002
    with pytest.raises(ValueError, match='sample_changed'):
        store.reset_capacity_reference(initial, result['epoch']['id'])


@pytest.fixture
def api(tmp_path, monkeypatch):
    path = tmp_path / 'latest.json'
    item = view(time.time())
    atomic_json(path, item)
    store = Store(tmp_path / 'history.sqlite')
    store.ingest(item, now=item['server_time'])
    store.flush()
    ready = Event()
    # Freeze writer input to isolate HTTP reads/writes from background cadence.
    monkeypatch.setattr(store, 'ingest', lambda _view: ready.set())
    monkeypatch.setattr(app_module, 'Store', lambda _database: store)
    with TestClient(app_module.create_app(path, store.path, tmp_path)) as client:
        assert ready.wait(2)
        yield client, store, path


def post(client, epoch_id, **kwargs):
    headers = {'x-ups-capacity': '1', 'content-type': 'application/json', **kwargs.pop('headers', {})}
    return client.post('/api/battery-capacity/reset', headers=headers,
                       json={'expected_epoch_id': epoch_id}, **kwargs)


def test_get_is_read_only_and_successful_reset_is_persistent(api):
    client, store, _ = api
    before = copy.deepcopy(store.battery_capacity.__dict__)
    response = client.get('/api/battery-capacity')
    assert response.status_code == 200 and response.headers['cache-control'] == 'no-store'
    report = response.json()
    assert report['schema'] == 1 and report['baseline'] is None and report['comparison'] is None
    assert report['epoch'] is not None
    for _ in range(3):
        assert client.get('/api/battery-capacity').status_code == 200
    assert store.battery_capacity.__dict__ == before
    old = report['epoch']['id']
    reset = post(client, old)
    assert reset.status_code == 200
    new = reset.json()['epoch']['id']
    assert new != old
    assert Store(store.path).capacity_reference(view(time.time()))['epoch']['id'] == new
    assert post(client, old).status_code == 409


@pytest.mark.parametrize('headers', [
    {'x-ups-capacity': '0'}, {'content-type': 'text/plain'},
    {'origin': 'https://untrusted.example'}, {'origin': 'https://testserver/elsewhere'},
    {'origin': 'https://user@testserver'}, {'origin': 'file://testserver'},
    {'origin': 'https://[invalid'}, {'sec-fetch-site': 'cross-site'}, {'sec-fetch-site': 'same-site'},
])
def test_reset_rejects_cross_site_and_non_json_browser_requests(api, headers):
    client, store, _ = api
    old = client.get('/api/battery-capacity').json()['epoch']['id']
    before = copy.deepcopy(store.battery_capacity.__dict__)
    assert post(client, old, headers=headers).status_code == 403
    assert store.battery_capacity.__dict__ == before


@pytest.mark.parametrize('payload', ['null', '[]', '{}', '{"expected_epoch_id":2}',
                                    '{"expected_epoch_id":null,"extra":1}', 'x' * 1025])
def test_reset_rejects_invalid_body(api, payload):
    client, store, _ = api
    before = copy.deepcopy(store.battery_capacity.__dict__)
    response = client.post('/api/battery-capacity/reset', content=payload,
                           headers={'x-ups-capacity': '1', 'content-type': 'application/json'})
    assert response.status_code == (413 if len(payload) > 1024 else 400)
    assert store.battery_capacity.__dict__ == before


def test_stale_capture_cannot_reset_and_storage_error_is_reported(api, monkeypatch):
    client, store, path = api
    old = client.get('/api/battery-capacity').json()['epoch']['id']
    atomic_json(path, view(time.time() - 60))
    assert post(client, old).status_code == 409
    atomic_json(path, view(time.time()))
    def unavailable(*args):
        raise sqlite3.OperationalError('synthetic read failure')
    monkeypatch.setattr(store, 'capacity_reference', unavailable)
    assert client.get('/api/battery-capacity').status_code == 503
    monkeypatch.setattr(store, 'reset_capacity_reference', unavailable)
    assert post(client, old).status_code == 503
