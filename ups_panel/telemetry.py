"""Validate raw telemetry independently from optional empirical power estimates.

A calibration error cannot erase a valid USB report or the collector's identity.
Only a complete, matching configuration may retain calculated power/provenance;
rejected calibration is diagnostic evidence, never an implicit ``none`` preset.
"""
import json
from pathlib import Path
import re
import time

from .calibration import CalibrationError, PROFILES, normalize_config
from .power import ESTIMATE_FIELDS, finite_number
from .storage import METRICS


MAX_SNAPSHOT_BYTES = 65536
ESTIMATE_POWER_FIELDS = ('ac_input_estimate_w', 'battery_energy_estimate_w')
RAW_METRICS = tuple(key for key in METRICS if key not in ESTIMATE_POWER_FIELDS)
_PROFILE = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,63}\Z')


def _validation(valid, reason, profile=None):
    result = {'valid': valid, 'reason': reason}
    if isinstance(profile, str) and _PROFILE.fullmatch(profile):
        result['profile'] = profile
    return result


def _config(raw):
    """Snapshots must carry actual coefficients/revision, not preset shortcuts."""
    if not isinstance(raw, dict) or not {'schema', 'profile', 'coefficients', 'revision'} <= raw.keys():
        return None, 'missing_metadata'
    profile = raw['profile']
    if not isinstance(profile, str) or not _PROFILE.fullmatch(profile):
        return None, 'invalid_config'
    if profile not in PROFILES:
        return None, 'unsupported_profile'
    if profile != 'none' and not isinstance(raw['coefficients'], dict):
        return None, 'invalid_config'
    try:
        config = normalize_config(raw)
    except (CalibrationError, TypeError, ValueError, OverflowError):
        return None, 'invalid_config'
    if raw['revision'] != config['revision']:
        return None, 'revision_mismatch'
    return config, None


def _valid_estimates(sample, config):
    profile, revision = config['profile'], config['revision']
    custom = profile == 'custom'
    expected_ac = (f'us3000_custom_v2_{revision}' if config['schema'] == 2 else
                   f'us3000_19v_custom_{revision}' if custom else
                   'us3000_19v_v1' if profile == 'local-19v-v1' else None)
    expected_battery = (f'us3000_battery_custom_{revision}' if custom else
                        'nominal_43_2wh_soc_v1' if profile == 'local-19v-v1' else None)
    if ('ac_estimate_model' not in sample or 'battery_estimate_basis' not in sample
            or sample['ac_estimate_model'] != expected_ac
            or sample['battery_estimate_basis'] != expected_battery):
        return False
    # These are empirical models. A snapshot cannot promote their precision.
    if sample.get('calibration_verified', False) is not False:
        return False
    ac, battery = (sample.get(key) for key in ESTIMATE_POWER_FIELDS)
    if any(value is not None and (not finite_number(value) or value < 0) for value in (ac, battery)):
        return False
    if profile == 'none':
        return (ac is None and battery is None
                and sample.get('ac_estimate_quality') == 'not_configured'
                and sample.get('battery_estimate_quality') == 'not_configured')
    ac_success = ('custom_unverified',) if custom else ('calibrated_range', 'extrapolated')
    battery_success = ('custom_unverified',) if custom else ('nominal_capacity_assumption',)
    ac_quality, battery_quality = sample.get('ac_estimate_quality'), sample.get('battery_estimate_quality')
    if ac_quality not in (*ac_success, 'warming_up', 'unsupported_voltage', 'invalid_data', 'unavailable', 'charge_not_configured'):
        return False
    if battery_quality not in (*battery_success, 'warming_up', 'invalid_data', 'unavailable', 'battery_not_configured'):
        return False
    if (ac is not None) != (ac_quality in ac_success) or (battery is not None) != (battery_quality in battery_success):
        return False
    mode = sample['mode']
    if ac is not None:
        voltage = sample.get('input_voltage')
        nominal = config.get('ac_voltage_nominal_v', 19)
        if (mode not in ('online', 'charging') or not finite_number(voltage)
                or not nominal - 1 <= voltage <= nominal + 1
                or mode == 'charging' and config['coefficients']['charge_gain'] is None):
            return False
    # A positive raw power times a small positive gain may round to 0.00 W on
    # the display. Battery Wh still uses the unsmoothed positive product.
    if battery is not None and (mode != 'battery' or config['coefficients']['battery_gain'] is None):
        return False
    return True


def validate_calibration(view):
    """Check sample and active identities without using one to fill the other."""
    sample = view.get('sample')
    if not isinstance(sample, dict):
        return _validation(False, 'missing_metadata')
    profile = sample.get('calibration_profile')
    if isinstance(profile, str) and _PROFILE.fullmatch(profile) and profile not in PROFILES:
        return _validation(False, 'unsupported_profile', profile)
    required = ('calibration_profile', 'calibration_revision', 'calibration_coefficients')
    if any(key not in sample for key in required):
        return _validation(False, 'missing_metadata', profile)
    raw_config = {'schema': sample.get('calibration_schema', 1), 'profile': profile,
                  'coefficients': sample['calibration_coefficients'], 'revision': sample['calibration_revision']}
    if 'ac_voltage_nominal_v' in sample:
        raw_config['ac_voltage_nominal_v'] = sample['ac_voltage_nominal_v']
    config, reason = _config(raw_config)
    if reason:
        return _validation(False, reason, profile)
    metadata = view.get('calibration')
    reported_active = metadata.get('config') if isinstance(metadata, dict) else None
    active, reason = _config(reported_active)
    if reason:
        reported_profile = reported_active.get('profile') if isinstance(reported_active, dict) else None
        return _validation(False, reason, reported_profile if reason == 'unsupported_profile' else profile)
    if config != active:
        return _validation(False, 'config_mismatch', profile)
    if not _valid_estimates(sample, config):
        return _validation(False, 'invalid_estimate', profile)
    return _validation(True, None, profile)


def _disable_estimates(sample):
    # Candidate current/power channels and V*I remain raw hypotheses; none of
    # them is an empirical calibration field or eligible calibrated provenance.
    for key in ESTIMATE_FIELDS:
        sample.pop(key, None)
    sample.update(ac_input_estimate_w=None, battery_energy_estimate_w=None,
                  ac_estimate_quality='invalid_calibration', battery_estimate_quality='invalid_calibration',
                  calibration_verified=False)


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Duplicate snapshot field')
        result[key] = value
    return result


def load_snapshot(path, now=None):
    try:
        with Path(path).open('rb') as source:
            encoded = source.read(MAX_SNAPSHOT_BYTES + 1)
        if len(encoded) > MAX_SNAPSHOT_BYTES:
            raise ValueError('Snapshot too large')

        def reject_constant(value):
            raise ValueError(f'Invalid JSON number: {value}')

        def parse_number(value):
            number = float(value)
            if not finite_number(number):
                raise ValueError('Non-finite JSON number')
            return number

        value = json.loads(encoded, object_pairs_hook=_unique_object,
                           parse_constant=reject_constant, parse_float=parse_number)
        if not isinstance(value, dict) or type(value.get('schema')) is not int or value['schema'] != 1:
            raise ValueError('Unknown snapshot schema')
        if not finite_number(value.get('heartbeat')):
            raise ValueError('Invalid heartbeat')
        sample = value.get('sample')
        if sample is not None:
            if (not isinstance(sample, dict) or sample.get('mode') not in ('online', 'charging', 'battery', 'unknown')
                    or not finite_number(sample.get('timestamp')) or sample['timestamp'] < 0
                    or not isinstance(sample.get('cells'), list) or len(sample['cells']) != 4):
                raise ValueError('Invalid sample')
            if (not finite_number(sample.get('soc')) or not 0 <= sample['soc'] <= 100
                    or any(not finite_number(cell) or not 1 <= cell <= 5 for cell in sample['cells'])):
                raise ValueError('Invalid battery field')
            for key in RAW_METRICS:
                if sample.get(key) is not None and not finite_number(sample[key]):
                    raise ValueError('Invalid numeric field')
            for key in ('formula_version', 'decoder_version'):
                if key in sample and (type(sample[key]) is not int or sample[key] < 1):
                    raise ValueError('Invalid sample version')
            if (not isinstance(sample.get('warnings', []), list)
                    or any(not isinstance(warning, str) for warning in sample.get('warnings', []))):
                raise ValueError('Invalid warnings')
        # The collector atomically replaces this file. Take the implicit clock
        # after reading it, so a publication during the read cannot look future-
        # dated relative to a time captured before opening the newer snapshot.
        now = time.time() if now is None else now
        age = now - sample['timestamp'] if sample else None
        heartbeat_age = now - value['heartbeat']
        value.update(fresh=sample is not None and 0 <= age <= 10 and 0 <= heartbeat_age <= 10,
                     age_sec=round(max(0, age), 1) if age is not None else None, server_time=now)
        nut = value.get('nut')
        if (not isinstance(nut, dict) or not isinstance(nut.get('available', False), bool)
                or not finite_number(nut.get('timestamp', 0))):
            value['nut'] = {'available': False}
        value['calibration_validation'] = validate_calibration(value)
        if sample is not None and not value['calibration_validation']['valid']:
            _disable_estimates(sample)
        return value
    except (OSError, ValueError, TypeError, KeyError, RecursionError) as exc:
        now = time.time() if now is None else now
        return {'fresh': False, 'sample': None, 'age_sec': None, 'server_time': now,
                'source': 'unavailable', 'nut': {'available': False},
                'diagnostics': {'error': '尚未收到有效采集数据'}, 'read_error': type(exc).__name__,
                'calibration_validation': _validation(False, 'snapshot_unavailable')}
