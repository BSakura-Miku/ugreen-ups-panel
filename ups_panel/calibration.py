"""Validated, versioned coefficient configuration shared by the API and collector."""
import hashlib
import json
import math
import os
from pathlib import Path
import secrets
import stat


MAX_CONFIG_BYTES = 4096
DEFAULT_COEFFICIENTS = {
    'base_gain': 1.182379,
    'charge_gain': 1.2843154306288043,
    'battery_gain': 1.2091130139203523,
}
PRESET_PROFILES = ('none', 'local-19v-v1')
PROFILES = (*PRESET_PROFILES, 'custom')


class CalibrationError(ValueError):
    """A calibration configuration could not be validated, read, or saved."""


def _coefficients(value):
    if not isinstance(value, dict) or set(value) != set(DEFAULT_COEFFICIENTS):
        raise CalibrationError('校准系数必须包含 base_gain、charge_gain、battery_gain，且不能有其他字段')
    result = {}
    for name in DEFAULT_COEFFICIENTS:
        number = value[name]
        if isinstance(number, bool) or not isinstance(number, (int, float)):
            raise CalibrationError('校准系数必须是有限数值')
        try:
            number = float(number)
        except (OverflowError, ValueError):
            raise CalibrationError('校准系数必须是有限数值') from None
        if not math.isfinite(number):
            raise CalibrationError('校准系数必须是有限数值')
        if number > 10 or (number < 0 if name == 'charge_gain' else number <= 0):
            raise CalibrationError('基底和电池系数必须大于 0 且不超过 10；充电系数必须在 0–10 之间')
        # Canonicalize negative zero as well as integer-versus-float spellings.
        result[name] = 0.0 if number == 0 else number
    return result


def normalize_config(data):
    """Return canonical content with a content-derived revision; never trust its input revision."""
    if not isinstance(data, dict) or set(data) - {'schema', 'profile', 'coefficients', 'revision'}:
        raise CalibrationError('校准配置包含未知字段或不是对象')
    if type(data.get('schema')) is not int or data['schema'] != 1:
        raise CalibrationError('不支持的校准配置版本')
    profile = data.get('profile')
    if not isinstance(profile, str) or profile not in PROFILES:
        raise CalibrationError('未知校准配置')
    supplied = data.get('coefficients')
    if profile == 'none':
        if supplied is not None:
            raise CalibrationError('关闭校准时 coefficients 必须为 null')
        coefficients = None
    elif profile == 'local-19v-v1':
        if supplied is not None and _coefficients(supplied) != DEFAULT_COEFFICIENTS:
            raise CalibrationError('开发样机配置的系数不可修改；请使用 custom')
        coefficients = dict(DEFAULT_COEFFICIENTS)
    else:
        coefficients = _coefficients(supplied)
    config = {'schema': 1, 'profile': profile, 'coefficients': coefficients}
    canonical = json.dumps(config, sort_keys=True, separators=(',', ':'), allow_nan=False)
    config['revision'] = hashlib.sha256(canonical.encode('utf-8')).hexdigest()
    return config


def default_config(profile='none'):
    """Return a preset configuration; custom coefficients must be supplied explicitly."""
    if profile not in PRESET_PROFILES:
        raise CalibrationError(f'Unknown calibration profile: {profile}')
    return normalize_config({'schema': 1, 'profile': profile})


def _unique_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise CalibrationError('校准配置包含重复字段')
        value[key] = item
    return value


def _reject_constant(_):
    raise CalibrationError('校准配置包含非有限数值')


def load_config(path):
    """Read at most 4 KiB from a regular, non-symlink file, or return None if absent."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise CalibrationError('校准配置不可读取或不是普通文件') from exc
    try:
        with os.fdopen(fd, 'rb') as source:
            metadata = os.fstat(source.fileno())
            if not stat.S_ISREG(metadata.st_mode):
                raise CalibrationError('校准配置必须为普通文件')
            if metadata.st_size > MAX_CONFIG_BYTES:
                raise CalibrationError('校准配置超过 4 KiB')
            encoded = source.read(MAX_CONFIG_BYTES + 1)
        if len(encoded) > MAX_CONFIG_BYTES:
            raise CalibrationError('校准配置超过 4 KiB')
        data = json.loads(encoded.decode('utf-8'), object_pairs_hook=_unique_object,
                          parse_constant=_reject_constant)
        return normalize_config(data)
    except CalibrationError:
        raise
    except (OSError, UnicodeError, ValueError, RecursionError) as exc:
        raise CalibrationError('校准配置不是有效 JSON') from exc


def save_config(path, config):
    """Atomically replace a configuration with mode 0640, without following its symlink."""
    normalized = normalize_config(config)
    encoded = (json.dumps(normalized, ensure_ascii=False, sort_keys=True, indent=2,
                          allow_nan=False) + '\n').encode('utf-8')
    path = Path(path)
    directory_fd = None
    temporary = None
    try:
        # Hold the parent directory open so all final operations address the same directory.
        directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            metadata = os.stat(path.name, dir_fd=directory_fd, follow_symlinks=False)
        except FileNotFoundError:
            metadata = None
        if metadata is not None and not stat.S_ISREG(metadata.st_mode):
            raise CalibrationError('校准配置必须为普通文件，不能覆盖链接或目录')
        temporary = '.calibration-' + secrets.token_hex(12)
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                     0o640, dir_fd=directory_fd)
        with os.fdopen(fd, 'wb') as destination:
            destination.write(encoded)
            destination.flush()
            os.fchmod(destination.fileno(), 0o640)
            os.fsync(destination.fileno())
        os.replace(temporary, path.name, src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
        temporary = None
        os.fsync(directory_fd)
        return normalized
    except CalibrationError:
        raise
    except OSError as exc:
        raise CalibrationError('校准配置保存失败') from exc
    finally:
        if directory_fd is not None:
            if temporary is not None:
                try:
                    os.unlink(temporary, dir_fd=directory_fd)
                except FileNotFoundError:
                    pass
            os.close(directory_fd)
