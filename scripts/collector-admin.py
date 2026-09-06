#!/usr/bin/env python3
"""Install/restore only this project's passive collector; never manage NUT."""
import argparse
import json
import math
import os
from pathlib import Path
import shutil
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


def command(*args, check=True):
    return subprocess.run(args, check=check, capture_output=True, text=True, timeout=30)


def link_to(target, link):
    temporary = link.with_name(link.name + '.new')
    temporary.unlink(missing_ok=True)
    temporary.symlink_to(target)
    temporary.replace(link)


def save_state(directory):
    directory.mkdir(parents=True, exist_ok=True)
    os.chmod(directory, 0o700)
    state = {'current': os.readlink(BASE / 'current') if (BASE / 'current').is_symlink() else None,
             'previous': os.readlink(BASE / 'previous') if (BASE / 'previous').is_symlink() else None,
             'active': command('systemctl', 'is-active', '--quiet', UNIT, check=False).returncode == 0,
             'enabled': command('systemctl', 'is-enabled', '--quiet', UNIT, check=False).returncode == 0,
             'configs': {}}
    for name, path in CONFIGS.items():
        state['configs'][name] = path.exists()
        if path.exists():
            shutil.copy2(path, directory / name)
    (directory / 'state.json').write_text(json.dumps(state))
    return state


def restore_state(directory):
    state = json.loads((directory / 'state.json').read_text())
    command('systemctl', 'stop', UNIT, check=False)
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


def write_environment(profile):
    path = CONFIGS['env']
    text = path.read_text() if path.exists() else '# Passive UPS collector configuration\nUPS_SERIAL=\nUPS_NUT_TARGET=ups0@localhost\n'
    if profile is not None:
        text = '\n'.join(line for line in text.splitlines() if not line.strip().startswith('UPS_CALIBRATION_PROFILE=')) + '\n'
        text += 'UPS_CALIBRATION_PROFILE=' + profile + '\n'
    elif not any(line.strip().startswith('UPS_CALIBRATION_PROFILE=') for line in text.splitlines()):
        text += '\nUPS_CALIBRATION_PROFILE=none\n'
    path.write_text(text)
    path.chmod(0o600)


def prepare_data_directory(source):
    """Prepare only the bind-mount directory; preserve every existing data file."""
    data = source / 'data'
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


def install(source, profile=None):
    for filename in ('__init__.py', 'collector.py', 'protocol.py', 'power.py', 'usbmon.py'):
        if not (source / 'ups_panel' / filename).is_file():
            raise ValueError(f'Collector source is incomplete: {filename}')
    for filename in ('ugreen-ups-collector.service', 'ugreen-ups-panel.tmpfiles.conf'):
        if not (source / 'deploy' / filename).is_file():
            raise ValueError(f'Deployment source is incomplete: {filename}')
    command('/usr/bin/python3', '-c', 'import ctypes, select, sqlite3; import sys; assert sys.version_info >= (3, 10), "Python 3.10+ required"')
    command('/sbin/modinfo', 'usbmon')
    prepare_data_directory(source)
    (BASE / 'releases').mkdir(parents=True, exist_ok=True)
    release = Path(tempfile.mkdtemp(prefix=time.strftime('%Y%m%d-%H%M%S-'), dir=BASE / 'releases'))
    release.chmod(0o755)
    (release / 'ups_panel').mkdir()
    for path in (source / 'ups_panel').glob('*.py'):
        shutil.copy2(path, release / 'ups_panel' / path.name)
    before = save_state(release / 'rollback')
    marker = BASE / 'module-initial-state'
    if not marker.exists():
        marker.write_text('preexisting\n' if Path('/sys/module/usbmon').exists() else 'absent\n')
    try:
        write_environment(profile)
        for name, filename in (('service', 'ugreen-ups-collector.service'), ('tmpfiles', 'ugreen-ups-panel.tmpfiles.conf')):
            shutil.copyfile(source / 'deploy' / filename, CONFIGS[name])
            CONFIGS[name].chmod(0o644)
        command('systemd-tmpfiles', '--create', str(CONFIGS['tmpfiles']))
        link_to(str(release), BASE / 'current')
        command('systemctl', 'daemon-reload')
        command('systemctl', 'restart', UNIT)
        # Only a report completed after restart qualifies. A last report from
        # the previous process must not make a broken release appear healthy.
        validate_fresh(time.time())
        command('systemctl', 'enable', UNIT)
        if before['current']:
            link_to(before['current'], BASE / 'previous')
    except BaseException as installation_error:
        try:
            restore_state(release / 'rollback')
        except BaseException as restore_error:
            raise RuntimeError(f'Installation and restoration failed. Recovery files retained at {release / "rollback"}: {restore_error}') from installation_error
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
    save_state(rescue)
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
    args = parser.parse_args()
    if os.geteuid() != 0:
        parser.error('Run as root')
    if args.action == 'rollback' and args.calibration_profile:
        parser.error('--calibration-profile applies to installation only')
    try:
        if args.action == 'install':
            install(Path(__file__).resolve().parents[1], args.calibration_profile)
        else:
            rollback()
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        parser.exit(1, f'{exc}\n')


if __name__ == '__main__':
    main()
