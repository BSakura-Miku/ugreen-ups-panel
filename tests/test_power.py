from pathlib import Path
import pytest
from ups_panel.power import PowerEstimator, BASE_GAIN, CHARGE_GAIN
from ups_panel.protocol import parse_frame
from ups_panel.storage import Store

ROOT = Path(__file__).parents[1]
def sample(fixture, ts):
    return parse_frame(bytes.fromhex((ROOT / 'fixtures' / fixture).read_text().splitlines()[0]), ts)


def test_warmup_smoothing_and_charge_compensation():
    e = PowerEstimator('local-19v-v1')
    for t in (100, 102, 104):
        assert e.update(sample('charging-local.hex', t))['ac_input_estimate_w'] is None
    s = e.update(sample('charging-local.hex', 106))
    expected = s['raw_fields']['byte_28'] * BASE_GAIN + s['battery_charge_power_candidate_w'] * CHARGE_GAIN
    assert s['ac_input_estimate_w'] == pytest.approx(expected, abs=.01)
    assert s['ac_estimate_model'] == 'us3000_19v_v1'
    assert not s['power_verified']


def test_mode_change_outage_reconnect_and_unknown_do_not_reuse_estimate():
    e = PowerEstimator('local-19v-v1')
    for t in (100, 102, 104, 106): e.update(sample('online.hex',t))
    assert e.update(sample('battery-local.hex',108))['ac_input_estimate_w'] is None
    assert e.update(sample('charging-local.hex',110))['ac_estimate_quality'] == 'warming_up'
    for t in (112,114,116): e.update(sample('charging-local.hex',t))
    assert e.update(sample('charging-local.hex',130))['ac_input_estimate_w'] is None
    s=sample('online.hex',132);s['mode']='unknown'
    assert e.update(s)['ac_input_estimate_w'] is None


def test_unsupported_adapter_and_extrapolation():
    e=PowerEstimator('local-19v-v1');s=sample('online.hex',100);s['input_voltage']=12
    assert e.update(s)['ac_estimate_quality']=='unsupported_voltage'
    for t in (102,104,106,108):
        s=sample('online.hex',t);s['raw_fields']['byte_28']=100;e.update(s)
    assert s['ac_estimate_quality']=='extrapolated'
    assert s['ac_input_estimate_w']==pytest.approx(118.24)


def test_estimate_history_separate_from_legacy(tmp_path):
    store=Store(tmp_path/'h.db');e=PowerEstimator('local-19v-v1')
    for t in (100,102,104,106,108):
        s=e.update(sample('charging-local.hex',t));store.ingest({'fresh':True,'sample':s},now=t)
    store.flush();v=store.history(1,now=110)['points'][-1]['values']
    assert 'ac_input_estimate_w' in v and 'battery_charge_power_candidate_w' in v
    assert 'power_w' not in v


def test_nominal_capacity_estimate_preserves_raw_values_and_resets():
    from ups_panel.power import BATTERY_NOMINAL_GAIN
    e=PowerEstimator('local-19v-v1')
    for t in (100,102,104,106):
        s=e.update(sample('battery-local.hex',t))
    raw=s['battery_discharge_power_candidate_w']
    assert s['battery_energy_estimate_w']==pytest.approx(raw*BATTERY_NOMINAL_GAIN,abs=.01)
    assert s['ac_input_estimate_w'] is None
    assert not s['battery_current_verified']
    assert s['battery_estimate_quality']=='nominal_capacity_assumption'
    assert e.update(sample('online.hex',108))['battery_energy_estimate_w'] is None
    assert e.update(sample('battery-local.hex',110))['battery_energy_estimate_w'] is None


def test_default_profile_never_applies_another_installations_coefficients():
    e = PowerEstimator()
    for mode_fixture in ('online.hex', 'charging-local.hex', 'battery-local.hex'):
        for t in range(100, 110, 2):
            original = sample(mode_fixture, t)
            raw = original['dc_power_estimate_w']
            s = e.update(original)
            assert s['dc_power_estimate_w'] == raw
            assert s['ac_input_estimate_w'] is None
            assert s['battery_energy_estimate_w'] is None
            assert s['ac_estimate_quality'] == 'not_configured'
            assert s['battery_estimate_quality'] == 'not_configured'
            assert s['calibration_profile'] == 'none'
            assert s['ac_estimate_model'] is None
    with pytest.raises(ValueError, match='Unknown calibration'):
        PowerEstimator('typo')


def test_duplicate_snapshots_do_not_fill_or_reset_smoothing_window():
    e = PowerEstimator('local-19v-v1')
    for _ in range(10):
        assert e.update(sample('online.hex', 100))['ac_input_estimate_w'] is None
    for t in (102, 104, 106):
        s = e.update(sample('online.hex', t))
    estimate = s['ac_input_estimate_w']
    assert estimate is not None
    assert e.update(sample('online.hex', 106))['ac_input_estimate_w'] == estimate
    assert len(e.window) == 4
    assert e.update(sample('online.hex', 99))['ac_input_estimate_w'] is None


@pytest.mark.parametrize('field,value', [('byte_28', float('nan')), ('byte_28', True),
                                       ('charge', float('inf')), ('timestamp', float('nan'))])
def test_invalid_power_inputs_never_publish_nonfinite_estimates(field, value):
    e = PowerEstimator('local-19v-v1')
    for t in (100, 102, 104, 106):
        e.update(sample('charging-local.hex', t))
    s = sample('charging-local.hex', 108)
    if field == 'byte_28': s['raw_fields']['byte_28'] = value
    elif field == 'charge': s['battery_charge_power_candidate_w'] = value
    else: s['timestamp'] = value
    assert e.update(s)['ac_input_estimate_w'] is None
    assert s['ac_estimate_quality'] == 'invalid_data'
