"""Bad calibration must neither erase USB evidence nor grant power provenance."""
import copy
import json
from pathlib import Path

import pytest

from ups_panel.calibration import default_config, normalize_config
from ups_panel.diagnostics import diagnostic_view
from ups_panel.power import ESTIMATE_FIELDS, PowerEstimator
from ups_panel.protocol import parse_frame
from ups_panel.storage import Store
from ups_panel import telemetry
from ups_panel.telemetry import MAX_SNAPSHOT_BYTES, load_snapshot


FRAME = bytes.fromhex((Path(__file__).parents[1] / 'fixtures/online.hex').read_text().splitlines()[0])


def custom(voltage=12, *, partial=False):
    return normalize_config({'schema': 2, 'profile': 'custom', 'ac_voltage_nominal_v': voltage,
                             'coefficients': {'base_gain': 1.2, 'charge_gain': None if partial else 1.3,
                                              'battery_gain': None if partial else 1.4}})


def snapshot(config=None, *, now=1000, mode='online'):
    config = default_config('local-19v-v1') if config is None else config
    frame = bytearray(FRAME)
    frame[7] = {'online': 0x26, 'charging': 0x36, 'battery': 0x21, 'unknown': 0x99}[mode]
    frame[16:18] = int(config.get('ac_voltage_nominal_v', 19) * 1000).to_bytes(2, 'big')
    frame[29:31] = (650).to_bytes(2, 'big')
    frame[31:33] = (1000).to_bytes(2, 'big')
    estimator = PowerEstimator(config=config)
    for ts in (now - 6, now - 4, now - 2, now):
        sample = estimator.update(parse_frame(frame, ts))
    return {'schema': 1, 'source': 'usbmon', 'heartbeat': now, 'sample': sample,
            'device': {'serial': 'TELEMETRY-TEST', 'vendor': '1234', 'product': '5678'},
            'collector': {'schema': 1, 'build': {'version': 'test-collector'},
                          'host': {'system': 'Linux', 'python_supported': True},
                          'usb': {'discovery': 'found', 'usbmon_readable': True}},
            'calibration': {'config': copy.deepcopy(config), 'configurable': True, 'error': None,
                            'supported_config_schemas': [1, 2]},
            'nut': {'available': True, 'timestamp': now, 'values': {'ups.status': 'OL'}}}


def read(path, value, *, now=None):
    path.write_text(json.dumps(value, allow_nan=False))
    return load_snapshot(path, now=value['heartbeat'] if now is None else now)


@pytest.mark.parametrize('config', [default_config(), default_config('local-19v-v1'), custom(),
                                   custom(partial=True), custom(19), custom(20),
                                   normalize_config({'schema': 1, 'profile': 'custom', 'coefficients':
                                                     {'base_gain': 1.2, 'charge_gain': 1.3, 'battery_gain': 1.4}})])
@pytest.mark.parametrize('mode', ['online', 'charging', 'battery', 'unknown'])
def test_supported_configurations_preserve_real_collector_output(tmp_path, config, mode):
    original = snapshot(config, mode=mode)
    result = read(tmp_path / 'latest.json', original)
    assert result['fresh']
    assert result['sample'] == original['sample']
    assert result['calibration_validation'] == {'valid': True, 'reason': None, 'profile': config['profile']}


def test_small_positive_battery_gain_may_round_display_to_zero(tmp_path):
    config = custom()
    config['coefficients']['battery_gain'] = 0.00001
    config = normalize_config(config)
    value = snapshot(config, mode='battery')
    assert value['sample']['battery_energy_estimate_w'] == 0.0
    result = read(tmp_path / 'latest.json', value)
    assert result['fresh'] and result['calibration_validation']['valid']
    assert result['sample']['battery_estimate_quality'] == 'custom_unverified'


@pytest.mark.parametrize('change,reason', [
    ('profile', 'unsupported_profile'), ('profile_without_coefficients', 'unsupported_profile'),
    ('revision', 'revision_mismatch'), ('coefficients', 'missing_metadata'),
    ('top_metadata', 'missing_metadata'), ('top_config', 'missing_metadata'),
    ('top_coefficients', 'missing_metadata'), ('top_revision', 'revision_mismatch'),
    ('top_profile', 'unsupported_profile'), ('active_mismatch', 'config_mismatch'),
    ('schema', 'invalid_config'), ('voltage', 'revision_mismatch'),
    ('ac_model', 'invalid_estimate'), ('battery_basis', 'invalid_estimate'),
    ('numeric_estimate', 'invalid_estimate'), ('verified', 'invalid_estimate'),
    ('quality', 'invalid_estimate'), ('unsafe_profile', 'invalid_config'),
])
def test_calibration_failure_preserves_raw_data_identity_and_nut(tmp_path, change, reason):
    value = snapshot(custom())
    sample = value['sample']
    if change in ('profile', 'profile_without_coefficients'):
        sample['calibration_profile'] = 'local-12v-v1'
        if change == 'profile_without_coefficients':
            sample.pop('calibration_coefficients')
    elif change == 'revision':
        sample['calibration_revision'] = 'bad-revision'
    elif change == 'coefficients':
        sample.pop('calibration_coefficients')
    elif change == 'top_metadata':
        value.pop('calibration')
    elif change == 'top_config':
        value['calibration'].pop('config')
    elif change == 'top_coefficients':
        value['calibration']['config'].pop('coefficients')
    elif change == 'top_revision':
        value['calibration']['config']['revision'] = 'bad-revision'
    elif change == 'top_profile':
        value['calibration']['config']['profile'] = 'local-12v-v1'
    elif change == 'active_mismatch':
        value['calibration']['config'] = custom(19)
    elif change == 'schema':
        sample['calibration_schema'] = True
    elif change == 'voltage':
        sample['ac_voltage_nominal_v'] = 19
    elif change == 'ac_model':
        sample['ac_estimate_model'] = 'different-model'
    elif change == 'battery_basis':
        sample['battery_estimate_basis'] = 'different-basis'
    elif change == 'numeric_estimate':
        sample['ac_input_estimate_w'] = '56.75'
    elif change == 'verified':
        sample['calibration_verified'] = True
    elif change == 'quality':
        sample['ac_estimate_quality'] = 'calibrated_range'
    elif change == 'unsafe_profile':
        sample['calibration_profile'] = '<script>\n' + 'x' * 128
    result = read(tmp_path / 'latest.json', value)
    assert result['fresh'] and result['source'] == 'usbmon'
    assert result['calibration_validation']['valid'] is False
    assert result['calibration_validation']['reason'] == reason
    assert result['collector'] == value['collector'] and result['nut'] == value['nut']
    assert result.get('calibration') == value.get('calibration')
    assert {key: item for key, item in result['sample'].items() if key not in ESTIMATE_FIELDS} == {
        key: item for key, item in sample.items() if key not in ESTIMATE_FIELDS}
    assert result['sample']['ac_input_estimate_w'] is None
    assert result['sample']['battery_energy_estimate_w'] is None
    assert result['sample']['ac_estimate_quality'] == result['sample']['battery_estimate_quality'] == 'invalid_calibration'
    assert 'calibration_profile' not in result['sample'] and 'calibration_coefficients' not in result['sample']
    assert 'calibration_revision' not in result['sample'] and 'battery_estimate_basis' not in result['sample']
    if change == 'unsafe_profile':
        assert 'profile' not in result['calibration_validation']
    if change == 'top_profile':
        assert result['calibration_validation']['profile'] == 'local-12v-v1'
    diagnostic = diagnostic_view(result, {}, now=value['heartbeat'])
    assert diagnostic['capture_fresh']
    assert diagnostic['versions']['collector']['version'] == 'test-collector'
    assert diagnostic['connection']['checks'][0]['status'] == 'ok'


def test_legacy_raw_snapshot_is_available_without_inventing_calibration(tmp_path):
    sample = parse_frame(FRAME, 1000)
    sample['ac_input_estimate_w'] = 123  # No matching calibration evidence.
    value = {'schema': 1, 'heartbeat': 1000, 'source': 'usbmon', 'sample': sample}
    result = read(tmp_path / 'latest.json', value)
    assert result['fresh'] and result['sample']['soc'] == sample['soc']
    assert result['sample']['dc_power_estimate_w'] == sample['dc_power_estimate_w']
    assert result['calibration_validation'] == {'valid': False, 'reason': 'missing_metadata'}
    assert result['sample']['ac_input_estimate_w'] is None
    assert 'calibration_profile' not in result['sample'] and 'calibration' not in result


@pytest.mark.parametrize('change', ['soc', 'cells', 'timestamp', 'current', 'warnings', 'decoder'])
def test_invalid_raw_report_still_fails_closed(tmp_path, change):
    value = snapshot()
    key, bad = {'soc': ('soc', 101), 'cells': ('cells', [4, 4, 4, 6]),
                'timestamp': ('timestamp', -1), 'current': ('current', '1.2'),
                'warnings': ('warnings', 'bad'), 'decoder': ('decoder_version', True)}[change]
    value['sample'][key] = bad
    result = read(tmp_path / 'latest.json', value)
    assert result['sample'] is None and not result['fresh']
    assert result['calibration_validation'] == {'valid': False, 'reason': 'snapshot_unavailable'}


@pytest.mark.parametrize('bad', ['{', '{"schema":1,"schema":1}',
                               '{"schema":1,"heartbeat":NaN}', '{"schema":1,"heartbeat":1e999}',
                               ' ' * (MAX_SNAPSHOT_BYTES + 1)])
def test_malformed_nonfinite_duplicate_and_oversized_json_are_rejected(tmp_path, bad):
    path = tmp_path / 'latest.json'
    path.write_text(bad)
    assert load_snapshot(path, now=1000)['sample'] is None


def test_byte_limit_and_freshness_are_independent_of_calibration_validity(tmp_path):
    path = tmp_path / 'latest.json'
    encoded = json.dumps(snapshot()).encode()
    path.write_bytes(encoded + b' ' * (MAX_SNAPSHOT_BYTES - len(encoded)))
    assert load_snapshot(path, now=1000)['calibration_validation']['valid']
    stale = load_snapshot(path, now=1011)
    assert not stale['fresh'] and stale['calibration_validation']['valid']
    path.write_bytes(path.read_bytes() + b' ')
    assert load_snapshot(path, now=1000)['calibration_validation']['reason'] == 'snapshot_unavailable'


@pytest.mark.parametrize('explicit_now', [None, 1002])
def test_atomic_snapshot_replacement_during_read_preserves_freshness(tmp_path, monkeypatch, explicit_now):
    path = tmp_path / 'latest.json'
    store = Store(tmp_path / 'history.sqlite')
    store.ingest(read(path, snapshot(now=1000)), now=1000)
    replacement = tmp_path / 'replacement.json'
    replacement.write_text(json.dumps(snapshot(now=1002.002)))
    clock = {'now': 1002}
    real_open = Path.open

    def replace_before_open(target, *args, **kwargs):
        if target == path:
            replacement.replace(path)
            clock['now'] = 1002.004
        return real_open(target, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(telemetry.time, 'time', lambda: clock['now'])
        patch.setattr(Path, 'open', replace_before_open)
        value = load_snapshot(path, now=explicit_now)
    assert value['server_time'] == (1002.004 if explicit_now is None else explicit_now)
    assert value['fresh'] is (explicit_now is None)
    assert value['calibration_validation']['valid']
    if explicit_now is None:
        store.ingest(value, now=value['server_time'])
        assert [event['detail'] for event in store.events()] == ['online:online']


@pytest.mark.parametrize('sample_ts,heartbeat,fresh', [
    (990, 990, True), (989.999999, 1000, False), (1000, 989.999999, False),
    (1000.000001, 1000, False), (1000, 1000.000001, False),
])
def test_implicit_clock_keeps_exact_stale_and_future_bounds(tmp_path, monkeypatch, sample_ts, heartbeat, fresh):
    value = snapshot(now=sample_ts)
    value['heartbeat'] = heartbeat
    path = tmp_path / 'latest.json'
    path.write_text(json.dumps(value))
    monkeypatch.setattr(telemetry.time, 'time', lambda: 1000)
    result = load_snapshot(path)
    assert result['server_time'] == 1000
    assert result['fresh'] is fresh
    assert result['calibration_validation']['valid']


@pytest.mark.parametrize('malformed', [False, True])
@pytest.mark.parametrize('explicit_now', [None, 999])
def test_snapshot_read_errors_keep_a_valid_evaluation_time(tmp_path, monkeypatch, malformed, explicit_now):
    path = tmp_path / 'latest.json'
    if malformed:
        path.write_text('{')
    monkeypatch.setattr(telemetry.time, 'time', lambda: 1000)
    result = load_snapshot(path, now=explicit_now)
    assert result['server_time'] == (1000 if explicit_now is None else explicit_now)
    assert result['fresh'] is False and result['sample'] is None
    assert result['read_error'] == ('JSONDecodeError' if malformed else 'FileNotFoundError')


def test_bad_calibration_records_raw_history_without_power_or_bad_provenance(tmp_path):
    store = Store(tmp_path / 'history.sqlite')
    value = snapshot(now=1000)
    value['sample']['calibration_profile'] = 'local-12v-v1'
    result = read(tmp_path / 'latest.json', value)
    store.ingest(result, now=1000)
    store.flush()
    point = store.history(1, now=1001)['points'][0]
    assert point['values']['soc'] == value['sample']['soc']
    assert point['values']['cell_1'] == value['sample']['cells'][0]
    assert point['values'].get('ac_input_estimate_w') is None
    assert point['values'].get('battery_energy_estimate_w') is None
    assert 'calibration_profile' not in point['context']
    assert 'calibration_revision' not in point['context']
