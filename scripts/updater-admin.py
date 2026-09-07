#!/usr/bin/env python3
"""Install or remove the optional, local-only collector update service."""
import argparse
import ast
from contextlib import contextmanager
import errno
import fcntl
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import socket
import stat
import subprocess
import tempfile
import time

BASE = Path('/opt/ugreen-ups-updater')
KEY_DIR = Path('/etc/ugreen-ups-updater')
KEY = KEY_DIR / 'key'
STATE = Path('/var/lib/ugreen-ups-updater')
RUNTIME = Path('/run/ugreen-ups-updater')
SOCKET = RUNTIME / 'control.sock'
SERVICE = Path('/etc/systemd/system/ugreen-ups-updater.service')
COLLECTOR_CURRENT = Path('/opt/ugreen-ups-panel/current')
COLLECTOR_LOCK = Path('/run/lock/ugreen-ups-panel-install.lock')
LOCK = Path('/run/lock/ugreen-ups-updater-install.lock')
UNIT = 'ugreen-ups-updater.service'
OWNER_UID = 0
SOCKET_GID = 10001


def command(*args, check=True):
    # A graceful updater stop may finish an already-running collector transaction.
    return subprocess.run(args, check=check, capture_output=True, text=True, timeout=220)


@contextmanager
def file_lock(path, busy_message):
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != OWNER_UID:
            raise RuntimeError('Updater installation lock is not a root-owned regular file.')
        os.fchmod(fd, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError(busy_message) from None
        yield
    finally:
        os.close(fd)


def transaction_lock():
    return file_lock(LOCK, 'Another updater installation or removal is in progress.')


def collector_transaction_lock():
    return file_lock(COLLECTOR_LOCK, 'Another collector installation or rollback is in progress.')


def regular_file(path, *, root_owned=False):
    info = path.lstat()
    if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
            or (root_owned and info.st_uid != OWNER_UID)):
        raise ValueError('Expected a regular, privately managed file.')
    return info


def directory(path):
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != OWNER_UID or info.st_mode & 0o022:
        raise ValueError('Managed updater paths must be root-owned directories, without symlinks.')
    return info


def ensure_directory(path, mode, gid=0):
    path.mkdir(mode=mode, exist_ok=True)
    directory(path)
    os.chown(path, OWNER_UID, gid)
    path.chmod(mode)


def atomic_write(path, data, mode):
    if path.exists() or path.is_symlink():
        regular_file(path, root_owned=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix='.updater-', delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(data)
            os.fchmod(handle.fileno(), mode)
            os.fchown(handle.fileno(), OWNER_UID, 0)
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def link_to(target, link):
    if link.exists() and not link.is_symlink():
        raise ValueError('Updater current must be a managed symbolic link.')
    temporary = link.with_name(link.name + '.new')
    if temporary.exists() or temporary.is_symlink():
        if not temporary.is_symlink():
            raise ValueError('Unexpected updater temporary link path.')
        temporary.unlink()
    temporary.symlink_to(target)
    temporary.replace(link)


def key_bytes():
    info = regular_file(KEY, root_owned=True)
    if stat.S_IMODE(info.st_mode) != 0o600:
        raise ValueError('Existing updater key must have permissions 0600.')
    with KEY.open('rb') as handle:
        data = handle.read(129)
    if re.fullmatch(rb'[A-Za-z0-9_-]{43}\n?', data) is None:
        raise ValueError('Existing updater key has an invalid format; it was not replaced.')
    return data


def collector_permission_snapshot():
    """Inventory only managed code metadata; no source or configuration contents."""
    base = COLLECTOR_CURRENT.parent
    releases = base / 'releases'
    current_info = COLLECTOR_CURRENT.lstat()
    if not stat.S_ISLNK(current_info.st_mode) or current_info.st_uid != OWNER_UID:
        raise ValueError('Collector current must be a root-owned release link.')
    target = COLLECTOR_CURRENT.readlink()
    release = target if target.is_absolute() else base / target
    if release.parent != releases or release.name in ('', '.', '..'):
        raise ValueError('Collector current must select a direct managed release.')
    package = release / 'ups_panel'
    paths = [(path, 'directory') for path in (base, releases, release, package)]
    # Reject linked parents before enumerating their contents.
    for path, _ in paths:
        info = path.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != OWNER_UID:
            raise ValueError('Collector code directories must be root-owned and must not be symlinks.')
    sources = sorted(package.glob('*.py'))
    required = {'__init__.py', 'collector.py', 'protocol.py', 'power.py',
                'calibration.py', 'usbmon.py', 'build_info.py', 'doctor.py'}
    if not 8 <= len(sources) <= 128 or not required <= {path.name for path in sources}:
        raise ValueError('The installed collector source is incomplete.')
    if any(re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*\.py', path.name) is None for path in sources):
        raise ValueError('The installed collector has an unexpected Python filename.')
    paths.extend((path, 'file') for path in sources)
    metadata = release / 'collector-release.json'
    if metadata.exists() or metadata.is_symlink():
        paths.append((metadata, 'file'))
    changes = []
    for path, kind in paths:
        info = path.lstat() if kind == 'directory' else regular_file(path, root_owned=True)
        mode = stat.S_IMODE(info.st_mode)
        if mode & 0o022:
            changes.append({'path': str(path), 'kind': kind, 'mode': mode,
                            'device': info.st_dev, 'inode': info.st_ino,
                            'uid': info.st_uid, 'gid': info.st_gid})
    return changes


def open_collector_path(entry):
    """Open through directory descriptors, without following replaced parent links."""
    base = COLLECTOR_CURRENT.parent
    parts = Path(entry['path']).relative_to(base).parts
    descriptor = os.open(base, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        info = os.fstat(descriptor)
        if info.st_uid != OWNER_UID:
            raise ValueError('Collector code ownership changed during installation.')
        for index, part in enumerate(parts):
            flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
            if index < len(parts) - 1 or entry['kind'] == 'directory':
                flags |= os.O_DIRECTORY
            child = os.open(part, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
            info = os.fstat(descriptor)
            if info.st_uid != OWNER_UID:
                raise ValueError('Collector code ownership changed during installation.')
        result = descriptor
        descriptor = None
        return result
    finally:
        if descriptor is not None:
            os.close(descriptor)


def same_collector_entry(info, entry):
    expected_kind = stat.S_ISDIR if entry['kind'] == 'directory' else stat.S_ISREG
    return (expected_kind(info.st_mode) and (entry['kind'] == 'directory' or info.st_nlink == 1)
            and (info.st_dev, info.st_ino, info.st_uid, info.st_gid)
            == (entry['device'], entry['inode'], entry['uid'], entry['gid']))


def harden_collector_permissions(before):
    for entry in before['collector_permissions']:
        descriptor = open_collector_path(entry)
        try:
            info = os.fstat(descriptor)
            if not same_collector_entry(info, entry) or stat.S_IMODE(info.st_mode) != entry['mode']:
                raise ValueError('Collector code identity changed during installation.')
            os.fchmod(descriptor, entry['mode'] & ~0o022)
            after = os.fstat(descriptor)
            if not same_collector_entry(after, entry) or stat.S_IMODE(after.st_mode) != (entry['mode'] & ~0o022):
                raise ValueError('Collector code permissions could not be tightened without changing identity.')
        finally:
            os.close(descriptor)


def restore_collector_permissions(before):
    for entry in reversed(before['collector_permissions']):
        try:
            descriptor = open_collector_path(entry)
        except (FileNotFoundError, NotADirectoryError, ValueError):
            continue
        except OSError as error:
            if error.errno == errno.ELOOP:
                continue
            raise
        try:
            info = os.fstat(descriptor)
            if same_collector_entry(info, entry) and stat.S_IMODE(info.st_mode) == (entry['mode'] & ~0o022):
                os.fchmod(descriptor, entry['mode'])
        finally:
            os.close(descriptor)


def snapshot_state():
    current = BASE / 'current'
    if (current.exists() or current.is_symlink()) and not current.is_symlink():
        raise ValueError('Updater current must be a managed symbolic link.')
    if current.is_symlink():
        resolved = current.resolve(strict=True)
        if not resolved.is_dir() or not resolved.is_relative_to(BASE / 'releases'):
            raise ValueError('Existing updater release is outside its installation directory.')
    directories = {}
    for path in (BASE, BASE / 'releases', KEY_DIR, STATE, RUNTIME):
        if path.exists() or path.is_symlink():
            info = directory(path)
            directories[str(path)] = {'mode': stat.S_IMODE(info.st_mode), 'uid': info.st_uid, 'gid': info.st_gid}
        else:
            directories[str(path)] = None
    service = None
    if SERVICE.exists() or SERVICE.is_symlink():
        info = regular_file(SERVICE, root_owned=True)
        service = {'data': SERVICE.read_text(), 'mode': stat.S_IMODE(info.st_mode)}
    key = key_bytes().decode('ascii') if KEY.exists() or KEY.is_symlink() else None
    return {'current': str(current.readlink()) if current.is_symlink() else None,
            'service': service, 'key': key, 'directories': directories,
            'collector_permissions': collector_permission_snapshot(),
            'active': command('systemctl', 'is-active', '--quiet', UNIT, check=False).returncode == 0,
            'enabled': command('systemctl', 'is-enabled', '--quiet', UNIT, check=False).returncode == 0}


def remove_socket():
    if SOCKET.exists() or SOCKET.is_symlink():
        if not stat.S_ISSOCK(SOCKET.lstat().st_mode):
            raise ValueError('Refusing to remove an unexpected updater socket path.')
        SOCKET.unlink()


def stop_service():
    command('systemctl', 'stop', UNIT, check=False)
    active = command('systemctl', 'is-active', '--quiet', UNIT, check=False).returncode
    if active == 0:
        raise RuntimeError('Updater is still running; its installation was not removed.')
    if active not in (3, 4):
        raise RuntimeError('Updater stop could not be confirmed; its installation was not removed.')


def validate_service(started, timeout=20):
    """Use only the updater's local, read-only status action; never require UPS data."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if command('systemctl', 'is-active', '--quiet', UNIT, check=False).returncode:
                raise ValueError('Updater is not active')
            info = SOCKET.lstat()
            if (not stat.S_ISSOCK(info.st_mode) or info.st_uid != OWNER_UID
                    or info.st_gid != SOCKET_GID or stat.S_IMODE(info.st_mode) != 0o660
                    or info.st_ctime < started):
                raise ValueError('Updater socket is not ready')
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
                connection.settimeout(3)
                connection.connect(str(SOCKET))
                connection.sendall(b'{"schema":1,"action":"status"}\n')
                data = bytearray()
                while b'\n' not in data:
                    chunk = connection.recv(min(4096, 16385 - len(data)))
                    if not chunk:
                        raise ValueError('Incomplete updater reply')
                    data.extend(chunk)
                    if len(data) > 16384:
                        raise ValueError('Oversized updater reply')
                response = json.loads(bytes(data).split(b'\n', 1)[0])
            status = response.get('status') if isinstance(response, dict) else None
            if (response.get('ok') is True and isinstance(status, dict)
                    and type(status.get('schema')) is int and status['schema'] == 1
                    and status.get('installed') is True):
                return
        except (OSError, ValueError, TypeError, AttributeError, RecursionError):
            pass
        time.sleep(1)
    raise RuntimeError('Updater did not provide a valid local status response within the startup window.')


def restore_state(before):
    stop_service()
    remove_socket()
    if before['service'] is None:
        SERVICE.unlink(missing_ok=True)
    else:
        atomic_write(SERVICE, before['service']['data'].encode(), before['service']['mode'])
    if before['key'] is None:
        KEY.unlink(missing_ok=True)
    else:
        atomic_write(KEY, before['key'].encode('ascii'), 0o600)
    current = BASE / 'current'
    if before['current'] is None:
        current.unlink(missing_ok=True)
    else:
        link_to(before['current'], current)
    command('systemctl', 'daemon-reload')
    command('systemctl', 'enable' if before['enabled'] else 'disable', UNIT, check=before['enabled'])
    if before['active']:
        started = time.time()
        command('systemctl', 'start', UNIT)
        validate_service(started)


def restore_directories(before):
    for name, info in reversed(list(before['directories'].items())):
        path = Path(name)
        if info is not None:
            os.chown(path, info['uid'], info['gid'])
            path.chmod(info['mode'])
        elif path.exists():
            directory(path)
            if path in (STATE, RUNTIME, KEY_DIR):
                shutil.rmtree(path)
            else:
                path.rmdir()


def source_files(source):
    package = source / 'ups_panel'
    if package.is_symlink() or not package.is_dir():
        raise ValueError('Updater source package must be a regular directory.')
    paths = sorted(package.glob('*.py'))
    required = {'__init__.py', 'updater.py', 'build_info.py'}
    if not required <= {path.name for path in paths}:
        raise ValueError('Updater source is incomplete.')
    helper = source / 'scripts' / 'collector-admin.py'
    unit = source / 'deploy' / 'ugreen-ups-updater.service'
    for path in [*paths, helper, unit]:
        if any((source / parent).is_symlink() for parent in path.relative_to(source).parents
               if parent != Path('.')):
            raise ValueError('Updater source parent must not be a symlink.')
        regular_file(path)
        if path.suffix == '.py':
            ast.parse(path.read_bytes(), filename=path.name)
    return paths, helper, unit


def source_license(source):
    path = source / 'LICENSE'
    if not path.exists() and not path.is_symlink():
        return None
    regular_file(path)
    with path.open('rb') as handle:
        data = handle.read(32769)
    if not data or len(data) > 32768:
        raise ValueError('Updater LICENSE is missing or exceeds its size limit.')
    data.decode('utf-8')
    return data


def install(source):
    with collector_transaction_lock():
        return install_locked(source)


def install_locked(source):
    source = Path(source).resolve(strict=True)
    paths, helper, unit = source_files(source)
    license_content = source_license(source)
    if not COLLECTOR_CURRENT.is_dir():
        raise ValueError('Install the host collector before enabling its optional updater.')
    command('/usr/bin/python3', '-c', 'import sys; assert sys.version_info >= (3, 10), "Python 3.10+ required"')
    before = snapshot_state()
    release = None
    backup = None
    service_changed = False
    try:
        ensure_directory(BASE, 0o755)
        ensure_directory(BASE / 'releases', 0o755)
        release = Path(tempfile.mkdtemp(prefix=time.strftime('%Y%m%d-%H%M%S-'), dir=BASE / 'releases'))
        release.chmod(0o755)
        (release / 'ups_panel').mkdir(mode=0o755)
        (release / 'scripts').mkdir(mode=0o755)
        for path in paths:
            shutil.copyfile(path, release / 'ups_panel' / path.name)
            (release / 'ups_panel' / path.name).chmod(0o644)
        shutil.copyfile(helper, release / 'scripts' / 'collector-admin.py')
        (release / 'scripts' / 'collector-admin.py').chmod(0o644)
        if license_content is not None:
            (release / 'LICENSE').write_bytes(license_content)
            (release / 'LICENSE').chmod(0o644)
        backup = release / 'rollback'
        backup.mkdir(mode=0o700)
        atomic_write(backup / 'state.json', json.dumps(before).encode(), 0o600)
        harden_collector_permissions(before)
        ensure_directory(KEY_DIR, 0o700)
        ensure_directory(STATE, 0o700)
        ensure_directory(RUNTIME, 0o750, SOCKET_GID)
        if before['key'] is None:
            atomic_write(KEY, (secrets.token_urlsafe(32) + '\n').encode('ascii'), 0o600)
        atomic_write(SERVICE, unit.read_bytes(), 0o644)
        service_changed = True
        link_to(str(release), BASE / 'current')
        command('systemctl', 'daemon-reload')
        started = time.time()
        command('systemctl', 'restart', UNIT)
        validate_service(started)
        command('systemctl', 'enable', UNIT)
    except BaseException as installation_error:
        try:
            try:
                if service_changed:
                    restore_state(before)
                elif before['key'] is None:
                    KEY.unlink(missing_ok=True)
            finally:
                restore_collector_permissions(before)
            if release is not None:
                shutil.rmtree(release)
            restore_directories(before)
        except BaseException as restore_error:
            location = str(backup) if backup is not None else str(BASE)
            raise RuntimeError(f'Updater installation and recovery failed. Recovery files retained at {location}.') from restore_error
        raise installation_error
    print('Collector update service installed; local status verified.')
    print(f'Management key retained at {KEY}; read locally with: sudo cat {KEY}')


def uninstall():
    # Keep the key and operation history for a later reinstall; never touch the collector.
    if SERVICE.exists() or SERVICE.is_symlink():
        regular_file(SERVICE, root_owned=True)
    stop_service()
    command('systemctl', 'disable', UNIT, check=False)
    SERVICE.unlink(missing_ok=True)
    remove_socket()
    command('systemctl', 'daemon-reload')
    print('Collector update service removed. Updater code, key and state retained; collector and NUT unchanged.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('install', 'uninstall'))
    args = parser.parse_args()
    if os.geteuid() != 0:
        parser.error('Run as root')
    try:
        with transaction_lock():
            if args.action == 'install':
                install(Path(__file__).resolve().parents[1])
            else:
                uninstall()
    except (OSError, ValueError, SyntaxError, RuntimeError, subprocess.SubprocessError) as exc:
        parser.exit(1, f'{exc}\n')


if __name__ == '__main__':
    main()
