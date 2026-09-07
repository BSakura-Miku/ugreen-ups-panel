import asyncio
import importlib
from pathlib import Path
import sqlite3
import threading
import time

from fastapi.testclient import TestClient
import httpx

from ups_panel.cell_balance import CellBalanceMonitor
from ups_panel.collector import atomic_json
from ups_panel.protocol import parse_frame

app_module = importlib.import_module('ups_panel.app')
FRAME = bytes.fromhex((Path(__file__).parents[1] / 'fixtures/online.hex').read_text().splitlines()[0])


def publish(path, timestamp):
    value = {'schema': 1, 'source': 'usbmon', 'heartbeat': timestamp,
             'sample': parse_frame(FRAME, timestamp), 'nut': {}}
    atomic_json(path, value)
    return value


def test_observer_keeps_running_while_database_is_blocked(tmp_path, monkeypatch):
    entered, release = threading.Event(), threading.Event()

    def blocked_store(database):
        entered.set()
        assert release.wait(5)
        raise sqlite3.OperationalError('database is locked')

    monkeypatch.setattr(app_module, 'Store', blocked_store)
    path = tmp_path / 'latest.json'
    start = time.time() - 4
    publish(path, start)
    try:
        with TestClient(app_module.create_app(path, tmp_path / 'history.sqlite', tmp_path)) as client:
            assert entered.wait(2)
            first = client.get('/api/live').json()['cell_balance']
            assert first['observed'] and first['recent_sample_count'] == 1
            publish(path, start + 2)
            # Arrive before the observer's next tick: the request waits for its
            # observation even though the independent SQLite writer is blocked.
            second = client.get('/api/live').json()['cell_balance']
            assert second['observed'] and second['recent_sample_count'] == 2
            assert second['standby_duration_sec'] == 2
            assert not release.is_set()
            release.set()
    finally:
        release.set()


def test_get_timeout_does_not_count_http_reads_as_observations(tmp_path, monkeypatch):
    monitor = CellBalanceMonitor()
    monkeypatch.setattr(app_module, 'CellBalanceMonitor', lambda: monitor)
    path = tmp_path / 'latest.json'
    publish(path, time.time())
    # No lifespan means no background observer. Requests must neither start the
    # clock nor manufacture independent samples, including after the wait ends.
    async def read_without_observer():
        transport = httpx.ASGITransport(app=app_module.create_app(path, tmp_path / 'history.sqlite', tmp_path))
        async with httpx.AsyncClient(transport=transport, base_url='http://testserver') as client:
            for _ in range(2):
                result = (await client.get('/api/live')).json()['cell_balance']
                assert result['state'] == 'observing' and result['reason'] == 'not_observed'
                assert not result['observed'] and result['recent_sample_count'] == 0
                assert result['level'] is None
    asyncio.run(read_without_observer())
    assert monitor.snapshot(app_module.load_snapshot(path))['recent_sample_count'] == 0


def test_api_hides_previous_grade_immediately_when_sample_expires(tmp_path, monkeypatch):
    monitor = CellBalanceMonitor()
    monkeypatch.setattr(app_module, 'CellBalanceMonitor', lambda: monitor)
    path = tmp_path / 'latest.json'
    end = time.time()
    for offset in range(-1800, 1, 2):
        value = {'schema': 1, 'source': 'usbmon', 'heartbeat': end + offset,
                 'sample': parse_frame(FRAME, end + offset), 'fresh': True}
        monitor.ingest(value)
    publish(path, end)
    client = TestClient(app_module.create_app(path, tmp_path / 'history.sqlite', tmp_path))
    assert client.get('/api/live').json()['cell_balance']['level'] == 'good'
    publish(path, end - 60)
    result = client.get('/api/live').json()['cell_balance']
    assert result['state'] == 'unavailable' and result['level'] is None
    assert result['recent_max_delta_mv'] is None
    assert result['frequent_lowest_cell'] is None
