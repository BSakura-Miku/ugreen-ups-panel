"""The web bridge must never turn cross-origin or malformed requests into host actions."""
import json

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from ups_panel.update_api import install_update_routes
from ups_panel.update_client import UpdateError, unavailable

KEY = 'A' * 43
HEADERS = {'X-UPS-Update': '1', 'X-UPS-Update-Key': KEY}


@pytest.fixture
def client():
    class Host:
        calls = []
        error = None
        calibration = None
        def request(self, *args):
            self.calls.append(args)
            if self.error:
                raise UpdateError(self.error)
            return unavailable()
    host = Host()
    app = FastAPI()
    install_update_routes(app, lambda: {'collector': {'build': {'version': '0.8.0'}}}, host,
                          calibration_status=lambda: host.calibration)
    with TestClient(app) as client:
        client.host_updater = host
        yield client


def test_read_only_status_never_sends_key_and_handles_uninstalled_service(client):
    response = client.get('/api/collector-update')
    assert response.status_code == 200
    assert response.json()['installed'] is False
    assert response.json()['current'] is None
    assert response.json()['runtime']['version'] == '0.8.0'
    assert client.host_updater.calls == [()]


@pytest.mark.parametrize('headers', [{}, {'X-UPS-Update': '1'}, {'X-UPS-Update-Key': KEY},
    dict(HEADERS, Origin='https://evil.invalid'), dict(HEADERS, **{'Sec-Fetch-Site': 'cross-site'}),
    dict(HEADERS, **{'Sec-Fetch-Site': 'same-site'}), dict(HEADERS, Origin='http://testserver@evil.invalid'),
    dict(HEADERS, Origin='null'), dict(HEADERS, Origin='http://testserver/path'),
    dict(HEADERS, **{'X-UPS-Update-Key': 'bad-key'})])
def test_missing_auth_or_foreign_browser_requests_never_reach_host(client, headers):
    response = client.post('/api/collector-update/check', json={}, headers=headers)
    assert response.status_code in (401, 403)
    assert not client.host_updater.calls
    assert KEY not in response.text


@pytest.mark.parametrize('action,payload', [('check', {'command': 'anything'}),
    ('check', []), ('install', {'version': '0.9.0'}), ('rollback', {'version': '0.8.0'}),
    ('install', {'version': '0.9.0', 'release_id': 1, 'sha256': 'b' * 64, 'url': 'https://evil.invalid'})])
def test_exact_action_schemas_reject_extra_or_missing_fields(client, action, payload):
    response = client.post('/api/collector-update/' + action, json=payload, headers=HEADERS)
    assert response.status_code == 400
    assert not client.host_updater.calls


def test_body_size_is_bounded_before_dispatch(client):
    response = client.post('/api/collector-update/check', content=' ' * 1025,
                           headers=dict(HEADERS, **{'Content-Type': 'application/json'}))
    assert response.status_code == 413 and not client.host_updater.calls


@pytest.mark.parametrize('number', ['NaN', 'Infinity', '-Infinity'])
def test_non_json_numbers_cannot_crash_the_real_ipc_encoder(client, number):
    body = '{"version":"0.9.0","release_id":' + number + ',"sha256":"' + 'b' * 64 + '"}'
    response = client.post('/api/collector-update/install', content=body,
                           headers=dict(HEADERS, **{'Content-Type': 'application/json'}))
    assert response.status_code == 400 and not client.host_updater.calls


@pytest.mark.parametrize('origin', ['http://testserver', 'https://testserver', 'http://testserver/'])
def test_proxy_tls_and_same_host_are_supported_without_persisting_key(client, origin):
    response = client.post('/api/collector-update/check', json={}, headers=dict(HEADERS, Origin=origin))
    assert response.status_code == 202
    assert client.host_updater.calls == [('check', {}, KEY)]
    assert KEY not in response.text


@pytest.mark.parametrize('code,status', [('unauthorized', 401), ('busy', 409), ('rate_limited', 429),
    ('service_unavailable', 503), ('stale_release', 409), ('collector_unavailable', 409)])
def test_host_failures_are_fixed_messages_and_no_mutation_retries(client, code, status):
    client.host_updater.error = code
    response = client.post('/api/collector-update/check', json={}, headers=HEADERS)
    assert response.status_code == status
    assert response.json()['detail']['code'] == code
    assert len(client.host_updater.calls) == 1
    assert KEY not in response.text


@pytest.mark.parametrize('action,payload', [('install', {'version': '0.12.1', 'release_id': 1, 'sha256': 'b' * 64}),
                                         ('rollback', {'version': '0.9.0', 'current_version': '0.12.1'})])
@pytest.mark.parametrize('readiness,code', [
    ({'target': 'mismatched', 'code': 'snapshot_stale'}, 'calibration_target_mismatch'),
    ({'target': 'unavailable'}, 'calibration_unreadable'),
    ({'target': 'matched', 'code': 'file_unreadable'}, 'calibration_unreadable'),
    ({'target': 'unverified', 'code': 'path_unconfigured'}, 'calibration_unconfigured'),
    ({'target': 'matched', 'code': 'unsupported_schema'}, 'calibration_incompatible'),
])
def test_panel_wiring_preflight_blocks_host_changes_with_fixed_public_reasons(client, action, payload, readiness, code):
    client.host_updater.calibration = {'readiness': dict(readiness, message='private /data/secret path')}
    status = client.get('/api/collector-update')
    assert status.json()['preflight'] == {'ready': False, 'code': code, 'target_verified': False}
    response = client.post('/api/collector-update/' + action, json=payload, headers=HEADERS)
    assert response.status_code == 409 and response.json()['detail']['code'] == code
    assert client.host_updater.calls == [()]
    assert 'private' not in response.text and 'secret' not in response.text


@pytest.mark.parametrize('readiness', [
    {'target': 'unverified', 'code': 'target_unverified', 'ready': False},
    {'target': 'unverified', 'code': 'snapshot_stale', 'ready': False},
    {'target': 'unverified', 'code': 'unsupported_profile', 'ready': False},
])
def test_legacy_unknown_target_or_stale_data_does_not_prevent_host_upgrade_checks(client, readiness):
    client.host_updater.calibration = {'readiness': readiness}
    payload = {'version': '0.12.1', 'release_id': 1, 'sha256': 'b' * 64}
    response = client.post('/api/collector-update/install', json=payload, headers=HEADERS)
    assert response.status_code == 202
    assert client.host_updater.calls == [('install', payload, KEY)]


def test_panel_wiring_failure_keeps_release_check_available(client):
    client.host_updater.calibration = {'readiness': {'target': 'mismatched'}}
    response = client.post('/api/collector-update/check', json={}, headers=HEADERS)
    assert response.status_code == 202
    assert client.host_updater.calls == [('check', {}, KEY)]


@pytest.mark.parametrize('code,expected', [('path_unconfigured', 'calibration_unconfigured'),
    ('file_unreadable', 'calibration_unreadable'), ('unsupported_schema', 'calibration_incompatible')])
def test_secondary_readiness_failure_is_not_hidden_by_unknown_profile(client, code, expected):
    client.host_updater.calibration = {'readiness': {'target': 'unverified', 'code': 'unsupported_profile',
        'issues': [{'code': 'unsupported_profile'}, {'code': code}]}}
    response = client.post('/api/collector-update/install', json={
        'version': '0.12.1', 'release_id': 1, 'sha256': 'b' * 64}, headers=HEADERS)
    assert response.status_code == 409 and response.json()['detail']['code'] == expected
    assert not client.host_updater.calls
