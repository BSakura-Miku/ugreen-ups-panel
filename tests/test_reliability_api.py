import errno
import importlib
import json
from pathlib import Path
import sqlite3
import time

from fastapi.testclient import TestClient
import pytest

from ups_panel.calibration import default_config, inspect_config, save_config
from ups_panel.calibration_status import calibration_status
from ups_panel.collector import atomic_json
from ups_panel.config_target import target_identity
from ups_panel.power import PowerEstimator
from ups_panel.protocol import parse_frame
from ups_panel.storage import Store
from ups_panel.storage_health import storage_error_code
from ups_panel.telemetry import load_snapshot

app_module = importlib.import_module('ups_panel.app')
FRAME = bytes.fromhex((Path(__file__).parents[1] / 'fixtures/online.hex').read_text().splitlines()[0])
HEADERS = {'X-UPS-Calibration': '1', 'X-UPS-Calibration-Version': '2'}


def publish(tmp_path, *, target=True):
    now = time.time()
    config = default_config('none')
    path = tmp_path / 'calibration.json'
    value = {'schema': 1, 'heartbeat': now, 'source': 'usbmon',
             'sample': PowerEstimator(config=config).update(parse_frame(FRAME, now)),
             'calibration': {'config': config, 'configurable': True, 'error': None,
                             'supported_config_schemas': [1, 2]}}
    if target:
        value['calibration']['config_target'] = target_identity(path)
        value['calibration']['file_state'] = 'missing'
    atomic_json(tmp_path / 'latest.json', value)
    return value, path


def client_for(tmp_path):
    return TestClient(app_module.create_app(tmp_path / 'latest.json', tmp_path / 'history.sqlite', tmp_path))


def repair_request(revision):
    return {'profile': 'custom', 'ac_voltage_nominal_v': 12,
            'coefficients': {'base_gain': 1.1, 'charge_gain': None, 'battery_gain': None},
            'expected_revision': revision}


def test_readable_unknown_file_requires_explicit_repair_and_preserves_edit_conflicts(tmp_path):
    value, path = publish(tmp_path)
    path.write_text(json.dumps({'schema': 1, 'profile': 'community-12v', 'coefficients': {'base_gain': 9}}))
    value['calibration'].update(file_state='invalid', error='invalid')
    atomic_json(tmp_path / 'latest.json', value)
    with client_for(tmp_path) as client:
        before = client.get('/api/calibration', headers=HEADERS).json()
        assert before['readiness']['can_save'] and not before['collector_ready']
        assert before['configuration_problem'] == {'source': 'desired', 'code': 'unsupported_profile', 'profile': 'community-12v'}
        token = before['edit_revision']
        assert token != before['desired']['revision']
        path.write_text(path.read_text() + ' ')
        assert client.put('/api/calibration', headers=HEADERS, json=repair_request(token)).status_code == 409
        latest = client.get('/api/calibration', headers=HEADERS).json()
        saved = client.put('/api/calibration', headers=HEADERS, json=repair_request(latest['edit_revision']))
        assert saved.status_code == 200
        assert saved.json()['pending']
        config = json.loads(path.read_text())
        assert config['profile'] == 'custom' and config['ac_voltage_nominal_v'] == 12
        assert config['coefficients']['charge_gain'] is None


@pytest.mark.parametrize('fault,code', [('unconfigured', 'path_unconfigured'), ('mismatch', 'target_mismatch'),
                                      ('unreadable', 'file_unreadable'), ('future', 'unsupported_schema')])
def test_unsafe_save_has_specific_reason_and_preserves_file(tmp_path, fault, code):
    value, path = publish(tmp_path)
    if fault == 'unconfigured':
        value['calibration']['configurable'] = False
    elif fault == 'mismatch':
        value['calibration']['config_target']['identity'] = '0' * 64
    elif fault == 'unreadable':
        path.mkdir()
    else:
        path.write_text('{"schema":3,"profile":"custom"}')
    atomic_json(tmp_path / 'latest.json', value)
    status = calibration_status(load_snapshot(tmp_path / 'latest.json'), path)
    assert not status['readiness']['can_save']
    assert code in [issue['code'] for issue in status['readiness']['issues']]
    with client_for(tmp_path) as client:
        response = client.put('/api/calibration', headers=HEADERS,
                              json=repair_request(status['edit_revision'] or 'missing'))
        assert response.status_code == 503


def test_unknown_sample_retains_raw_data_and_explains_estimate_pause(tmp_path):
    value, path = publish(tmp_path)
    value['sample']['calibration_profile'] = 'community-12v'
    atomic_json(tmp_path / 'latest.json', value)
    with client_for(tmp_path) as client:
        live = client.get('/api/live').json()
        assert live['fresh'] and live['sample']['soc'] is not None
        assert live['calibration_validation']['reason'] == 'unsupported_profile'
        status = client.get('/api/calibration', headers=HEADERS).json()
        assert not status['readiness']['can_save']
        diagnostic = client.get('/api/diagnostics/export.json').json()
        assert diagnostic['calibration_readiness']['code'] == 'unsupported_profile'
        exported = json.dumps(diagnostic)
        assert 'community-12v' not in exported and str(tmp_path) not in exported
        assert value['calibration']['config_target']['identity'] not in exported


def test_legacy_target_is_warning_and_new_target_is_confirmed(tmp_path):
    _, path = publish(tmp_path, target=False)
    status = calibration_status(load_snapshot(tmp_path / 'latest.json'), path)
    assert status['collector_ready'] and status['readiness']['can_save']
    assert status['readiness']['code'] == 'target_unverified'
    publish(tmp_path)
    status = calibration_status(load_snapshot(tmp_path / 'latest.json'), path)
    assert status['collector_ready'] and status['readiness']['code'] is None


def test_writer_permission_failure_does_not_hide_existing_readable_history(tmp_path, monkeypatch):
    value, _ = publish(tmp_path)
    database = tmp_path / 'history.sqlite'
    original = Store(database)
    view = load_snapshot(tmp_path / 'latest.json')
    original.ingest(view)
    original.flush()

    class FailedWriter(Store):
        def ingest(self, *args, **kwargs):
            raise PermissionError(errno.EACCES, 'sensitive local path')

    monkeypatch.setattr(app_module, 'Store', FailedWriter)
    with client_for(tmp_path) as client:
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            health = client.get('/api/health').json()
            if health['storage_error_code']:
                break
            time.sleep(0.01)
        assert health['storage_error_code'] == 'permission_denied'
        history = client.get('/api/history')
        assert history.status_code == 200 and history.json()['points']
        assert client.get('/api/events').status_code == 200
        assert client.get('/api/live').json()['fresh']
        report = client.get('/api/diagnostics/export.json')
        assert report.json()['storage']['code'] == 'permission_denied'
        assert 'sensitive local path' not in report.text
        assert client.post('/api/battery-capacity/reset', headers={'X-UPS-Capacity': '1'},
                           json={'expected_epoch_id': None}).status_code == 503


@pytest.mark.parametrize('error,code', [
    (PermissionError(), 'permission_denied'),
    (OSError(errno.ENOSPC, 'private'), 'disk_full'),
    (sqlite3.OperationalError('database is locked'), 'database_locked'),
    (sqlite3.OperationalError('attempt to write a readonly database'), 'permission_denied'),
    (sqlite3.DatabaseError('database disk image is malformed'), 'database_corrupt'),
    (ValueError('private'), 'invalid_history_data'),
    (sqlite3.OperationalError('unable to open database file'), 'database_unavailable'),
])
def test_storage_errors_use_fixed_public_codes(error, code):
    assert storage_error_code(error) == code
