"""Energy ledger integration, atomic history writes, and read-only HTTP reports."""
import copy
from datetime import datetime, timedelta, timezone
import importlib
import json
from pathlib import Path
import sqlite3
from threading import Event
import time

import pytest
from fastapi.testclient import TestClient

from ups_panel.calibration import normalize_config
from ups_panel.collector import atomic_json
from ups_panel.power import PowerEstimator
from ups_panel.protocol import parse_frame
from ups_panel.storage import Store

app_module = importlib.import_module('ups_panel.app')
FRAME = bytes.fromhex((Path(__file__).parents[1] / 'fixtures/online.hex').read_text().splitlines()[0])
BEIJING = timezone(timedelta(hours=8))


def view(ts):
    config = normalize_config({'schema': 2, 'profile': 'custom', 'ac_voltage_nominal_v': 12,
                               'coefficients': {'base_gain': 1, 'charge_gain': None, 'battery_gain': None}})
    estimator = PowerEstimator(config=config)
    for offset in (-8, -6, -4, -2, 0):
        sample = parse_frame(FRAME, ts + offset)
        sample.update(input_voltage=12, adapter_input_voltage_v=12)
        sample['raw_fields']['byte_28'] = 60
        estimator.update(sample)
    assert sample['ac_input_estimate_w'] == 60
    return {'schema': 1, 'source': 'replay', 'device': {'serial': 'DEMO-ENERGY-API'},
            'heartbeat': ts, 'server_time': ts, 'fresh': True, 'sample': sample, 'nut': {}}


def total(store, ts):
    return store.usage_month(None, view(ts))['summary']['estimate_kwh']


def test_store_commits_energy_and_original_history_atomically(tmp_path, monkeypatch):
    store = Store(tmp_path / 'history.sqlite')
    ts = time.time() - 20
    store.ingest(view(ts), now=ts)
    store.flush()
    before = Store(store.path)
    assert total(before, ts) is None
    store.ingest(view(ts + 2), now=ts + 2)
    energy = total(store, ts + 2)
    assert energy == pytest.approx(60 * 2 / 3600000)
    write = store.energy_usage.write

    def fail(db):
        write(db)
        raise sqlite3.OperationalError('synthetic disk failure')

    monkeypatch.setattr(store.energy_usage, 'write', fail)
    with pytest.raises(sqlite3.OperationalError):
        store.flush()
    restored = Store(store.path)
    assert total(restored, ts + 2) is None
    assert restored.last_ts == ts
    assert total(store, ts + 2) == energy
    # Sampling continues while the failed write awaits its next retry.
    store.ingest(view(ts + 4), now=ts + 4)
    assert total(store, ts + 4) == pytest.approx(energy * 2)
    monkeypatch.setattr(store.energy_usage, 'write', write)
    store.flush()
    restored = Store(store.path)
    assert total(restored, ts + 4) == pytest.approx(energy * 2)
    assert restored.last_ts == ts + 4
    assert restored.energy_usage.state['last_ts'] == restored.last_ts
    restored.ingest(view(ts + 6), now=ts + 6)
    assert total(restored, ts + 6) == pytest.approx(energy * 3)
    restored.flush()
    restored.flush()
    restored.prune(ts + 400 * 86400)
    with restored.connect() as db:
        assert db.execute('PRAGMA user_version').fetchone()[0] == 2
        assert db.execute('PRAGMA quick_check').fetchone()[0] == 'ok'
    # Power-history retention does not delete the permanent electricity ledger.
    assert total(Store(store.path), ts + 6) == pytest.approx(energy * 3)


@pytest.fixture
def api(tmp_path, monkeypatch):
    ts = time.time() - 4
    store = Store(tmp_path / 'history.sqlite')
    store.ingest(view(ts), now=ts)
    store.ingest(view(ts + 2), now=ts + 2)
    store.flush()
    path = tmp_path / 'latest.json'
    atomic_json(path, view(ts + 2))
    ready = Event()
    monkeypatch.setattr(store, 'ingest', lambda _view: ready.set())
    monkeypatch.setattr(app_module, 'Store', lambda _database: store)
    with TestClient(app_module.create_app(path, store.path, tmp_path)) as client:
        assert ready.wait(2)
        yield client, store, path, ts


def test_reports_have_matching_calendar_totals_and_get_never_records(api):
    client, store, _, ts = api
    before = copy.deepcopy(store.energy_usage.__dict__)
    with store.connect() as db:
        database_before = list(db.iterdump())
    date = datetime.fromtimestamp(ts, BEIJING).date().isoformat()
    response = client.get('/api/energy-usage', params={'month': date[:7]})
    assert response.status_code == 200 and response.headers['cache-control'] == 'no-store'
    month = response.json()
    detail = client.get('/api/energy-usage/day', params={'date': date})
    assert detail.status_code == 200 and detail.headers['cache-control'] == 'no-store'
    day = detail.json()
    assert month['schema'] == day['schema'] == 1
    assert month['timezone'] == day['timezone'] == 'Asia/Shanghai'
    assert month['capture_fresh'] and day['capture_fresh']
    assert month['storage_error'] is None and day['storage_error'] is None
    assert month['summary']['estimate_kwh'] == day['day']['estimate_kwh']
    assert sum(hour['estimate_kwh'] or 0 for hour in day['hours']) == pytest.approx(day['day']['estimate_kwh'])
    assert month['summary']['complete_days'] == 0
    assert month['summary']['complete_day_average_kwh'] is None
    assert day['day']['covered_sec'] == 2
    assert day['day']['average_power_w'] == pytest.approx(60)
    assert len(day['hours']) == 24
    assert client.get('/api/energy-usage').status_code == 200
    assert store.energy_usage.__dict__ == before
    with store.connect() as db:
        assert list(db.iterdump()) == database_before


@pytest.mark.parametrize('query', ['month=2026-00', 'month=2026-13', 'month=26-09',
                                 'month=1969-12', 'month=9999-01', 'month=2026-09-extra'])
def test_month_rejects_invalid_or_unbounded_input(api, query):
    client, _, _, _ = api
    assert client.get('/api/energy-usage?' + query).status_code == 422


@pytest.mark.parametrize('date', ['2026-02-29', '2026-13-01', '2026-01-00', '2026-9-01',
                                 '0000-01-01', '1969-12-31', '9999-12-31', '2026-09-01T00:00'])
def test_day_rejects_invalid_dates(api, date):
    client, _, _, _ = api
    assert client.get('/api/energy-usage/day', params={'date': date}).status_code == 422


def test_future_and_empty_days_are_unknown_instead_of_false_zero(api):
    client, _, _, _ = api
    past = client.get('/api/energy-usage/day?date=2000-01-01').json()['day']
    future = client.get('/api/energy-usage/day?date=2099-01-01').json()['day']
    assert past['status'] == 'no_data' and past['estimate_kwh'] is None
    assert future['status'] == 'future' and future['estimate_kwh'] is None
    assert future['expected_sec'] == 0


def test_stale_capture_keeps_recorded_energy_and_query_errors_are_503(api, monkeypatch):
    client, store, path, ts = api
    before = client.get('/api/energy-usage').json()['summary']['estimate_kwh']
    atomic_json(path, view(ts - 60))
    result = client.get('/api/energy-usage').json()
    assert result['capture_fresh'] is False and result['summary']['estimate_kwh'] == before
    def fail(*args):
        raise sqlite3.OperationalError('synthetic read error')
    monkeypatch.setattr(store, 'usage_month', fail)
    monkeypatch.setattr(store, 'usage_day', fail)
    assert client.get('/api/energy-usage').status_code == 503
    assert client.get('/api/energy-usage/day?date=2026-09-09').status_code == 503
