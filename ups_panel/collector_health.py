"""Read-only host checks for collector configuration; never return local paths."""
from pathlib import Path
import shlex

from .calibration import CalibrationError, default_config, load_config, normalize_config
from .config_target import config_file_state, target_identity


def environment_config(path):
    """Read the installer's two calibration settings without executing shell text."""
    values = {'UPS_CALIBRATION_CONFIG': None, 'UPS_CALIBRATION_PROFILE': 'none'}
    with Path(path).open('rb') as stream:
        encoded = stream.read(65537)
    if len(encoded) > 65536:
        raise ValueError('Calibration environment is too large.')
    for line in encoded.decode('utf-8').splitlines():
        key, separator, content = line.strip().partition('=')
        if separator and key in values:
            parsed = shlex.split(content, comments=False)
            if len(parsed) != 1:
                raise ValueError('Invalid calibration environment setting.')
            values[key] = parsed[0]
    path = values['UPS_CALIBRATION_CONFIG']
    if path and (not Path(path).is_absolute() or any(ord(c) < 32 for c in path)):
        raise ValueError('Calibration path must be absolute.')
    return path, values['UPS_CALIBRATION_PROFILE']


def calibration_readiness(snapshot, path, profile='none', *, require_target=False):
    target = target_identity(path)
    state = config_file_state(path)
    result = {'ready': False, 'code': None, 'target_verified': False,
              'target_identity': target['identity'], 'file_state': state, 'revision': None}
    def failed(code):
        return dict(result, code=code)
    if not path:
        return failed('calibration_unconfigured')
    if target['state'] != 'ready' or state == 'unreadable':
        return failed('calibration_unreadable')
    try:
        # The collector constructs its fallback before reading the saved file.
        # Even a valid custom file cannot make an unknown environment preset safe.
        fallback = default_config(profile)
        desired = load_config(path) or fallback
    except CalibrationError as exc:
        return failed('calibration_unreadable' if exc.code == 'file_unreadable' else 'calibration_incompatible')
    result['revision'] = desired['revision']
    metadata = snapshot.get('calibration') if isinstance(snapshot, dict) else None
    if not isinstance(metadata, dict) or metadata.get('configurable') is not True:
        return failed('calibration_unconfigured')
    if metadata.get('file_state') == 'unreadable':
        return failed('calibration_unreadable')
    if metadata.get('error') or metadata.get('file_state') == 'invalid':
        return failed('calibration_incompatible')
    for field, expected in (('supported_config_schemas', desired['schema']), ('supported_profiles', desired['profile'])):
        supported = metadata.get(field)
        if supported is not None and (not isinstance(supported, list)
                or not any(type(item) is type(expected) and item == expected for item in supported)):
            return failed('calibration_incompatible')
    remote_target = metadata.get('config_target')
    if remote_target is not None:
        if (not isinstance(remote_target, dict) or remote_target.get('state') != 'ready'
                or remote_target.get('identity') != target['identity']):
            return failed('calibration_mismatch')
        result['target_verified'] = True
    elif require_target:
        return failed('calibration_target_unverified')
    if metadata.get('file_state') is not None and metadata['file_state'] != state:
        return failed('calibration_mismatch')
    try:
        active = metadata.get('config')
        normalized = normalize_config(active)
    except CalibrationError:
        return failed('calibration_incompatible')
    if active.get('revision') != normalized['revision'] or normalized != desired:
        return failed('calibration_mismatch')
    sample = snapshot.get('sample')
    if (not isinstance(sample, dict) or sample.get('calibration_revision') != desired['revision']
            or sample.get('calibration_profile') != desired['profile']):
        return failed('calibration_mismatch')
    # A matching revision string cannot excuse missing or altered coefficients.
    # normalize_config intentionally supplies preset coefficients for UI input;
    # runtime metadata must instead state the coefficients it actually used.
    if desired['profile'] != 'none' and not isinstance(sample.get('calibration_coefficients'), dict):
        return failed('calibration_mismatch')
    sample_config = {'schema': sample.get('calibration_schema', 1), 'profile': sample['calibration_profile'],
                     'coefficients': sample.get('calibration_coefficients')}
    if sample_config['schema'] == 2:
        sample_config['ac_voltage_nominal_v'] = sample.get('ac_voltage_nominal_v')
    elif sample.get('ac_voltage_nominal_v') is not None:
        return failed('calibration_mismatch')
    try:
        if normalize_config(sample_config) != desired:
            return failed('calibration_mismatch')
    except CalibrationError:
        return failed('calibration_mismatch')
    return dict(result, ready=True)
