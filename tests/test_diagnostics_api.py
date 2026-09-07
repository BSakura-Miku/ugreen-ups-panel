import csv
import importlib
import io
import json
from pathlib import Path
import sqlite3
import time

from fastapi.testclient import TestClient
import pytest

from ups_panel.collector import atomic_json
from ups_panel.protocol import parse_frame
from ups_panel.raw_observation import RawObservationMonitor

app_module = importlib.import_module('ups_panel.app')
FRAME = bytes.fromhex((Path(__file__).parents[1] / 'fixtures/online.hex').read_text().splitlines()[0])


def publish(path, now):
    value = {'schema': 1, 'source': 'usbmon', 'heartbeat': now,
             'sample': parse_frame(FRAME, now), 'nut': {'available': True, 'timestamp': now,
             'target': 'private@192.0.2.7', 'values': {'ups.status': 'OL', 'battery.runtime': '65535'}}}
    atomic_json(path, value)
    value['fresh'] = True
    return value


def test_diagnostics_and_downloads_are_readonly_and_use_same_window(tmp_path, monkeypatch):
    monitor = RawObservationMonitor()
    monkeypatch.setattr(app_module, 'RawObservationMonitor', lambda: monitor)
    path = tmp_path / 'latest.json'
    now = time.time()
    value = publish(path, now)
    monitor.ingest(value, now=now)
    database = tmp_path / 'history.sqlite'
    client = TestClient(app_module.create_app(path, database, tmp_path))
    before = path.read_bytes()
    for _ in range(3):
        response = client.get('/api/diagnostics?minutes=15')
        assert response.status_code == 200 and response.headers['cache-control'] == 'no-store'
        data = response.json()
        assert data['observation']['count'] == 1 and data['observation']['window_sec'] == 900
        assert data['observation']['latest']['observed']
        export = client.get('/api/diagnostics/export.json?minutes=15')
        assert export.status_code == 200 and 'attachment' in export.headers['content-disposition']
        assert export.json()['observation']['points'] == data['observation']['points']
        assert 'private@' not in export.text
        csv_result = client.get('/api/diagnostics/export.csv?minutes=15')
        assert csv_result.status_code == 200
        rows = list(csv.DictReader(io.StringIO(csv_result.text.lstrip('\ufeff'))))
        assert len(rows) == 1 and int(rows[0]['byte_26']) == data['observation']['points'][0]['byte_26']
    assert path.read_bytes() == before and not database.exists()
    assert monitor.snapshot(value, now=now)['count'] == 1


def test_requests_never_advance_an_unstarted_observer(tmp_path, monkeypatch):
    path = tmp_path / 'latest.json'
    publish(path, time.time())
    client = TestClient(app_module.create_app(path, tmp_path / 'history.sqlite', tmp_path))
    for _ in range(3):
        result = client.get('/api/diagnostics').json()['observation']
        assert result['count'] == 0 and result['latest']['fresh'] and not result['latest']['observed']


@pytest.mark.parametrize('route', ['/api/diagnostics', '/api/diagnostics/export.json', '/api/diagnostics/export.csv'])
def test_missing_snapshot_stays_diagnosable_and_rejects_unbounded_windows(tmp_path, route):
    client = TestClient(app_module.create_app(tmp_path / 'missing.json', tmp_path / 'history.sqlite', tmp_path))
    assert client.get(route).status_code == 200
    for query in ('0', '61', '1000000', 'abc'):
        assert client.get(route + '?minutes=' + query).status_code == 422


def test_storage_failure_does_not_stop_raw_observations_and_expiry_hides_latest(tmp_path, monkeypatch):
    def failed_store(path):
        raise sqlite3.OperationalError('read only database')
    monkeypatch.setattr(app_module, 'Store', failed_store)
    path = tmp_path / 'latest.json'
    now = time.time() - 2
    publish(path, now)
    with TestClient(app_module.create_app(path, tmp_path / 'history.sqlite', tmp_path)) as client:
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            result = client.get('/api/diagnostics').json()
            if result['observation']['count'] == 1:
                break
            time.sleep(0.02)
        assert result['observation']['count'] == 1
        publish(path, now + 1)
        while time.monotonic() < deadline:
            result = client.get('/api/diagnostics').json()
            if result['observation']['count'] == 2:
                break
            time.sleep(0.02)
        assert result['observation']['count'] == 2
        assert next(check for check in result['connection']['checks'] if check['id'] == 'storage')['status'] == 'warning'
        publish(path, now - 60)
        result = client.get('/api/diagnostics').json()
        assert not result['capture_fresh'] and result['observation']['count'] == 2
        assert not result['observation']['latest']['fresh']
        assert result['observation']['latest']['byte_26'] is None
