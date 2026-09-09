"""Configuration wiring checks use temporary directories, never host services."""
import json
from pathlib import Path

import pytest

from ups_panel.calibration import default_config, save_config
from ups_panel.collector import CalibrationState
from ups_panel.collector_health import calibration_readiness, environment_config
from ups_panel.config_target import config_file_state, target_identity


def reported(path, profile='none'):
    state = CalibrationState(path, profile)
    state.refresh()
    sample = {'calibration_profile': state.config['profile'], 'calibration_revision': state.config['revision'],
              'calibration_coefficients': state.config['coefficients']}
    if state.config['schema'] == 2:
        sample.update(calibration_schema=2, ac_voltage_nominal_v=state.config['ac_voltage_nominal_v'])
    return {'calibration': state.snapshot(), 'sample': sample}


def test_target_identity_survives_directory_alias_and_atomic_save_without_disclosing_path(tmp_path):
    data = tmp_path / 'private-volume-and-user'
    data.mkdir()
    alias = tmp_path / 'container-data'
    alias.symlink_to(data, target_is_directory=True)
    target = data / 'calibration.json'
    identity = target_identity(target)
    assert identity['state'] == 'ready' and len(identity['identity']) == 64
    assert target_identity(alias / target.name) == identity
    assert target_identity(data / 'other.json') != identity
    save_config(target, default_config())
    first_inode = target.stat().st_ino
    save_config(target, default_config('local-19v-v1'))
    assert target.stat().st_ino != first_inode
    assert target_identity(target) == identity
    assert str(tmp_path) not in json.dumps(identity)


def test_target_missing_parent_and_unconfigured_path_are_distinct(tmp_path):
    assert target_identity(None) == {'identity': None, 'state': 'unconfigured'}
    assert target_identity(tmp_path / 'absent' / 'calibration.json')['state'] == 'parent_unavailable'
    assert target_identity(tmp_path / 'bad\nname')['state'] == 'invalid_path'
    assert config_file_state(tmp_path / 'absent' / 'calibration.json') == 'unreadable'


def test_first_install_without_saved_calibration_is_ready_and_does_not_create_file(tmp_path):
    target = tmp_path / 'calibration.json'
    value = reported(target)
    result = calibration_readiness(value, target, require_target=True)
    assert result['ready'] and result['target_verified']
    assert result['file_state'] == 'missing'
    assert result['revision'] == default_config()['revision']
    assert not target.exists()
    assert value['calibration']['supported_profiles'] == ['none', 'local-19v-v1', 'custom']


def test_collector_metadata_distinguishes_missing_invalid_unreadable_and_unconfigured(tmp_path):
    target = tmp_path / 'calibration.json'
    state = CalibrationState(target)
    state.refresh()
    assert state.snapshot()['file_state'] == 'missing'
    target.write_text('{broken')
    state.refresh()
    assert state.snapshot()['file_state'] == 'invalid'
    target.unlink()
    target.symlink_to(tmp_path / 'absent-target')
    state.refresh()
    assert state.snapshot()['file_state'] == 'unreadable'
    assert state.snapshot()['config'] == default_config()
    unconfigured = CalibrationState()
    assert unconfigured.snapshot()['file_state'] == 'unconfigured'
    assert unconfigured.snapshot()['configurable'] is False
    assert str(tmp_path) not in json.dumps(state.snapshot())


@pytest.mark.parametrize('mutation,code', [
    (lambda value: value.pop('calibration'), 'calibration_unconfigured'),
    (lambda value: value['calibration'].update(configurable=False), 'calibration_unconfigured'),
    (lambda value: value['calibration'].update(file_state='unreadable'), 'calibration_unreadable'),
    (lambda value: value['calibration'].update(error='bad config'), 'calibration_incompatible'),
    (lambda value: value['calibration']['config'].update(profile='local-12v-v1'), 'calibration_incompatible'),
    (lambda value: value['calibration']['config'].update(revision='0' * 64), 'calibration_mismatch'),
    (lambda value: value['sample'].update(calibration_revision='0' * 64), 'calibration_mismatch'),
    (lambda value: value['calibration']['config_target'].update(identity='0' * 64), 'calibration_mismatch'),
    (lambda value: value['calibration'].update(file_state='loaded'), 'calibration_mismatch'),
])
def test_specific_runtime_configuration_failures(tmp_path, mutation, code):
    target = tmp_path / 'calibration.json'
    value = reported(target)
    mutation(value)
    result = calibration_readiness(value, target, require_target=True)
    assert not result['ready'] and result['code'] == code


def test_old_collector_requires_content_match_and_reports_unverified_target(tmp_path):
    target = tmp_path / 'calibration.json'
    value = reported(target)
    value['calibration'].pop('config_target')
    value['calibration'].pop('file_state')
    result = calibration_readiness(value, target)
    assert result['ready'] and not result['target_verified']
    assert calibration_readiness(value, target, require_target=True)['code'] == 'calibration_target_unverified'
    value['sample']['calibration_revision'] = '0' * 64
    assert calibration_readiness(value, target)['code'] == 'calibration_mismatch'


def test_supported_custom_12v_is_ready_while_local_patch_profile_is_rejected(tmp_path):
    target = tmp_path / 'calibration.json'
    custom = {'schema': 2, 'profile': 'custom', 'ac_voltage_nominal_v': 12,
              'coefficients': {'base_gain': 1.1, 'charge_gain': None, 'battery_gain': None}}
    save_config(target, custom)
    assert calibration_readiness(reported(target), target, require_target=True)['ready']
    target.write_text(json.dumps(dict(custom, profile='local-12v-v1')))
    result = calibration_readiness(reported(target), target, require_target=True)
    assert not result['ready'] and result['code'] == 'calibration_incompatible'


@pytest.mark.parametrize('patch', [{'calibration_coefficients': None},
    {'calibration_coefficients': {'base_gain': True, 'charge_gain': 1, 'battery_gain': 1}},
    {'calibration_coefficients': {'base_gain': 1, 'charge_gain': 1, 'battery_gain': 1}},
    {'calibration_schema': True}, {'ac_voltage_nominal_v': 12}])
def test_matching_revision_does_not_hide_changed_or_missing_sample_coefficients(tmp_path, patch):
    target = tmp_path / 'calibration.json'
    save_config(target, default_config('local-19v-v1'))
    value = reported(target)
    value['sample'].update(patch)
    assert calibration_readiness(value, target)['code'] == 'calibration_mismatch'


def test_unknown_environment_preset_is_rejected_even_when_saved_config_is_valid(tmp_path):
    target = tmp_path / 'calibration.json'
    save_config(target, default_config())
    assert calibration_readiness(reported(target), target, 'local-12v-v1')['code'] == 'calibration_incompatible'


def test_environment_reader_uses_only_calibration_settings_and_never_executes_contents(tmp_path):
    env = tmp_path / 'collector.env'
    env.write_text(f'OTHER_SECRET=do-not-return\nUPS_CALIBRATION_CONFIG="{tmp_path}/data with spaces/calibration.json"\nUPS_CALIBRATION_PROFILE=none\n')
    path, profile = environment_config(env)
    assert path == str(tmp_path / 'data with spaces/calibration.json') and profile == 'none'
    env.write_text('UPS_CALIBRATION_CONFIG=relative/calibration.json\n')
    with pytest.raises(ValueError, match='absolute'):
        environment_config(env)
    env.write_text('UPS_CALIBRATION_CONFIG="unterminated\n')
    with pytest.raises(ValueError):
        environment_config(env)
