import json
from pathlib import Path
import time

from fastapi.testclient import TestClient
import pytest

from ups_panel.app import create_app, load_snapshot
from ups_panel.calibration import default_config, normalize_config, save_config
from ups_panel.collector import atomic_json
from ups_panel.power import PowerEstimator
from ups_panel.protocol import parse_frame
from ups_panel.storage import Store

FRAME = bytes.fromhex((Path(__file__).parents[1] / 'fixtures/online.hex').read_text().splitlines()[0])
HEADERS = {'X-UPS-Calibration': '1', 'Origin': 'http://testserver'}


def publish(path, config, *, capable=True, timestamp=None):
    timestamp = time.time() if timestamp is None else timestamp
    sample = PowerEstimator(config=config).update(parse_frame(FRAME, timestamp))
    snapshot = {'schema': 1, 'source': 'usbmon', 'heartbeat': timestamp, 'sample': sample}
    if capable:
        snapshot['calibration'] = {'config': config, 'configurable': True, 'error': None}
    atomic_json(path, snapshot)
    return snapshot


@pytest.fixture
def panel(tmp_path):
    path = tmp_path / 'latest.json'
    publish(path, default_config('local-19v-v1'))
    with TestClient(create_app(snapshot=path, database=tmp_path / 'history.sqlite', static=tmp_path)) as client:
        yield client, path, tmp_path / 'calibration.json'


def request_for(client, profile='custom', coefficients=None):
    return {'profile': profile,
            'coefficients': coefficients if coefficients is not None else
                {'base_gain': 1.5, 'charge_gain': 0, 'battery_gain': 1.1} if profile == 'custom' else None,
            'expected_revision': client.get('/api/calibration').json()['desired']['revision']}


def test_shows_exact_active_coefficients_and_confirms_collector_application(panel):
    client, snapshot, path = panel
    before = client.get('/api/calibration').json()
    assert before['active'] == default_config('local-19v-v1')
    assert before['collector_ready'] and not before['pending']
    response = client.put('/api/calibration', json=request_for(client), headers=HEADERS)
    assert response.status_code == 200
    saved = response.json()
    assert saved['pending'] and saved['active'] == before['active']
    assert saved['desired']['coefficients']['charge_gain'] == 0
    assert json.loads(path.read_text()) == saved['desired']
    publish(snapshot, saved['desired'])
    after = client.get('/api/calibration').json()
    assert not after['pending'] and after['active'] == saved['desired']
    assert path.stat().st_mode & 0o777 == 0o640


def test_preset_and_disabled_choices_have_canonical_coefficients(panel):
    client, snapshot, _ = panel
    for profile in ('none', 'local-19v-v1'):
        response = client.put('/api/calibration', json=request_for(client, profile), headers=HEADERS)
        assert response.status_code == 200
        desired = response.json()['desired']
        assert desired == default_config(profile)
        publish(snapshot, desired)


def test_older_or_disconnected_collector_cannot_accept_a_misleading_save(panel):
    client, snapshot, path = panel
    for kwargs in ({'capable': False}, {'timestamp': time.time() - 60}):
        publish(snapshot, default_config('local-19v-v1'), **kwargs)
        status = client.get('/api/calibration').json()
        assert status['active']['coefficients']['base_gain'] == 1.182379
        assert not status['collector_ready']
        assert client.put('/api/calibration', json=request_for(client), headers=HEADERS).status_code == 503
        assert not path.exists()


def test_two_editors_cannot_silently_overwrite_each_other(panel):
    client, _, path = panel
    request = request_for(client)
    assert client.put('/api/calibration', json=request, headers=HEADERS).status_code == 200
    saved = path.read_bytes()
    request['coefficients']['base_gain'] = 2
    assert client.put('/api/calibration', json=request, headers=HEADERS).status_code == 409
    assert path.read_bytes() == saved


@pytest.mark.parametrize('headers', [
    {}, {'X-UPS-Calibration': '1', 'Origin': 'https://elsewhere.example'},
    {'X-UPS-Calibration': '1', 'Origin': 'null'},
    {'X-UPS-Calibration': '1', 'Origin': 'http://['},
    {'X-UPS-Calibration': '1', 'Sec-Fetch-Site': 'cross-site'},
])
def test_cross_site_writes_are_rejected(panel, headers):
    client, _, path = panel
    assert client.put('/api/calibration', json=request_for(client), headers=headers).status_code == 403
    assert not path.exists()


def test_https_proxy_preserving_host_can_save_without_trusting_forwarded_headers(panel):
    client, _, _ = panel
    headers = {'X-UPS-Calibration': '1', 'Origin': 'https://ups.example',
               'Host': 'ups.example', 'Sec-Fetch-Site': 'same-origin'}
    assert client.put('/api/calibration', json=request_for(client), headers=headers).status_code == 200


def test_forwarded_headers_cannot_authorize_another_origin(panel):
    client, _, path = panel
    headers = {'X-UPS-Calibration': '1', 'Origin': 'https://elsewhere.example',
               'Host': 'testserver', 'X-Forwarded-Host': 'elsewhere.example', 'X-Forwarded-Proto': 'https'}
    assert client.put('/api/calibration', json=request_for(client), headers=headers).status_code == 403
    assert not path.exists()


@pytest.mark.parametrize('coefficients', [
    {'base_gain': True, 'charge_gain': 1, 'battery_gain': 1},
    {'base_gain': 0, 'charge_gain': 1, 'battery_gain': 1},
    {'base_gain': 1, 'charge_gain': -1, 'battery_gain': 1},
    {'base_gain': 1, 'charge_gain': 1, 'battery_gain': 11},
    {'base_gain': '1', 'charge_gain': 1, 'battery_gain': 1},
    {'base_gain': 1, 'charge_gain': 1},
])
def test_invalid_coefficients_never_replace_configuration(panel, coefficients):
    client, _, path = panel
    response = client.put('/api/calibration', json=request_for(client, coefficients=coefficients), headers=HEADERS)
    assert response.status_code == 400 and not path.exists()


def test_invalid_configuration_can_be_repaired_but_symlink_is_not_followed(panel, tmp_path):
    client, _, path = panel
    path.write_text('{invalid')
    assert client.get('/api/calibration').json()['error']
    assert client.put('/api/calibration', json=request_for(client), headers=HEADERS).status_code == 200
    target = tmp_path / 'leave-intact'
    target.write_text('keep')
    path.unlink()
    path.symlink_to(target)
    assert client.put('/api/calibration', json=request_for(client), headers=HEADERS).status_code == 503
    assert target.read_text() == 'keep' and path.is_symlink()


def test_nonfinite_and_oversized_requests_are_rejected(panel):
    client, _, path = panel
    body = json.dumps(request_for(client)).replace('1.5', 'NaN')
    headers = {**HEADERS, 'Content-Type': 'application/json'}
    assert client.put('/api/calibration', content=body, headers=headers).status_code == 400
    assert client.put('/api/calibration', content=' ' * 4097, headers=headers).status_code == 413
    assert not path.exists()


def test_snapshot_rejects_coefficients_that_do_not_match_revision(tmp_path):
    path = tmp_path / 'latest.json'
    snapshot = publish(path, default_config('local-19v-v1'))
    assert load_snapshot(path)['fresh']
    snapshot['sample']['calibration_coefficients']['base_gain'] = 2
    atomic_json(path, snapshot)
    assert not load_snapshot(path)['fresh']


def test_collector_config_without_matching_new_sample_is_not_confirmed(panel):
    client, path, _ = panel
    snapshot = publish(path, default_config('local-19v-v1'))
    snapshot['calibration']['config'] = default_config('none')
    atomic_json(path, snapshot)
    assert not client.get('/api/calibration').json()['collector_ready']


def test_history_preserves_coefficients_and_separates_same_bucket_changes(tmp_path):
    store = Store(tmp_path / 'history.sqlite')
    for ts, gain in ((100, 1.2), (104, 1.5)):
        config = normalize_config({'schema': 1, 'profile': 'custom', 'coefficients':
                                  {'base_gain': gain, 'charge_gain': 1, 'battery_gain': 1}})
        sample = PowerEstimator(config=config).update(parse_frame(FRAME, ts))
        store.ingest({'fresh': True, 'sample': sample}, now=ts)
    store.flush()
    points = store.history(1, now=110)['points']
    assert len(points) == 2
    assert {p['context']['calibration_coefficients']['base_gain'] for p in points} == {1.2, 1.5}
    assert len({p['context']['calibration_revision'] for p in points}) == 2


def test_configuration_survives_application_restart(panel, tmp_path):
    client, snapshot, path = panel
    assert client.put('/api/calibration', json=request_for(client), headers=HEADERS).status_code == 200
    saved = json.loads(path.read_text())
    with TestClient(create_app(snapshot=snapshot, database=tmp_path / 'second.sqlite', static=tmp_path)) as restarted:
        assert restarted.get('/api/calibration').json()['desired'] == saved
