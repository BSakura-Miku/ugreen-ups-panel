"""Version negotiation, partial calibration and history provenance across API/collector boundaries."""
import csv
import io
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
V2 = {'X-UPS-Calibration-Version': '2'}
WRITE = {**V2, 'X-UPS-Calibration': '1', 'Origin': 'http://testserver'}


def publish(path, config, supported=(1, 2), *, timestamp=None):
    timestamp = time.time() if timestamp is None else timestamp
    sample = parse_frame(FRAME, timestamp)
    sample['input_voltage'] = sample['adapter_input_voltage_v'] = config.get('ac_voltage_nominal_v', 19)
    sample = PowerEstimator(config=config).update(sample)
    value = {'schema': 1, 'source': 'usbmon', 'heartbeat': timestamp, 'sample': sample,
             'calibration': {'config': config, 'configurable': True, 'error': None}}
    if supported is not None:
        value['calibration']['supported_config_schemas'] = list(supported)
    atomic_json(path, value)
    return value


@pytest.fixture
def panel(tmp_path):
    snapshot = tmp_path / 'latest.json'
    config_path = tmp_path / 'calibration.json'
    config = default_config()
    publish(snapshot, config)
    save_config(config_path, config)
    with TestClient(create_app(snapshot=snapshot, database=tmp_path / 'history.sqlite', static=tmp_path)) as client:
        yield client, snapshot, config_path


def request_for(client, voltage=12):
    status = client.get('/api/calibration', headers=V2).json()
    return {'profile': 'custom', 'ac_voltage_nominal_v': voltage,
            'coefficients': {'base_gain': 1.2345678901234, 'charge_gain': None, 'battery_gain': None},
            'expected_revision': status['desired']['revision']}


@pytest.mark.parametrize('voltage', [12, 19, 20])
def test_partial_configuration_is_saved_then_confirmed_by_matching_fresh_collector(panel, voltage):
    client, snapshot, path = panel
    before = client.get('/api/calibration', headers=V2).json()
    assert before['supported_config_schemas'] == [1, 2]
    response = client.put('/api/calibration', json=request_for(client, voltage), headers=WRITE)
    assert response.status_code == 200
    saved = response.json()
    assert saved['desired']['schema'] == 2 and saved['pending']
    assert saved['desired']['ac_voltage_nominal_v'] == voltage
    assert saved['desired']['coefficients'] == {'base_gain': 1.2345678901234, 'charge_gain': None, 'battery_gain': None}
    assert json.loads(path.read_text()) == saved['desired']
    publish(snapshot, saved['desired'])
    live = client.get('/api/live').json()
    assert live['fresh'] and live['sample']['calibration_schema'] == 2
    assert live['sample']['ac_voltage_nominal_v'] == voltage
    confirmed = client.get('/api/calibration', headers=V2).json()
    assert confirmed['collector_ready'] and not confirmed['pending']
    assert confirmed['active'] == confirmed['desired'] == saved['desired']


def test_legacy_client_cannot_read_or_overwrite_v2_even_while_application_is_pending(panel):
    client, snapshot, path = panel
    request = request_for(client)
    assert client.put('/api/calibration', json=request, headers=WRITE).status_code == 200
    config = json.loads(path.read_text())
    baseline = path.read_bytes()
    for apply in (False, True):
        if apply:
            publish(snapshot, config)
        old_get = client.get('/api/calibration')
        assert old_get.status_code == 409 and '刷新' in old_get.json()['detail']
        for profile in ('custom', 'none', 'local-19v-v1'):
            old_request = {'profile': profile, 'expected_revision': config['revision'],
                           'coefficients': {'base_gain': 2, 'charge_gain': 2, 'battery_gain': 2} if profile == 'custom' else None}
            response = client.put('/api/calibration', json=old_request,
                                  headers={'X-UPS-Calibration': '1', 'Origin': 'http://testserver'})
            assert response.status_code == 409 and path.read_bytes() == baseline


@pytest.mark.parametrize('supported', [None, (1,), (True, 2), (1, 3), ()])
def test_absent_old_or_malformed_capability_cannot_authorize_v2_save(panel, supported):
    client, snapshot, path = panel
    publish(snapshot, default_config(), supported)
    baseline = path.read_bytes()
    status = client.get('/api/calibration', headers=V2).json()
    assert status['collector_ready'] and status['supported_config_schemas'] == [1]
    response = client.put('/api/calibration', json=request_for(client), headers=WRITE)
    assert response.status_code == 503 and path.read_bytes() == baseline
    assert client.get('/api/live').json()['fresh']


def test_old_collector_can_still_save_explicit_legacy_coefficients_without_forced_migration(panel):
    client, snapshot, path = panel
    publish(snapshot, default_config(), None)
    config = {'schema': 1, 'profile': 'custom', 'coefficients': {
        'base_gain': 1.2345678901234, 'charge_gain': 1.2843154306288043, 'battery_gain': 1.2091130139203523}}
    request = {key: value for key, value in config.items() if key != 'schema'}
    request['expected_revision'] = client.get('/api/calibration', headers=V2).json()['desired']['revision']
    response = client.put('/api/calibration', json=request, headers=WRITE)
    assert response.status_code == 200
    assert response.json()['desired'] == normalize_config(config)
    assert json.loads(path.read_text())['schema'] == 1


def test_v2_editor_cannot_drop_voltage_silently_but_can_explicitly_disable(panel):
    client, snapshot, path = panel
    saved = client.put('/api/calibration', json=request_for(client), headers=WRITE).json()['desired']
    publish(snapshot, saved)
    baseline = path.read_bytes()
    downgrade = {'profile': 'custom', 'coefficients': {'base_gain': 1, 'charge_gain': 1, 'battery_gain': 1},
                 'expected_revision': saved['revision']}
    assert client.put('/api/calibration', json=downgrade, headers=WRITE).status_code == 409
    assert path.read_bytes() == baseline
    disabled = client.put('/api/calibration', json={'profile': 'none', 'coefficients': None,
                          'expected_revision': saved['revision']}, headers=WRITE)
    assert disabled.status_code == 200 and disabled.json()['desired'] == default_config()
    # The old UI stays protected until the collector also leaves schema 2.
    assert client.get('/api/calibration').status_code == 409
    publish(snapshot, default_config())
    assert client.get('/api/calibration').status_code == 200


def test_versioned_saves_keep_concurrency_and_staleness_guards(panel):
    client, snapshot, path = panel
    request = request_for(client)
    saved = client.put('/api/calibration', json=request, headers=WRITE).json()['desired']
    baseline = path.read_bytes()
    request['ac_voltage_nominal_v'] = 19
    assert client.put('/api/calibration', json=request, headers=WRITE).status_code == 409
    assert path.read_bytes() == baseline
    publish(snapshot, saved, timestamp=time.time() - 60)
    request['expected_revision'] = saved['revision']
    assert client.put('/api/calibration', json=request, headers=WRITE).status_code == 503
    assert path.read_bytes() == baseline


@pytest.mark.parametrize('change', ['voltage', 'missing_voltage', 'schema', 'missing_coefficients'])
def test_snapshot_does_not_accept_v2_metadata_without_matching_config_revision(panel, change):
    client, snapshot, _ = panel
    config = normalize_config({'schema': 2, 'profile': 'custom', 'ac_voltage_nominal_v': 12,
                              'coefficients': {'base_gain': 1.2, 'charge_gain': None, 'battery_gain': None}})
    value = publish(snapshot, config)
    assert load_snapshot(snapshot)['fresh']
    if change == 'voltage':
        value['sample']['ac_voltage_nominal_v'] = 19
    elif change == 'missing_voltage':
        value['sample'].pop('ac_voltage_nominal_v')
    elif change == 'schema':
        value['sample']['calibration_schema'] = True
    else:
        value['sample'].pop('calibration_coefficients')
    atomic_json(snapshot, value)
    result = load_snapshot(snapshot)
    assert result['fresh'] and result['sample']['soc'] == value['sample']['soc']
    assert not result['calibration_validation']['valid']
    assert result['sample']['ac_input_estimate_w'] is None


def test_voltage_revision_context_survives_history_and_csv_without_rewriting_legacy(tmp_path):
    path, snapshot = tmp_path / 'history.sqlite', tmp_path / 'latest.json'
    store = Store(path)
    now = time.time()
    legacy = normalize_config({'schema': 1, 'profile': 'custom', 'coefficients':
                               {'base_gain': 1.2, 'charge_gain': 1.3, 'battery_gain': 1.4}})
    configs = [legacy] + [normalize_config({'schema': 2, 'profile': 'custom', 'ac_voltage_nominal_v': voltage,
               'coefficients': {'base_gain': 1.2, 'charge_gain': None, 'battery_gain': None}}) for voltage in (12, 19, 20)]
    for i, config in enumerate(configs):
        value = publish(snapshot, config, timestamp=now - 8 + i * 2)
        store.ingest({'fresh': True, 'sample': value['sample']}, now=now)
    store.flush()
    history = store.history(1, now=now)
    assert len(history['points']) == 4
    contexts = [point['context'] for point in history['points']]
    assert len({context['calibration_revision'] for context in contexts}) == 4
    assert {c['ac_voltage_nominal_v'] for c in contexts if c.get('calibration_schema') == 2} == {12, 19, 20}
    old = next(c for c in contexts if c['calibration_revision'] == legacy['revision'])
    assert 'ac_voltage_nominal_v' not in old and 'calibration_schema' not in old
    with TestClient(create_app(snapshot=snapshot, database=path, static=tmp_path)) as client:
        deadline = time.monotonic() + 2
        while True:
            response = client.get('/api/export.csv?hours=1')
            if response.status_code != 503 or time.monotonic() >= deadline:
                break
            time.sleep(.01)
        assert response.status_code == 200
        rows = list(csv.DictReader(io.StringIO(response.text.lstrip('\ufeff'))))
        assert {row['ac_voltage_nominal_v'] for row in rows} == {'', '12', '19', '20'}
        assert any(json.loads(row['calibration_coefficients'])['charge_gain'] is None for row in rows)
        assert client.get('/api/history?hours=8760').status_code == 200
