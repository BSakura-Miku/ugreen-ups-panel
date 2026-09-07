#!/usr/bin/env python3
"""Install/restore only this project's passive collector; never manage NUT."""
import argparse
from contextlib import contextmanager
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import shlex
import signal
import stat
import subprocess
import tempfile
import time

BASE = Path('/opt/ugreen-ups-panel')
CONFIGS = {
    'service': Path('/etc/systemd/system/ugreen-ups-collector.service'),
    'tmpfiles': Path('/etc/tmpfiles.d/ugreen-ups-panel.conf'),
    'env': Path('/etc/ugreen-ups-panel.env'),
}
SNAPSHOT = Path('/run/ugreen-ups-panel/latest.json')
UNIT = 'ugreen-ups-collector.service'
LOCK = Path('/run/lock/ugreen-ups-panel-install.lock')


@contextmanager
def transaction_lock():
    """Serialize manual installs, rollbacks and the optional host updater."""
    fd = os.open(LOCK, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != 0:
            raise RuntimeError('Collector transaction lock is not a root-owned regular file.')
        os.fchmod(fd, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError('Another collector installation or rollback is in progress.') from None
        yield
    finally:
        os.close(fd)


@contextmanager
def termination_recovery():
    def interrupted(signum, frame):
        # Give the existing BaseException recovery path time to complete.
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        raise KeyboardInterrupt
    previous = signal.signal(signal.SIGTERM, interrupted)
    try:
        yield
    finally:
        signal.signal(signal.SIGTERM, previous)


def current_source_sha256():
    package = BASE / 'current' / 'ups_panel'
    if package.is_symlink() or not package.is_dir():
        raise ValueError('Current collector source is unavailable.')
    paths = sorted(package.glob('*.py'))
    if not paths:
        raise ValueError('Current collector source is unavailable.')
    digest = hashlib.sha256()
    for path in paths:
        if path.is_symlink() or not path.is_file():
            raise ValueError('Current collector source contains an unsafe file.')
        digest.update(path.name.encode('utf-8') + b'\0')
        digest.update(path.read_bytes())
        digest.update(b'\0')
    return digest.hexdigest()


def command(*args, check=True):
    return subprocess.run(args, check=check, capture_output=True, text=True, timeout=30)


def link_to(target, link):
    temporary = link.with_name(link.name + '.new')
    temporary.unlink(missing_ok=True)
    temporary.symlink_to(target)
    temporary.replace(link)


def save_state(directory, preserve_host_config=False):
    directory.mkdir(parents=True, exist_ok=True)
    os.chmod(directory, 0o700)
    state = {'current': os.readlink(BASE / 'current') if (BASE / 'current').is_symlink() else None,
             'previous': os.readlink(BASE / 'previous') if (BASE / 'previous').is_symlink() else None,
             'active': command('systemctl', 'is-active', '--quiet', UNIT, check=False).returncode == 0,
             'enabled': command('systemctl', 'is-enabled', '--quiet', UNIT, check=False).returncode == 0,
             'preserve_host_config': preserve_host_config,
             'configs': {}}
    if not preserve_host_config:
        for name, path in CONFIGS.items():
            state['configs'][name] = path.exists()
            if path.exists():
                shutil.copy2(path, directory / name)
    (directory / 'state.json').write_text(json.dumps(state))
    return state


def restore_state(directory):
    state = json.loads((directory / 'state.json').read_text())
    stopped = command('systemctl', 'stop', UNIT, check=False)
    if stopped.returncode:
        active = command('systemctl', 'is-active', '--quiet', UNIT, check=False)
        if active.returncode not in (3, 4):
            raise RuntimeError('Collector stop could not be confirmed; recovery files were retained.')
    preserve = state.get('preserve_host_config') is True
    if not preserve:
        for name, path in CONFIGS.items():
            if state['configs'][name]:
                shutil.copy2(directory / name, path)
            else:
                path.unlink(missing_ok=True)
    for name in ('current', 'previous'):
        if state[name]:
            link_to(state[name], BASE / name)
        else:
            (BASE / name).unlink(missing_ok=True)
    if not preserve:
        command('systemctl', 'daemon-reload')
        command('systemctl', 'enable' if state['enabled'] else 'disable', UNIT, check=state['enabled'])
    if state['active']:
        command('systemctl', 'start', UNIT)
    return state


def validate_fresh(started, timeout=20):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with SNAPSHOT.open('rb') as source:
                encoded = source.read(65537)
            if len(encoded) > 65536:
                raise ValueError('Snapshot too large')
            snapshot = json.loads(encoded)
            if type(snapshot.get('schema')) is not int or snapshot['schema'] != 1 or snapshot.get('source') != 'usbmon':
                raise ValueError('Expected a live USB snapshot')
            sample = snapshot.get('sample')
            timestamp = sample['timestamp']
            heartbeat = snapshot.get('heartbeat')
            now = time.time()
            def fresh(value):
                return (isinstance(value, (int, float)) and not isinstance(value, bool)
                        and math.isfinite(value) and started <= value <= now and now - value < 10)
            if fresh(timestamp) and fresh(heartbeat):
                return
        except (OSError, ValueError, KeyError, TypeError, AttributeError, OverflowError, RecursionError):
            pass
        time.sleep(1)
    raise RuntimeError(f'No fresh UPS telemetry within {timeout} seconds; check USB connection and the existing UPS driver.')


def write_environment(profile, calibration_path):
    path = CONFIGS['env']
    text = path.read_text() if path.exists() else '# Passive UPS collector configuration\nUPS_SERIAL=\nUPS_NUT_TARGET=ups0@localhost\n'
    if profile is not None:
        text = '\n'.join(line for line in text.splitlines() if not line.strip().startswith('UPS_CALIBRATION_PROFILE=')) + '\n'
        text += 'UPS_CALIBRATION_PROFILE=' + profile + '\n'
    elif not any(line.strip().startswith('UPS_CALIBRATION_PROFILE=') for line in text.splitlines()):
        text += '\nUPS_CALIBRATION_PROFILE=none\n'
    text = '\n'.join(line for line in text.splitlines()
                     if not line.strip().startswith('UPS_CALIBRATION_CONFIG=')) + '\n'
    text += 'UPS_CALIBRATION_CONFIG=' + json.dumps(str(calibration_path), ensure_ascii=False) + '\n'
    path.write_text(text)
    path.chmod(0o600)


def prepare_data_directory(source, data_dir=None):
    """Prepare only the bind-mount directory; preserve every existing data file."""
    data = Path(data_dir) if data_dir is not None else source / 'data'
    if data.is_symlink() or (data.exists() and not data.is_dir()):
        raise ValueError('Project data must be a directory, not a symlink or file.')
    data.mkdir(mode=0o750, exist_ok=True)
    # Operate on the opened directory, never follow a substituted symlink.
    fd = os.open(data, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        current = os.fstat(fd)
        if (current.st_uid, current.st_gid) != (10001, 10001):
            os.fchown(fd, 10001, 10001)
        if current.st_mode & 0o7777 != 0o750:
            os.fchmod(fd, 0o750)
    finally:
        os.close(fd)
    return data.resolve()


def installation_data_directory(source, data_dir):
    if data_dir is not None:
        chosen = Path(data_dir)
    else:
        chosen = source / 'data'
        env = CONFIGS['env']
        for line in env.read_text().splitlines() if env.exists() else []:
            if line.strip().startswith('UPS_CALIBRATION_CONFIG='):
                values = shlex.split(line.split('=', 1)[1], comments=False)
                if len(values) != 1 or Path(values[0]).name != 'calibration.json':
                    raise ValueError('Invalid existing calibration path; specify --data-dir explicitly.')
                chosen = Path(values[0]).parent
    if not chosen.is_absolute() or any(ord(c) < 32 for c in str(chosen)):
        raise ValueError('Data directory must be an absolute path without control characters.')
    return chosen


def release_metadata(source):
    """Copy only the fixed identity fields; package authentication is upstream."""
    path = source / 'collector-release.json'
    if not path.exists() and not path.is_symlink():
        return None
    if path.is_symlink() or not path.is_file() or path.stat().st_nlink != 1:
        raise ValueError('Collector release metadata must be a regular file.')
    with path.open('rb') as handle:
        data = handle.read(16385)
    if len(data) > 16384:
        raise ValueError('Collector release metadata is too large.')
    value = json.loads(data)
    required = {'schema', 'version', 'revision', 'source_sha256',
                'minimum_updater_schema', 'calibration_schemas'}
    if (not isinstance(value, dict) or set(value) != required
            or type(value.get('schema')) is not int or value['schema'] != 1
            or type(value.get('minimum_updater_schema')) is not int
            or value['minimum_updater_schema'] != 1
            or not isinstance(value.get('version'), str)
            or re.fullmatch(r'(?:0|[1-9]\d{0,5})\.(?:0|[1-9]\d{0,5})\.(?:0|[1-9]\d{0,5})', value['version']) is None
            or not isinstance(value.get('revision'), str)
            or re.fullmatch(r'[0-9a-f]{40}', value['revision']) is None
            or not isinstance(value.get('source_sha256'), str)
            or re.fullmatch(r'[0-9a-f]{64}', value['source_sha256']) is None
            or not isinstance(value.get('calibration_schemas'), list)
            or any(type(item) is not int for item in value['calibration_schemas'])
            or value['calibration_schemas'] != [1, 2]):
        raise ValueError('Collector release metadata has invalid identity fields.')
    return json.dumps(value, sort_keys=True, separators=(',', ':')) + '\n'


def source_license(source, required=False):
    path = source / 'LICENSE'
    if not path.exists() and not path.is_symlink() and not required:
        return None
    if (path.is_symlink() or not path.is_file() or path.stat().st_nlink != 1):
        raise ValueError('Collector LICENSE must be a regular file.')
    with path.open('rb') as handle:
        data = handle.read(32769)
    if not data or len(data) > 32768:
        raise ValueError('Collector LICENSE is missing or exceeds its size limit.')
    data.decode('utf-8')
    return data


def install(source, profile=None, data_dir=None, preserve_host_config=False):
    source = Path(source).resolve(strict=True)
    if preserve_host_config and (profile is not None or data_dir is not None):
        raise ValueError('Preserving host configuration cannot change calibration or the data directory.')
    package = source / 'ups_panel'
    if package.is_symlink():
        raise ValueError('Collector source package must not be a symlink.')
    python_sources = sorted(package.glob('*.py'))
    if any(path.is_symlink() or not path.is_file() for path in python_sources):
        raise ValueError('Collector source must contain only regular Python files.')
    metadata = release_metadata(source)
    license_content = source_license(source, required=metadata is not None)
    for filename in ('__init__.py', 'collector.py', 'protocol.py', 'power.py', 'calibration.py', 'usbmon.py', 'build_info.py', 'doctor.py'):
        if not (source / 'ups_panel' / filename).is_file():
            raise ValueError(f'Collector source is incomplete: {filename}')
    if not preserve_host_config:
        for filename in ('ugreen-ups-collector.service', 'ugreen-ups-panel.tmpfiles.conf'):
            if not (source / 'deploy' / filename).is_file():
                raise ValueError(f'Deployment source is incomplete: {filename}')
    elif not ((BASE / 'current').is_symlink() and (BASE / 'current').is_dir()
              and all(path.is_file() and not path.is_symlink() for path in CONFIGS.values())):
        raise ValueError('Preserving host configuration requires an existing installed collector.')
    if preserve_host_config:
        if command('systemctl', 'is-active', '--quiet', UNIT, check=False).returncode != 0:
            raise ValueError('Preserving host configuration requires an active collector.')
        validate_fresh(time.time() - 10, timeout=1)
    command('/usr/bin/python3', '-c', 'import ctypes, select, sqlite3; import sys; assert sys.version_info >= (3, 10), "Python 3.10+ required"')
    if not preserve_host_config:
        command('/sbin/modinfo', 'usbmon')
        data = prepare_data_directory(source, installation_data_directory(source, data_dir))
    (BASE / 'releases').mkdir(parents=True, exist_ok=True)
    release = Path(tempfile.mkdtemp(prefix=time.strftime('%Y%m%d-%H%M%S-'), dir=BASE / 'releases'))
    release.chmod(0o755)
    package_destination = release / 'ups_panel'
    package_destination.mkdir(mode=0o755)
    package_destination.chmod(0o755)
    for path in python_sources:
        copied = package_destination / path.name
        shutil.copy2(path, copied)
        copied.chmod(0o644)
    if metadata is not None:
        metadata_destination = release / 'collector-release.json'
        metadata_destination.write_text(metadata)
        metadata_destination.chmod(0o644)
    if license_content is not None:
        license_destination = release / 'LICENSE'
        license_destination.write_bytes(license_content)
        license_destination.chmod(0o644)
    before = save_state(release / 'rollback', preserve_host_config=preserve_host_config)
    if preserve_host_config and not before['active']:
        raise ValueError('The collector stopped before the update; its state was not changed.')
    marker = BASE / 'module-initial-state'
    if not preserve_host_config and not marker.exists():
        marker.write_text('preexisting\n' if Path('/sys/module/usbmon').exists() else 'absent\n')
    try:
        if not preserve_host_config:
            write_environment(profile, data / 'calibration.json')
            for name, filename in (('service', 'ugreen-ups-collector.service'), ('tmpfiles', 'ugreen-ups-panel.tmpfiles.conf')):
                shutil.copyfile(source / 'deploy' / filename, CONFIGS[name])
                CONFIGS[name].chmod(0o644)
            command('systemd-tmpfiles', '--create', str(CONFIGS['tmpfiles']))
        link_to(str(release), BASE / 'current')
        if not preserve_host_config:
            command('systemctl', 'daemon-reload')
        command('systemctl', 'restart', UNIT)
        # Only a report completed after restart qualifies. A last report from
        # the previous process must not make a broken release appear healthy.
        print('{"stage":"validating"}', flush=True)
        validate_fresh(time.time())
        if not preserve_host_config:
            command('systemctl', 'enable', UNIT)
        if before['current']:
            link_to(before['current'], BASE / 'previous')
    except BaseException as installation_error:
        try:
            restore_state(release / 'rollback')
        except BaseException as restore_error:
            print('{"outcome":"recovery_failed"}', flush=True)
            raise RuntimeError(f'Installation and restoration failed. Recovery files retained at {release / "rollback"}: {restore_error}') from installation_error
        print('{"outcome":"restored"}', flush=True)
        print('Installation failed. Previous collector code, configuration and service state restored.')
        raise
    print(f'Installed {release}. Fresh UPS telemetry verified.')


def rollback():
    current = (BASE / 'current').resolve(strict=True)
    backup = current / 'rollback'
    state = json.loads((backup / 'state.json').read_text())
    previous = Path(state['current']) if state['current'] else None
    if previous is not None and not previous.is_absolute():
        previous = BASE / previous
    if previous is None or not previous.is_dir():
        raise RuntimeError('No previous collector release is available.')
    rescue = Path(tempfile.mkdtemp(prefix='rollback-rescue-', dir=BASE))
    save_state(rescue, preserve_host_config=state.get('preserve_host_config') is True)
    recovered = False
    try:
        restored = restore_state(backup)
        if restored['active']:
            validate_fresh(time.time())
        recovered = True
    except BaseException as rollback_error:
        try:
            restore_state(rescue)
            recovered = True
        except BaseException as rescue_error:
            raise RuntimeError(f'Rollback and recovery failed. Recovery files retained at {rescue}: {rescue_error}') from rollback_error
        raise
    finally:
        if recovered:
            shutil.rmtree(rescue)
    print('Previous collector code and configuration restored. Dashboard image/database rollback is separate.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('install', 'rollback'))
    parser.add_argument('--calibration-profile', choices=('none', 'local-19v-v1'))
    parser.add_argument('--data-dir', type=Path, help='Existing dashboard data directory; defaults to saved path or source/data')
    parser.add_argument('--source', type=Path, help='Validated local source tree; installation only')
    parser.add_argument('--preserve-host-config', action='store_true',
                        help='Update an installed collector without changing host configuration or data permissions')
    parser.add_argument('--expected-current-sha256',
                        help='Require the unchanged current source identity while holding the transaction lock')
    args = parser.parse_args()
    if os.geteuid() != 0:
        parser.error('Run as root')
    if args.action == 'rollback' and (args.calibration_profile or args.data_dir or args.source or args.preserve_host_config):
        parser.error('Source, calibration, data and preservation options apply to installation only')
    if args.expected_current_sha256 is not None and re.fullmatch(r'[0-9a-f]{64}', args.expected_current_sha256) is None:
        parser.error('--expected-current-sha256 must be 64 lowercase hexadecimal characters')
    try:
        with termination_recovery(), transaction_lock():
            if args.expected_current_sha256 is not None and current_source_sha256() != args.expected_current_sha256:
                raise ValueError('The current collector changed before this transaction; no changes were made.')
            if args.action == 'install':
                install(args.source or Path(__file__).resolve().parents[1], args.calibration_profile,
                        args.data_dir, args.preserve_host_config)
            else:
                rollback()
    except KeyboardInterrupt:
        parser.exit(1, 'Collector transaction interrupted.\n')
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        parser.exit(1, f'{exc}\n')


if __name__ == '__main__':
    main()
