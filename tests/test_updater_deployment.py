"""Local installer transactions with fake systemd; no root or host services required."""
from contextlib import contextmanager
import configparser
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]


def load_admin(name):
    spec = importlib.util.spec_from_file_location(name.replace('-', '_'), ROOT / 'scripts' / name)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def fake_systemd(module, monkeypatch, *, active=False, enabled=False):
    state = {'active': active, 'enabled': enabled}
    calls = []
    def command(*args, check=True):
        calls.append(args)
        result = 0
        if args[0] == 'systemctl':
            assert args[1] == 'daemon-reload' or args[-1] == module.UNIT
            action = args[1]
            if action == 'is-active': result = 0 if state['active'] else 3
            elif action == 'is-enabled': result = 0 if state['enabled'] else 1
            elif action in ('start', 'restart'): state['active'] = True
            elif action == 'stop': state['active'] = False
            elif action in ('enable', 'disable'): state['enabled'] = action == 'enable'
            elif action == 'daemon-reload': pass
            else: raise AssertionError(args)
        if check and result:
            raise subprocess.CalledProcessError(result, args)
        return subprocess.CompletedProcess(args, result, '', '')
    monkeypatch.setattr(module, 'command', command)
    module.test_systemd = state
    module.test_calls = calls


@pytest.fixture
def updater(tmp_path, monkeypatch):
    module = load_admin('updater-admin.py')
    values = {
        'BASE': tmp_path / 'opt/updater', 'KEY_DIR': tmp_path / 'etc/updater',
        'KEY': tmp_path / 'etc/updater/key', 'STATE': tmp_path / 'var/lib/updater',
        'RUNTIME': tmp_path / 'run/updater', 'SOCKET': tmp_path / 'run/updater/control.sock',
        'SERVICE': tmp_path / 'etc/systemd/updater.service',
        'COLLECTOR_CURRENT': tmp_path / 'opt/collector/current', 'LOCK': tmp_path / 'lock',
        'COLLECTOR_LOCK': tmp_path / 'collector.lock',
    }
    for name, path in values.items():
        monkeypatch.setattr(module, name, path)
        path.parent.mkdir(parents=True, exist_ok=True)
    release = module.COLLECTOR_CURRENT.parent / 'releases/old'
    (release / 'ups_panel').mkdir(parents=True)
    for name in ('__init__.py', 'collector.py', 'protocol.py', 'power.py', 'calibration.py',
                 'usbmon.py', 'build_info.py', 'doctor.py'):
        (release / 'ups_panel' / name).write_text("VERSION = '0.8.0'\n")
    module.COLLECTOR_CURRENT.symlink_to(release)
    (module.COLLECTOR_CURRENT / 'keep.txt').write_text('unchanged collector')
    monkeypatch.setattr(module, 'OWNER_UID', os.getuid())
    ownership_calls = []
    monkeypatch.setattr(module.os, 'chown', lambda path, uid, gid: ownership_calls.append(('path', str(path), uid, gid)))
    monkeypatch.setattr(module.os, 'fchown', lambda fd, uid, gid: ownership_calls.append(('inode', os.fstat(fd).st_ino, uid, gid)))
    module.test_ownership_calls = ownership_calls
    fake_systemd(module, monkeypatch)
    module.test_validate_service = module.validate_service
    monkeypatch.setattr(module, 'validate_service', lambda started, timeout=20: None)
    source = tmp_path / 'source'
    for directory in ('ups_panel', 'scripts', 'deploy'):
        (source / directory).mkdir(parents=True)
    for filename in ('__init__.py', 'updater.py', 'build_info.py'):
        (source / 'ups_panel' / filename).write_text("VERSION = 'test'\n")
    (source / 'scripts/collector-admin.py').write_text("raise RuntimeError('Do not execute downloaded scripts')\n")
    shutil.copyfile(ROOT / 'deploy/ugreen-ups-updater.service', source / 'deploy/ugreen-ups-updater.service')
    shutil.copyfile(ROOT / 'LICENSE', source / 'LICENSE')
    module.test_source = source
    return module


def updater_state(module):
    return {
        'current': (module.BASE / 'current').readlink(),
        'service': (module.SERVICE.read_bytes(), stat.S_IMODE(module.SERVICE.stat().st_mode)),
        'key': (module.KEY.read_bytes(), stat.S_IMODE(module.KEY.stat().st_mode)),
        'directories': {str(path): stat.S_IMODE(path.stat().st_mode)
                        for path in (module.BASE, module.KEY_DIR, module.STATE, module.RUNTIME)},
        'systemd': dict(module.test_systemd),
    }


def test_bootstrap_installs_independent_code_key_and_restricted_directories(updater, capsys):
    updater.install(updater.test_source)
    release = (updater.BASE / 'current').resolve()
    assert release.parent == updater.BASE / 'releases'
    assert (release / 'scripts/collector-admin.py').read_bytes() == (updater.test_source / 'scripts/collector-admin.py').read_bytes()
    assert (release / 'ups_panel/updater.py').is_file()
    assert (release / 'LICENSE').read_bytes() == (ROOT / 'LICENSE').read_bytes()
    assert stat.S_IMODE((release / 'LICENSE').stat().st_mode) == 0o644
    assert len(updater.KEY.read_text().strip()) == 43
    assert stat.S_IMODE(updater.KEY.stat().st_mode) == 0o600
    assert stat.S_IMODE(updater.KEY_DIR.stat().st_mode) == 0o700
    assert stat.S_IMODE(updater.STATE.stat().st_mode) == 0o700
    assert stat.S_IMODE(updater.RUNTIME.stat().st_mode) == 0o750
    assert stat.S_IMODE((release / 'rollback').stat().st_mode) == 0o700
    assert updater.test_systemd == {'active': True, 'enabled': True}
    output = capsys.readouterr().out
    assert updater.KEY.read_text().strip() not in output
    assert f'sudo cat {updater.KEY}' in output
    assert (updater.COLLECTOR_CURRENT / 'keep.txt').read_text() == 'unchanged collector'
    assert all('ugreen-ups-collector.service' not in call for call in updater.test_calls)
    assert all('modprobe' not in ' '.join(call) for call in updater.test_calls)


def test_reinstall_preserves_key_and_previous_updater_release(updater):
    updater.install(updater.test_source)
    old = (updater.BASE / 'current').resolve()
    key = updater.KEY.read_bytes()
    (updater.STATE / 'operation.json').write_text('{"keep":true}')
    updater.install(updater.test_source)
    assert (updater.BASE / 'current').resolve() != old
    assert old.is_dir()
    assert updater.KEY.read_bytes() == key
    assert (updater.STATE / 'operation.json').read_text() == '{"keep":true}'


def legacy_collector(updater, metadata=True):
    from ups_panel.updater import UpdaterPaths, read_build
    base = updater.COLLECTOR_CURRENT.parent
    release = updater.COLLECTOR_CURRENT.resolve()
    paths = UpdaterPaths(base=base, trusted_uid=updater.OWNER_UID)
    build = read_build(paths)
    if metadata:
        (release / 'collector-release.json').write_text(json.dumps({
            'schema': 1, 'version': build['version'], 'source_sha256': build['source_sha256'],
            'revision': 'a' * 40, 'minimum_updater_schema': 1, 'calibration_schemas': [1, 2]}))
    directories = [base, base / 'releases', release, release / 'ups_panel']
    files = list(sorted((release / 'ups_panel').glob('*.py')))
    if metadata:
        files.append(release / 'collector-release.json')
    for path in directories:
        path.chmod(0o775)
    for path in files:
        path.chmod(0o664)
    private = [base / 'data', release / 'rollback']
    for path in private:
        path.mkdir(mode=0o700)
        (path / 'unchanged.json').write_text('{"keep":true}')
        (path / 'unchanged.json').chmod(0o600)
    private.append(base / 'saved-env')
    private[-1].write_text('unchanged settings')
    private[-1].chmod(0o600)
    return paths, build, directories, files, private


def file_identity(path):
    info = path.stat()
    return (path.read_bytes() if path.is_file() else None, info.st_dev, info.st_ino,
            info.st_uid, info.st_gid, info.st_mtime_ns)


@pytest.mark.parametrize('metadata', [False, True])
def test_bootstrap_tightens_legacy_code_under_shared_lock_without_changing_contents(updater, monkeypatch, metadata):
    from ups_panel.updater import read_build
    paths, old_build, directories, files, private = legacy_collector(updater, metadata)
    before = {path: file_identity(path) for path in directories + files + private}
    private_modes = {path: stat.S_IMODE(path.stat().st_mode) for path in private}
    with pytest.raises(ValueError):
        read_build(paths)
    def validate(started, timeout=20):
        current = read_build(paths)
        assert current['version'] == '0.8.0' and current['source_sha256'] == old_build['source_sha256']
        with pytest.raises(RuntimeError, match='collector installation'):
            with updater.collector_transaction_lock():
                raise AssertionError('Collector lock was not held during bootstrap health check')
    monkeypatch.setattr(updater, 'validate_service', validate)
    updater.install(updater.test_source)
    for path in directories:
        assert stat.S_IMODE(path.stat().st_mode) == 0o755
    for path in files:
        assert stat.S_IMODE(path.stat().st_mode) == 0o644
    for path, identity in before.items():
        assert file_identity(path) == identity
    assert {path: stat.S_IMODE(path.stat().st_mode) for path in private} == private_modes
    saved = json.loads(((updater.BASE / 'current').resolve() / 'rollback/state.json').read_text())
    changes = saved['collector_permissions']
    assert {item['path'] for item in changes} == {str(path) for path in directories + files}
    assert all(set(item) == {'path', 'kind', 'mode', 'device', 'inode', 'uid', 'gid'} for item in changes)
    source_inodes = {path.stat().st_ino for path in directories + files}
    assert not any(call[0] == 'inode' and call[1] in source_inodes for call in updater.test_ownership_calls)
    assert not any(call[0] == 'path' and call[1].startswith(str(paths.base)) for call in updater.test_ownership_calls)
    with updater.collector_transaction_lock():
        pass


def test_failed_bootstrap_restores_original_collector_permissions_and_bytes(updater, monkeypatch):
    _, _, directories, files, private = legacy_collector(updater)
    before = {path: (file_identity(path), stat.S_IMODE(path.stat().st_mode)) for path in directories + files + private}
    monkeypatch.setattr(updater, 'validate_service', lambda *args: (_ for _ in ()).throw(RuntimeError('new updater failed')))
    with pytest.raises(RuntimeError, match='new updater failed'):
        updater.install(updater.test_source)
    assert {path: (file_identity(path), stat.S_IMODE(path.stat().st_mode)) for path in before} == before
    assert not any(call[-1] == 'ugreen-ups-collector.service' for call in updater.test_calls)


def test_partial_permission_failure_restores_already_changed_directories(updater, monkeypatch):
    _, _, directories, files, _ = legacy_collector(updater)
    target_inode = directories[1].stat().st_ino
    original = os.fchmod
    def fail_once(fd, mode):
        if os.fstat(fd).st_ino == target_inode and mode == 0o755:
            raise PermissionError('Cannot tighten release directory')
        original(fd, mode)
    monkeypatch.setattr(updater.os, 'fchmod', fail_once)
    with pytest.raises(PermissionError):
        updater.install(updater.test_source)
    assert all(stat.S_IMODE(path.stat().st_mode) == 0o775 for path in directories)
    assert all(stat.S_IMODE(path.stat().st_mode) == 0o664 for path in files)


def test_permission_recovery_skips_replaced_file_instead_of_chmoding_new_inode(updater, monkeypatch):
    _, _, directories, files, _ = legacy_collector(updater)
    target = files[0]
    old_inode = target.stat().st_ino
    def replace_during_start(started, timeout=20):
        target.rename(target.with_suffix('.retained'))
        target.write_text('new independently replaced file')
        target.chmod(0o600)
        raise RuntimeError('startup failed after replacement')
    monkeypatch.setattr(updater, 'validate_service', replace_during_start)
    with pytest.raises(RuntimeError, match='startup failed'):
        updater.install(updater.test_source)
    assert target.stat().st_ino != old_inode
    assert target.read_text() == 'new independently replaced file'
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert all(stat.S_IMODE(path.stat().st_mode) == 0o775 for path in directories)


@pytest.mark.parametrize('unsafe', ['current_directory', 'outside_release', 'linked_package', 'linked_python', 'hardlinked_python', 'linked_metadata'])
def test_bootstrap_rejects_unsafe_collector_layout_before_permission_changes(updater, tmp_path, unsafe):
    release = updater.COLLECTOR_CURRENT.resolve()
    source = release / 'ups_panel/collector.py'
    if unsafe == 'current_directory':
        updater.COLLECTOR_CURRENT.unlink()
        updater.COLLECTOR_CURRENT.mkdir()
    elif unsafe == 'outside_release':
        updater.COLLECTOR_CURRENT.unlink()
        updater.COLLECTOR_CURRENT.symlink_to(tmp_path)
    elif unsafe == 'linked_package':
        package = release / 'ups_panel'
        package.rename(release / 'original-package')
        package.symlink_to('original-package')
    elif unsafe == 'linked_python':
        source.rename(source.with_suffix('.saved'))
        source.symlink_to(source.with_suffix('.saved'))
    elif unsafe == 'hardlinked_python':
        os.link(source, release / 'second-link')
    else:
        (release / 'collector-release.json').symlink_to(source)
    with pytest.raises(ValueError):
        updater.install(updater.test_source)
    assert not updater.SERVICE.exists()
    assert not any(call[0] == 'systemctl' and call[1] in ('start', 'stop', 'restart') for call in updater.test_calls)


def test_busy_collector_transaction_rejects_bootstrap_before_inspection(updater):
    with updater.collector_transaction_lock():
        with pytest.raises(RuntimeError, match='collector installation'):
            updater.install(updater.test_source)
    assert not updater.test_calls and not updater.BASE.exists()


def test_failed_reinstall_restores_old_service_key_current_and_permissions(updater, monkeypatch):
    updater.install(updater.test_source)
    updater.SERVICE.write_text('old service with local settings\n')
    updater.SERVICE.chmod(0o640)
    updater.RUNTIME.chmod(0o755)
    updater.test_systemd['enabled'] = False
    before = updater_state(updater)
    releases = set((updater.BASE / 'releases').iterdir())
    calls = []
    def validate(started, timeout=20):
        calls.append(started)
        if len(calls) == 1:
            raise RuntimeError('new service failed')
    monkeypatch.setattr(updater, 'validate_service', validate)
    with pytest.raises(RuntimeError, match='new service failed'):
        updater.install(updater.test_source)
    assert updater_state(updater) == before
    assert set((updater.BASE / 'releases').iterdir()) == releases
    assert len(calls) == 2


def test_failed_first_install_removes_only_new_updater_content(updater, monkeypatch):
    # Fixture creation only prepared parents; these are the installer's own directories.
    for path in (updater.RUNTIME, updater.KEY_DIR):
        path.rmdir()
    monkeypatch.setattr(updater, 'validate_service', lambda *args: (_ for _ in ()).throw(RuntimeError('startup failed')))
    with pytest.raises(RuntimeError, match='startup failed'):
        updater.install(updater.test_source)
    for path in (updater.BASE, updater.KEY_DIR, updater.KEY, updater.STATE, updater.RUNTIME, updater.SERVICE):
        assert not path.exists()
    assert updater.test_systemd == {'active': False, 'enabled': False}
    assert (updater.COLLECTOR_CURRENT / 'keep.txt').read_text() == 'unchanged collector'


def test_pre_service_failure_restores_directories_without_stopping_service(updater, monkeypatch):
    original = updater.ensure_directory
    def fail(path, *args):
        if path == updater.STATE:
            raise PermissionError('Cannot create private state')
        return original(path, *args)
    monkeypatch.setattr(updater, 'ensure_directory', fail)
    with pytest.raises(PermissionError):
        updater.install(updater.test_source)
    assert not updater.BASE.exists()
    assert not updater.KEY.exists()
    assert not updater.SERVICE.exists()
    assert not any(call[:2] == ('systemctl', 'stop') for call in updater.test_calls)


def test_failed_recovery_retains_private_backup_without_exposing_key(updater, monkeypatch):
    updater.install(updater.test_source)
    key = updater.KEY.read_text().strip()
    monkeypatch.setattr(updater, 'validate_service', lambda *args: (_ for _ in ()).throw(RuntimeError('startup failed')))
    monkeypatch.setattr(updater, 'stop_service', lambda: (_ for _ in ()).throw(RuntimeError('cannot stop')))
    with pytest.raises(RuntimeError, match='Recovery files retained') as error:
        updater.install(updater.test_source)
    assert key not in str(error.value)
    backup = (updater.BASE / 'current').resolve() / 'rollback'
    assert stat.S_IMODE(backup.stat().st_mode) == 0o700
    assert json.loads((backup / 'state.json').read_text())['key'].strip() == key


def test_uninstall_preserves_key_code_and_state_and_leaves_collector_running(updater):
    updater.install(updater.test_source)
    before_key = updater.KEY.read_bytes()
    current = (updater.BASE / 'current').resolve()
    (updater.STATE / 'keep.json').write_text('{}')
    updater.uninstall()
    assert not updater.SERVICE.exists()
    assert current.is_dir() and updater.KEY.read_bytes() == before_key
    assert (updater.STATE / 'keep.json').read_text() == '{}'
    assert updater.test_systemd == {'active': False, 'enabled': False}
    assert (updater.COLLECTOR_CURRENT / 'keep.txt').read_text() == 'unchanged collector'


@pytest.mark.parametrize('kind', ['permissions', 'invalid', 'symlink', 'hardlink'])
def test_unsafe_existing_key_is_rejected_before_mutating_services(updater, kind):
    updater.KEY.write_text('a' * 43 + '\n')
    updater.KEY.chmod(0o600)
    if kind == 'permissions': updater.KEY.chmod(0o644)
    if kind == 'invalid': updater.KEY.write_text('bad key')
    if kind == 'symlink':
        target = updater.KEY.with_name('original')
        updater.KEY.rename(target)
        updater.KEY.symlink_to(target)
    if kind == 'hardlink': os.link(updater.KEY, updater.KEY.with_name('second-link'))
    with pytest.raises(ValueError):
        updater.install(updater.test_source)
    assert not updater.SERVICE.exists()
    assert not any(call[0] == 'systemctl' for call in updater.test_calls)


def test_source_symlink_cannot_replace_trusted_installer(updater):
    script = updater.test_source / 'scripts/collector-admin.py'
    target = updater.test_source / 'external.py'
    script.rename(target)
    script.symlink_to(target)
    with pytest.raises(ValueError):
        updater.install(updater.test_source)
    assert not updater.test_calls


def test_runtime_symlink_is_not_followed(updater, tmp_path):
    updater.RUNTIME.rmdir()
    unrelated = tmp_path / 'unrelated'
    unrelated.mkdir(mode=0o700)
    updater.RUNTIME.symlink_to(unrelated, target_is_directory=True)
    with pytest.raises(ValueError, match='without symlinks'):
        updater.install(updater.test_source)
    assert stat.S_IMODE(unrelated.stat().st_mode) == 0o700


def test_group_writable_managed_directory_is_rejected(updater):
    updater.KEY_DIR.chmod(0o770)
    with pytest.raises(ValueError, match='root-owned directories'):
        updater.install(updater.test_source)
    assert not updater.SERVICE.exists()


def test_atomic_write_removes_its_temporary_file_if_ownership_fails(updater, monkeypatch):
    monkeypatch.setattr(updater.os, 'fchown', lambda *args: (_ for _ in ()).throw(PermissionError('owner denied')))
    before = set(updater.SERVICE.parent.iterdir())
    with pytest.raises(PermissionError):
        updater.atomic_write(updater.SERVICE, b'test unit', 0o644)
    assert set(updater.SERVICE.parent.iterdir()) == before


def test_uninstall_does_not_change_files_if_service_stop_is_unknown(updater, monkeypatch):
    updater.install(updater.test_source)
    before = updater_state(updater)
    monkeypatch.setattr(updater, 'command', lambda *args, **kwargs: subprocess.CompletedProcess(args, 1, '', ''))
    with pytest.raises(RuntimeError, match='could not be confirmed'):
        updater.uninstall()
    assert updater_state(updater) == before


def test_local_status_health_check_uses_no_key_or_collector_query(updater, monkeypatch):
    with tempfile.TemporaryDirectory(prefix='upsq-', dir='/tmp') as short:
        path = Path(short) / 'control.sock'
        monkeypatch.setattr(updater, 'SOCKET', path)
        monkeypatch.setattr(updater, 'SOCKET_GID', os.getgid())
        updater.test_systemd['active'] = True
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
            listener.bind(str(path))
            path.chmod(0o660)
            monkeypatch.setattr(updater, 'SOCKET_GID', path.stat().st_gid)
            listener.listen(1)
            requests = []
            def reply():
                connection, _ = listener.accept()
                with connection:
                    requests.append(json.loads(connection.recv(8192)))
                    connection.sendall(b'{"ok":true,"status":{"schema":1,"installed":true}}\n')
            thread = threading.Thread(target=reply, daemon=True)
            thread.start()
            updater.test_validate_service(time.time() - 1)
            thread.join(timeout=2)
        assert requests == [{'schema': 1, 'action': 'status'}]


@pytest.mark.parametrize('mode', [0o666, 0o600])
def test_local_status_health_rejects_wrong_socket_permissions(updater, monkeypatch, mode):
    with tempfile.TemporaryDirectory(prefix='upsq-', dir='/tmp') as short:
        path = Path(short) / 'control.sock'
        monkeypatch.setattr(updater, 'SOCKET', path)
        monkeypatch.setattr(updater, 'SOCKET_GID', os.getgid())
        ticks = iter([0, 0, 2])
        monkeypatch.setattr(updater, 'time', SimpleNamespace(monotonic=lambda: next(ticks), sleep=lambda _: None))
        updater.test_systemd['active'] = True
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
            listener.bind(str(path))
            path.chmod(mode)
            with pytest.raises(RuntimeError, match='status response'):
                updater.test_validate_service(0, timeout=1)


def test_updater_transaction_lock_is_exclusive_and_rejects_symlinks(updater):
    with updater.transaction_lock():
        with pytest.raises(RuntimeError, match='in progress'):
            with updater.transaction_lock():
                raise AssertionError('Second transaction entered')
    updater.LOCK.unlink()
    updater.LOCK.symlink_to(updater.test_source / 'ups_panel/updater.py')
    with pytest.raises(OSError):
        with updater.transaction_lock():
            raise AssertionError('Symlink lock entered')


def test_service_keeps_system_configuration_read_only_and_allows_graceful_worker_stop():
    unit = (ROOT / 'deploy/ugreen-ups-updater.service').read_text()
    for expected in ('WorkingDirectory=/opt/ugreen-ups-updater/current',
                     'ExecStart=/usr/bin/python3 -m ups_panel.updater',
                     'RuntimeDirectoryPreserve=yes', 'RuntimeDirectoryMode=0750',
                     'StateDirectoryMode=0700', 'TimeoutStopSec=180', 'KillMode=mixed',
                     'NoNewPrivileges=true', 'ProtectSystem=strict', 'ProtectKernelModules=true',
                     'ReadWritePaths=/opt/ugreen-ups-panel /run/lock'):
        assert expected in unit
    assert 'docker.sock' not in unit and 'ListenStream=' not in unit
    assert 'ExecStartPre=' not in unit
    for script in ('install-updater.sh', 'uninstall-updater.sh'):
        result = subprocess.run(['sh', '-n', str(ROOT / 'scripts' / script)], capture_output=True, text=True)
        assert result.returncode == 0, result.stderr
    result = subprocess.run([sys.executable, '-S', str(ROOT / 'scripts/updater-admin.py'), '--help'], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_systemd_uses_existing_root_group_and_prepares_socket_inside_main_process():
    from ups_panel.updater import UpdaterPaths
    admin = load_admin('updater-admin.py')
    unit = configparser.ConfigParser(interpolation=None)
    unit.read(ROOT / 'deploy/ugreen-ups-updater.service')
    service = unit['Service']
    assert service['User'] == 'root'
    assert service['Group'] == 'root'
    assert admin.SOCKET_GID == UpdaterPaths().socket_gid == 10001
    assert service['RuntimeDirectory'] == admin.RUNTIME.name == UpdaterPaths().socket.parent.name
    assert int(service['RuntimeDirectoryMode'], 8) == 0o750
    assert service['RuntimeDirectoryPreserve'] == 'yes'
    assert 'ExecStartPre' not in service
    # The socket group is assigned numerically inside ExecStart; systemd needs
    # only the existing root group and keeps persistent state root:root.
    assert service['StateDirectory'] == admin.STATE.name
    assert int(service['StateDirectoryMode'], 8) == 0o700
    assert int(service['UMask'], 8) == 0o077


@pytest.fixture
def collector(tmp_path, monkeypatch):
    module = load_admin('collector-admin.py')
    base = tmp_path / 'collector'
    old = base / 'releases/old'
    (old / 'ups_panel').mkdir(parents=True)
    (old / 'ups_panel/__init__.py').write_text("VERSION = 'old'\n")
    (old / 'ups_panel/collector.py').write_text("CODE = 'old'\n")
    (base / 'current').symlink_to(old)
    configs = {name: tmp_path / name for name in ('service', 'tmpfiles', 'env')}
    for name, path in configs.items():
        path.write_text('preserve ' + name)
        path.chmod(0o640)
    monkeypatch.setattr(module, 'BASE', base)
    monkeypatch.setattr(module, 'CONFIGS', configs)
    monkeypatch.setattr(module, 'LOCK', tmp_path / 'collector.lock')
    fake_systemd(module, monkeypatch, active=True, enabled=False)
    validations = []
    monkeypatch.setattr(module, 'validate_fresh', lambda started, timeout=20: validations.append((started, timeout)))
    source = tmp_path / 'stage'
    (source / 'ups_panel').mkdir(parents=True)
    for name in ('__init__.py', 'collector.py', 'protocol.py', 'power.py', 'calibration.py', 'usbmon.py', 'build_info.py', 'doctor.py'):
        (source / 'ups_panel' / name).write_text("VERSION = 'new'\n")
    metadata = {'schema': 1, 'version': '0.9.0', 'revision': 'a' * 40,
                'source_sha256': 'b' * 64, 'minimum_updater_schema': 1, 'calibration_schemas': [1, 2]}
    (source / 'collector-release.json').write_text(json.dumps(metadata))
    shutil.copyfile(ROOT / 'LICENSE', source / 'LICENSE')
    module.test_source = source
    module.test_old = old
    module.test_metadata = metadata
    module.test_validations = validations
    return module


def collector_configuration(collector):
    return {name: (path.read_bytes(), path.stat().st_mode, path.stat().st_mtime_ns)
            for name, path in collector.CONFIGS.items()}


def test_preserve_update_copies_identity_and_code_without_host_configuration_changes(collector):
    before = collector_configuration(collector)
    collector.install(collector.test_source, preserve_host_config=True)
    release = (collector.BASE / 'current').resolve()
    assert json.loads((release / 'collector-release.json').read_text()) == collector.test_metadata
    assert (release / 'LICENSE').read_bytes() == (ROOT / 'LICENSE').read_bytes()
    assert stat.S_IMODE((release / 'LICENSE').stat().st_mode) == 0o644
    backup = json.loads((release / 'rollback/state.json').read_text())
    assert backup['preserve_host_config'] is True and backup['configs'] == {}
    assert collector_configuration(collector) == before
    assert not (collector.test_source / 'data').exists()
    assert not (collector.BASE / 'module-initial-state').exists()
    assert collector.test_systemd == {'active': True, 'enabled': False}
    assert len(collector.test_validations) == 2
    assert collector.test_validations[0][1] == 1
    forbidden = ('daemon-reload', 'enable', 'disable')
    assert not any(call[0] == 'systemctl' and call[1] in forbidden for call in collector.test_calls)
    assert not any(call[0] in ('/sbin/modinfo', 'systemd-tmpfiles') for call in collector.test_calls)


@pytest.mark.parametrize('preserve', [False, True])
def test_private_service_umask_keeps_collector_code_readable_and_private_state_restricted(collector, monkeypatch, preserve):
    before = collector_configuration(collector)
    data = collector.test_source / 'data'
    data.mkdir(mode=0o750)
    data.chmod(0o750)
    (data / 'calibration.json').write_text('{"retain":true}')
    (data / 'calibration.json').chmod(0o640)
    data_before = (stat.S_IMODE(data.stat().st_mode), (data / 'calibration.json').read_bytes(),
                   stat.S_IMODE((data / 'calibration.json').stat().st_mode))
    for path in (collector.test_source / 'ups_panel').glob('*.py'):
        path.chmod(0o600)
    if not preserve:
        (collector.test_source / 'deploy').mkdir()
        (collector.test_source / 'deploy/ugreen-ups-collector.service').write_text('installed service')
        (collector.test_source / 'deploy/ugreen-ups-panel.tmpfiles.conf').write_text('installed tmpfiles')
        monkeypatch.setattr(collector.os, 'fchown', lambda *args: None)
    previous_umask = os.umask(0o077)
    try:
        collector.install(collector.test_source, preserve_host_config=preserve)
    finally:
        os.umask(previous_umask)
    release = (collector.BASE / 'current').resolve()
    assert stat.S_IMODE(release.stat().st_mode) == 0o755
    assert stat.S_IMODE((release / 'ups_panel').stat().st_mode) == 0o755
    assert all(stat.S_IMODE(path.stat().st_mode) == 0o644 for path in (release / 'ups_panel').glob('*.py'))
    assert stat.S_IMODE((release / 'collector-release.json').stat().st_mode) == 0o644
    assert stat.S_IMODE((release / 'LICENSE').stat().st_mode) == 0o644
    assert stat.S_IMODE((release / 'rollback').stat().st_mode) == 0o700
    assert (stat.S_IMODE(data.stat().st_mode), (data / 'calibration.json').read_bytes(),
            stat.S_IMODE((data / 'calibration.json').stat().st_mode)) == data_before
    if preserve:
        assert collector_configuration(collector) == before
    else:
        # Traditional installation still applies its existing private environment policy.
        assert stat.S_IMODE(collector.CONFIGS['env'].stat().st_mode) == 0o600


def test_preserve_rollback_does_not_restore_or_rewrite_host_configuration(collector):
    collector.install(collector.test_source, preserve_host_config=True)
    collector.CONFIGS['env'].write_text('later user configuration')
    before = collector_configuration(collector)
    collector.rollback()
    assert (collector.BASE / 'current').resolve() == collector.test_old
    assert collector_configuration(collector) == before
    assert collector.test_systemd == {'active': True, 'enabled': False}


@pytest.mark.parametrize('error', [RuntimeError('no fresh sample'), KeyboardInterrupt()])
def test_preserve_update_recovers_original_release_on_failure_or_interruption(collector, monkeypatch, capsys, error):
    before = collector_configuration(collector)
    calls = []
    def validation(started, timeout=20):
        calls.append(started)
        if len(calls) == 2:
            raise error
    monkeypatch.setattr(collector, 'validate_fresh', validation)
    with pytest.raises(type(error)):
        collector.install(collector.test_source, preserve_host_config=True)
    assert (collector.BASE / 'current').resolve() == collector.test_old
    assert collector_configuration(collector) == before
    assert collector.test_systemd == {'active': True, 'enabled': False}
    assert '{"outcome":"restored"}' in capsys.readouterr().out


def test_preserve_update_requires_active_collector_and_fresh_precheck(collector, monkeypatch):
    collector.test_systemd['active'] = False
    with pytest.raises(ValueError, match='active collector'):
        collector.install(collector.test_source, preserve_host_config=True)
    assert len(list((collector.BASE / 'releases').iterdir())) == 1
    collector.test_systemd['active'] = True
    monkeypatch.setattr(collector, 'validate_fresh', lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError('stale before update')))
    with pytest.raises(RuntimeError, match='stale before'):
        collector.install(collector.test_source, preserve_host_config=True)
    assert len(list((collector.BASE / 'releases').iterdir())) == 1
    assert not any(call[:2] == ('systemctl', 'restart') for call in collector.test_calls)


def test_preserve_recovery_reports_failure_when_process_stop_cannot_be_confirmed(collector, monkeypatch, capsys):
    original = collector.command
    def command(*args, check=True):
        if args[:2] == ('systemctl', 'stop'):
            return subprocess.CompletedProcess(args, 1, '', '')
        return original(*args, check=check)
    monkeypatch.setattr(collector, 'command', command)
    calls = []
    def validation(started, timeout=20):
        calls.append(started)
        if len(calls) == 2:
            raise RuntimeError('new version failed')
    monkeypatch.setattr(collector, 'validate_fresh', validation)
    with pytest.raises(RuntimeError, match='restoration failed'):
        collector.install(collector.test_source, preserve_host_config=True)
    assert '{"outcome":"recovery_failed"}' in capsys.readouterr().out


@pytest.mark.parametrize('patch', [{'schema': True}, {'revision': 'bad'}, {'version': '0.9.0;command'},
                                  {'source_sha256': None}, {'calibration_schemas': [True, 2]},
                                  {'minimum_updater_schema': 2}, {'unexpected': 'field'}])
def test_metadata_rejected_before_service_changes(collector, patch):
    metadata = {**collector.test_metadata, **patch}
    (collector.test_source / 'collector-release.json').write_text(json.dumps(metadata))
    with pytest.raises(ValueError, match='identity fields'):
        collector.install(collector.test_source, preserve_host_config=True)
    assert not collector.test_calls


def test_metadata_symlink_is_rejected(collector):
    path = collector.test_source / 'collector-release.json'
    path.rename(path.with_name('other.json'))
    path.symlink_to('other.json')
    with pytest.raises(ValueError, match='regular file'):
        collector.install(collector.test_source, preserve_host_config=True)
    assert not collector.test_calls


@pytest.mark.parametrize('invalid', ['missing', 'symlink', 'too_large', 'invalid_utf8'])
def test_metadata_install_requires_a_safe_bounded_license(collector, invalid):
    license = collector.test_source / 'LICENSE'
    if invalid == 'missing': license.unlink()
    elif invalid == 'symlink':
        license.unlink()
        license.symlink_to(ROOT / 'LICENSE')
    elif invalid == 'too_large': license.write_bytes(b'x' * 32769)
    else: license.write_bytes(b'\xff')
    with pytest.raises(ValueError):
        collector.install(collector.test_source, preserve_host_config=True)
    assert not collector.test_calls


def test_updater_license_is_optional_but_existing_file_must_be_safe(updater):
    license = updater.test_source / 'LICENSE'
    license.unlink()
    updater.install(updater.test_source)
    assert not ((updater.BASE / 'current').resolve() / 'LICENSE').exists()
    license.symlink_to(ROOT / 'LICENSE')
    with pytest.raises(ValueError):
        updater.install(updater.test_source)


def test_current_hash_matches_build_info_byte_algorithm(collector):
    digest = hashlib.sha256()
    for path in sorted((collector.test_old / 'ups_panel').glob('*.py')):
        digest.update(path.name.encode() + b'\0' + path.read_bytes() + b'\0')
    assert collector.current_source_sha256() == digest.hexdigest()


@pytest.mark.parametrize('action', ['install', 'rollback'])
def test_expected_current_hash_is_checked_inside_lock_before_any_mutation(collector, monkeypatch, action):
    order = []
    @contextmanager
    def lock():
        order.append('lock')
        yield
        order.append('unlock')
    monkeypatch.setattr(collector, 'transaction_lock', lock)
    monkeypatch.setattr(collector.os, 'geteuid', lambda: 0)
    monkeypatch.setattr(collector, 'current_source_sha256', lambda: order.append('hash') or 'a' * 64)
    monkeypatch.setattr(collector, 'install', lambda *args: order.append('install'))
    monkeypatch.setattr(collector, 'rollback', lambda: order.append('rollback'))
    monkeypatch.setattr(sys, 'argv', ['collector-admin.py', action, '--expected-current-sha256', 'b' * 64])
    with pytest.raises(SystemExit) as error:
        collector.main()
    assert error.value.code == 1
    assert order == ['lock', 'hash']
    monkeypatch.setattr(sys, 'argv', ['collector-admin.py', action, '--expected-current-sha256', 'a' * 64])
    collector.main()
    assert order[-4:] == ['lock', 'hash', action, 'unlock']


def test_sigterm_raises_into_transaction_recovery_and_restores_handler(collector):
    previous = signal.getsignal(signal.SIGTERM)
    with pytest.raises(KeyboardInterrupt):
        with collector.termination_recovery():
            signal.raise_signal(signal.SIGTERM)
    assert signal.getsignal(signal.SIGTERM) == previous
