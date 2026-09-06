import copy
import json
from pathlib import Path
import stat

import pytest

from ups_panel.collector import CalibrationState
from ups_panel.calibration import (CalibrationError, DEFAULT_COEFFICIENTS, default_config,
                                   load_config, normalize_config, save_config)
from ups_panel.power import ESTIMATE_FIELDS, PowerEstimator
from ups_panel.protocol import parse_frame


ROOT = Path(__file__).parents[1]


def custom(voltage=12, base=2, charge=None, battery=None):
    return {'schema': 2, 'profile': 'custom', 'ac_voltage_nominal_v': voltage,
            'coefficients': {'base_gain': base, 'charge_gain': charge, 'battery_gain': battery}}


def sample(timestamp, mode='online', voltage=12):
    fixture = {'online': 'online.hex', 'charging': 'charging-local.hex', 'battery': 'battery-local.hex'}[mode]
    frame = bytearray.fromhex((ROOT / 'fixtures' / fixture).read_text().splitlines()[0])
    frame[16:18] = round(voltage * 1000).to_bytes(2, 'big')
    return parse_frame(bytes(frame), timestamp)


@pytest.mark.parametrize('config,revision', [
    ({'schema': 1, 'profile': 'none'},
     '1412d39ca649d51ca99abad1ca37cb1446211b5db05f3fd953170fc67da1f091'),
    ({'schema': 1, 'profile': 'local-19v-v1'},
     'c618a726a10241a8062d0a00b421551030137682d0fa468371d76c0dbe5109eb'),
    ({'schema': 1, 'profile': 'custom', 'coefficients': {'base_gain': 1, 'charge_gain': 0, 'battery_gain': 2}},
     '7733b2f7bb29caf04bbf3c97355e256bcae3b9e69695145e69f2bac0ca4d2b11'),
    ({'schema': 1, 'profile': 'custom', 'coefficients': {
        'base_gain': 1.1234567890123457, 'charge_gain': 1.2843154306288043,
        'battery_gain': 1.2091130139203523}},
     '02afe301931610b36f94f67adb9060caf9256f61ce98181bc9f351fc15541c36'),
])
def test_schema_one_revisions_and_full_precision_are_unchanged(config, revision):
    normalized = normalize_config(config)
    assert normalized['revision'] == revision
    assert set(normalized) == {'schema', 'profile', 'coefficients', 'revision'}
    if config.get('coefficients'):
        assert normalized['coefficients'] == config['coefficients']
    assert normalize_config(normalized) == normalized


def test_voltage_and_nullable_coefficients_are_canonical_and_part_of_revision():
    integer = normalize_config(custom(charge=0, battery=1.1234567890123457))
    floating = custom(voltage=12.0, base=2.0, charge=-0.0, battery=1.1234567890123457)
    floating['revision'] = 'caller-supplied'
    assert normalize_config(floating) == integer
    assert type(integer['ac_voltage_nominal_v']) is int
    assert integer['coefficients']['charge_gain'] == 0.0
    assert integer['coefficients']['battery_gain'] == 1.1234567890123457
    assert normalize_config(integer) == integer
    partial = normalize_config(custom())
    assert partial['coefficients'] == {'base_gain': 2.0, 'charge_gain': None, 'battery_gain': None}
    assert len({normalize_config(custom(voltage))['revision'] for voltage in (12, 19, 20)}) == 3
    assert partial['revision'] != normalize_config(custom(charge=0))['revision']
    assert partial['revision'] != normalize_config(custom(battery=1))['revision']


@pytest.mark.parametrize('voltage', [True, False, None, '12', 0, 18, 12.01, float('nan'),
                                   float('inf'), 10 ** 400, [], {}])
def test_schema_two_rejects_invalid_nominal_voltage(voltage):
    with pytest.raises(CalibrationError):
        normalize_config(custom(voltage))


def test_schema_two_rejects_unknown_keys_presets_and_missing_coefficient_keys():
    invalid = [custom(), custom(), custom(), custom(), custom(), custom(), custom()]
    invalid[0]['extra'] = 1
    invalid[1]['profile'] = 'local-19v-v1'
    invalid[2]['profile'] = 'none'
    invalid[3]['coefficients']['extra'] = 1
    del invalid[4]['coefficients']['base_gain']
    del invalid[5]['coefficients']['charge_gain']
    del invalid[6]['ac_voltage_nominal_v']
    for config in invalid:
        with pytest.raises(CalibrationError):
            normalize_config(config)
    with pytest.raises(CalibrationError):
        normalize_config({**custom(), 'schema': 1})


@pytest.mark.parametrize('value', [True, False, '1', -1, 10.0001, float('nan'), float('inf'), 10 ** 400])
def test_schema_two_retains_numeric_limits_for_configured_coefficients(value):
    for name in DEFAULT_COEFFICIENTS:
        config = custom()
        config['coefficients'][name] = value
        with pytest.raises(CalibrationError):
            normalize_config(config)
    assert normalize_config(custom(base=10, charge=0, battery=10))['coefficients'] == {
        'base_gain': 10.0, 'charge_gain': 0.0, 'battery_gain': 10.0}


@pytest.mark.parametrize('base,battery', [(None, None), (0, None), (2, 0)])
def test_schema_two_requires_positive_base_and_positive_configured_battery_gain(base, battery):
    with pytest.raises(CalibrationError):
        normalize_config(custom(base=base, battery=battery))


@pytest.mark.parametrize('nominal', [12, 19, 20])
@pytest.mark.parametrize('mode', ['online', 'charging'])
def test_voltage_scope_accepts_each_inclusive_boundary_and_rejects_outside(nominal, mode):
    config = normalize_config(custom(nominal, charge=.5))
    for voltage in (nominal - 1, nominal, nominal + 1):
        estimator = PowerEstimator(config=config)
        for timestamp in (100, 102, 104, 106):
            raw = sample(timestamp, mode, voltage)
            preserved = copy.deepcopy(raw)
            result = estimator.update(raw)
            assert all(result[key] == value for key, value in preserved.items())
        charge = result['battery_charge_power_candidate_w'] if mode == 'charging' else 0
        assert result['ac_input_estimate_w'] == pytest.approx(
            result['raw_fields']['byte_28'] * 2 + charge * .5, abs=.01)
        assert result['ac_estimate_quality'] == 'custom_unverified'
        assert result['ac_estimate_model'] == 'us3000_custom_v2_' + config['revision']
        assert result['calibration_schema'] == 2 and result['ac_voltage_nominal_v'] == nominal
        assert result['calibration_verified'] is False
    for voltage in (nominal - 1.001, nominal + 1.001):
        result = estimator.update(sample(108, mode, voltage))
        assert result['ac_input_estimate_w'] is None
        assert result['ac_estimate_quality'] == 'unsupported_voltage'
        assert not estimator.window
        estimator = PowerEstimator(config=config)


@pytest.mark.parametrize('profile', ['local-19v-v1', 'custom'])
def test_schema_one_models_keep_legacy_voltage_scope_and_model_identity(profile):
    config = (default_config(profile) if profile != 'custom' else normalize_config(
        {'schema': 1, 'profile': 'custom', 'coefficients': DEFAULT_COEFFICIENTS}))
    estimator = PowerEstimator(config=config)
    for timestamp in (100, 102, 104, 106):
        result = estimator.update(sample(timestamp, voltage=19))
    expected_model = ('us3000_19v_v1' if profile != 'custom' else 'us3000_19v_custom_' + config['revision'])
    assert result['ac_estimate_model'] == expected_model
    assert 'calibration_schema' not in result and 'ac_voltage_nominal_v' not in result
    for timestamp, voltage in ((108, 12), (110, 17.999), (112, 20.001)):
        result = estimator.update(sample(timestamp, voltage=voltage))
        assert result['ac_input_estimate_w'] is None and result['ac_estimate_quality'] == 'unsupported_voltage'


def test_first_step_only_estimates_online_and_never_borrows_other_gains():
    estimator = PowerEstimator(config=custom())
    for timestamp in (100, 102, 104, 106):
        online = estimator.update(sample(timestamp))
    assert online['ac_input_estimate_w'] is not None
    for timestamp, mode, quality in ((108, 'charging', 'charge_not_configured'),
                                     (110, 'battery', 'battery_not_configured')):
        raw = sample(timestamp, mode)
        preserved = copy.deepcopy(raw)
        result = estimator.update(raw)
        assert result['ac_input_estimate_w'] is None and result['battery_energy_estimate_w'] is None
        field = 'ac_estimate_quality' if mode == 'charging' else 'battery_estimate_quality'
        assert result[field] == quality and not estimator.window
        assert all(result[key] == value for key, value in preserved.items())
        assert result['calibration_coefficients']['charge_gain'] is None
        assert result['calibration_coefficients']['battery_gain'] is None
    for timestamp in (112, 114, 116):
        assert estimator.update(sample(timestamp))['ac_estimate_quality'] == 'warming_up'
    assert estimator.update(sample(118))['ac_input_estimate_w'] == online['ac_input_estimate_w']


def test_zero_charge_gain_and_configured_battery_gain_remain_distinct_from_null():
    estimator = PowerEstimator(config=custom(charge=0, battery=4))
    for timestamp in (100, 102, 104, 106):
        charging = estimator.update(sample(timestamp, 'charging'))
    assert charging['ac_estimate_quality'] == 'custom_unverified'
    assert charging['ac_input_estimate_w'] == pytest.approx(charging['raw_fields']['byte_28'] * 2)
    for timestamp in (108, 110, 112, 114):
        battery = estimator.update(sample(timestamp, 'battery'))
    assert battery['battery_estimate_quality'] == 'custom_unverified'
    assert battery['battery_energy_estimate_w'] == pytest.approx(
        battery['battery_discharge_power_candidate_w'] * 4, abs=.01)
    assert battery['ac_input_estimate_w'] is None


def test_duplicate_cache_preserves_optional_metadata_and_copies_coefficients():
    estimator = PowerEstimator(config=custom(19))
    for timestamp in (100, 102, 104, 106):
        result = estimator.update(sample(timestamp, voltage=19))
    expected = {key: copy.deepcopy(result[key]) for key in ESTIMATE_FIELDS if key in result}
    result['calibration_coefficients']['base_gain'] = 9
    repeated = estimator.update(sample(106, voltage=19))
    assert {key: repeated[key] for key in expected} == expected
    assert estimator.coefficients['base_gain'] == 2
    assert len(estimator.window) == 4

    # Switching back also removes optional metadata from a reused processed input.
    legacy = PowerEstimator('local-19v-v1')
    first = legacy.update(repeated)
    second = legacy.update(sample(106, voltage=19))
    assert 'calibration_schema' not in first and 'ac_voltage_nominal_v' not in first
    assert 'calibration_schema' not in second and 'ac_voltage_nominal_v' not in second


def test_schema_two_atomic_roundtrip_preserves_nulls_voltage_and_permissions(tmp_path):
    path = tmp_path / 'calibration.json'
    stored = save_config(path, custom(20.0, base=1.1234567890123457))
    assert load_config(path) == stored
    assert json.loads(path.read_text()) == stored
    assert stored['coefficients']['charge_gain'] is None
    assert stored['coefficients']['battery_gain'] is None
    assert type(stored['ac_voltage_nominal_v']) is int
    assert stat.S_IMODE(path.stat().st_mode) == 0o640


def test_collector_advertises_capability_and_hot_loads_both_schemas_without_mixing(tmp_path):
    path = tmp_path / 'calibration.json'
    first = save_config(path, {'schema': 1, 'profile': 'custom', 'coefficients': DEFAULT_COEFFICIENTS})
    state = CalibrationState(path)
    assert state.snapshot()['supported_config_schemas'] == [1, 2]
    assert state.refresh(now=99)
    for timestamp in (100, 102, 104, 106):
        previous = state.update(sample(timestamp, voltage=19))
    assert previous['ac_input_estimate_w'] is not None

    estimator = state.estimator
    path.write_text(json.dumps(custom(18)))
    assert not state.refresh(now=107)
    assert state.error and state.config == first and state.estimator is estimator
    assert state.update(sample(108, voltage=19))['calibration_revision'] == first['revision']

    second = save_config(path, custom(12))
    assert state.refresh(now=110)
    assert state.error is None and not state.estimator.window
    with pytest.raises(ValueError, match='predates'):
        state.update(sample(110))
    for timestamp in (112, 114, 116):
        assert state.update(sample(timestamp))['ac_estimate_quality'] == 'warming_up'
    current = state.update(sample(118))
    assert current['ac_input_estimate_w'] is not None
    assert current['calibration_revision'] == second['revision']
    assert current['calibration_schema'] == 2 and current['ac_voltage_nominal_v'] == 12
    assert state.snapshot()['config'] == second
    assert state.snapshot()['supported_config_schemas'] == [1, 2]
    assert state.changed_at is None

    estimator = state.estimator
    path.write_text('{broken')
    assert not state.refresh(now=119)
    assert state.config == second and state.estimator is estimator and state.error
    assert state.update(sample(120))['calibration_revision'] == second['revision']

    save_config(path, first)
    assert state.refresh(now=122)
    reverted = state.update(sample(124, voltage=19))
    assert reverted['calibration_revision'] == first['revision']
    assert 'calibration_schema' not in reverted and 'ac_voltage_nominal_v' not in reverted
    assert reverted['ac_estimate_quality'] == 'warming_up'
    assert state.snapshot()['supported_config_schemas'] == [1, 2]
