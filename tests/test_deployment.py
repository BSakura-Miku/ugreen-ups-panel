"""Installer transactions against a fake systemd; never require root or touch services."""
import importlib.util
import json
from pathlib import Path
import subprocess

import pytest


@pytest.fixture
def admin(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location('collector_admin_test',
        Path(__file__).parents[1] / 'scripts' / 'collector-admin.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    base = tmp_path / 'opt' / 'ugreen-ups-panel'
    base.mkdir(parents=True)
    configs = {name: tmp_path / 'etc' / filename for name, filename in
        [('service', 'collector.service'), ('tmpfiles', 'collector.tmpfiles'), ('env', 'collector.env')]}
    (tmp_path / 'etc').mkdir()
    monkeypatch.setattr(module, 'BASE', base)
    monkeypatch.setattr(module, 'CONFIGS', configs)
    monkeypatch.setattr(module, 'SNAPSHOT', tmp_path / 'latest.json')
    calls = []
    ownership_calls = []
    systemd = {'active': True, 'enabled': True}

    def command(*args, check=True):
        calls.append(args)
        code = 0
        if args[0] == 'systemctl':
            assert args[1] == 'daemon-reload' or args[-1] == module.UNIT
            action = args[1]
            if action == 'is-active': code = 0 if systemd['active'] else 3
            elif action == 'is-enabled': code = 0 if systemd['enabled'] else 1
            elif action in ('start', 'restart'): systemd['active'] = True
            elif action == 'stop': systemd['active'] = False
            elif action in ('enable', 'disable'): systemd['enabled'] = action == 'enable'
        result = subprocess.CompletedProcess(args, code, '', '')
        if check and code:
            raise subprocess.CalledProcessError(code, args)
        return result

    monkeypatch.setattr(module, 'command', command)
    def set_directory_owner(fd, uid, gid):
        assert module.os.fstat(fd).st_ino == module.test_expected_data.stat().st_ino
        ownership_calls.append((uid, gid))
    monkeypatch.setattr(module.os, 'fchown', set_directory_owner)
    module.test_validate_fresh = module.validate_fresh
    monkeypatch.setattr(module, 'validate_fresh', lambda started: None)
    source = tmp_path / 'source'
    (source / 'ups_panel').mkdir(parents=True)
    for filename in ('__init__.py', 'collector.py', 'protocol.py', 'power.py', 'calibration.py', 'usbmon.py', 'build_info.py', 'doctor.py'):
        (source / 'ups_panel' / filename).write_text("VERSION = 'new'\n")
    (source / 'deploy').mkdir()
    for name, filename in [('service', 'ugreen-ups-collector.service'),
                           ('tmpfiles', 'ugreen-ups-panel.tmpfiles.conf')]:
        (source / 'deploy' / filename).write_text('new ' + name + '\n')
    old = base / 'releases' / 'old'
    old.mkdir(parents=True)
    older = base / 'releases' / 'older'
    older.mkdir()
    (base / 'current').symlink_to('releases/old')
    (base / 'previous').symlink_to('releases/older')
    configs['service'].write_text('old service\n')
    configs['tmpfiles'].write_text('old tmpfiles\n')
    configs['env'].write_text('UPS_NUT_TARGET=office@localhost\nUPS_CALIBRATION_PROFILE=local-19v-v1\n')
    configs['env'].chmod(0o640)
    module.test_source = source
    module.test_expected_data = source / 'data'
    module.test_systemd = systemd
    module.test_calls = calls
    module.test_ownership_calls = ownership_calls
    return module


def state(admin):
    return {
        'current': (admin.BASE / 'current').readlink(),
        'previous': (admin.BASE / 'previous').readlink(),
        'configs': {name: (path.read_bytes(), path.stat().st_mode & 0o777)
                    for name, path in admin.CONFIGS.items() if path.exists()},
        'active': admin.test_systemd['active'],
        'enabled': admin.test_systemd['enabled'],
    }


def test_failed_install_restores_code_service_environment_and_service_state(admin, monkeypatch):
    admin.test_systemd['enabled'] = False
    before = state(admin)

    def fail_validation(started):
        assert (admin.BASE / 'current').readlink() != before['current']
        assert admin.CONFIGS['service'].read_text() == 'new service\n'
        assert 'UPS_CALIBRATION_PROFILE=none' in admin.CONFIGS['env'].read_text()
        raise RuntimeError('new collector did not produce telemetry')

    monkeypatch.setattr(admin, 'validate_fresh', fail_validation)
    with pytest.raises(RuntimeError, match='did not produce'):
        admin.install(admin.test_source, 'none')
    assert state(admin) == before
    assert list((admin.BASE / 'releases').glob('*/rollback/state.json'))


def test_unspecified_profile_preserves_existing_environment(admin):
    original = admin.CONFIGS['env'].read_text()
    old = (admin.BASE / 'current').readlink()
    admin.install(admin.test_source)
    assert admin.CONFIGS['env'].read_text().startswith(original)
    assert f'UPS_CALIBRATION_CONFIG="{admin.test_source}/data/calibration.json"' in admin.CONFIGS['env'].read_text()
    assert (admin.BASE / 'previous').readlink() == old
    assert (admin.BASE / 'current').resolve().name != 'old'
    assert admin.test_systemd == {'active': True, 'enabled': True}
    assert admin.CONFIGS['env'].stat().st_mode & 0o777 == 0o600


def test_failed_rollback_recovers_current_release_and_configs(admin, monkeypatch):
    admin.install(admin.test_source, 'none')
    before = state(admin)

    def reject_old(started):
        assert (admin.BASE / 'current').readlink() == Path('releases/old')
        assert 'UPS_CALIBRATION_PROFILE=local-19v-v1' in admin.CONFIGS['env'].read_text()
        raise RuntimeError('previous release is unhealthy')

    monkeypatch.setattr(admin, 'validate_fresh', reject_old)
    with pytest.raises(RuntimeError, match='unhealthy'):
        admin.rollback()
    assert state(admin) == before
    assert not list(admin.BASE.glob('rollback-rescue-*'))


def test_successful_rollback_supports_relative_symlinks(admin):
    before = state(admin)
    admin.install(admin.test_source, 'none')
    admin.rollback()
    assert state(admin) == before


def test_failed_recovery_retains_rescue_configuration(admin, monkeypatch):
    admin.install(admin.test_source, 'none')
    current = (admin.BASE / 'current').readlink()
    original_restore = admin.restore_state

    def restore(directory):
        if directory.name.startswith('rollback-rescue-'):
            raise OSError('system configuration is read-only')
        return original_restore(directory)

    monkeypatch.setattr(admin, 'restore_state', restore)
    monkeypatch.setattr(admin, 'validate_fresh', lambda started: (_ for _ in ()).throw(RuntimeError('bad release')))
    with pytest.raises(RuntimeError, match='Recovery files retained'):
        admin.rollback()
    rescues = list(admin.BASE.glob('rollback-rescue-*'))
    assert len(rescues) == 1
    saved = json.loads((rescues[0] / 'state.json').read_text())
    assert saved['current'] == str(current)
    assert 'UPS_CALIBRATION_PROFILE=none' in (rescues[0] / 'env').read_text()


def test_fresh_install_failure_removes_new_configs_and_keeps_service_stopped(admin, monkeypatch):
    for name in ('current', 'previous'):
        (admin.BASE / name).unlink()
    for path in admin.CONFIGS.values():
        path.unlink()
    admin.test_systemd.update(active=False, enabled=False)
    monkeypatch.setattr(admin, 'validate_fresh', lambda started: (_ for _ in ()).throw(RuntimeError('no UPS')))
    with pytest.raises(RuntimeError):
        admin.install(admin.test_source)
    assert not (admin.BASE / 'current').exists()
    assert not any(path.exists() for path in admin.CONFIGS.values())
    assert admin.test_systemd == {'active': False, 'enabled': False}


@pytest.mark.parametrize('change', [{'source': 'replay'}, {'heartbeat': 98}, {'sample': {'timestamp': 98}},
                                  {'heartbeat': float('nan')}, {'sample': {'timestamp': True}}])
def test_installer_health_check_rejects_replay_or_pre_restart_data(admin, monkeypatch, change):
    payload = {'schema': 1, 'source': 'usbmon', 'heartbeat': 100, 'sample': {'timestamp': 100}}
    payload.update(change)
    admin.SNAPSHOT.write_text(json.dumps(payload))
    ticks = iter([0, 0, 2])
    from types import SimpleNamespace
    monkeypatch.setattr(admin, 'time', SimpleNamespace(monotonic=lambda: next(ticks), time=lambda: 100, sleep=lambda _: None))
    with pytest.raises(RuntimeError, match='No fresh UPS telemetry'):
        admin.test_validate_fresh(99, timeout=1)


def test_install_prepares_container_data_directory_without_manual_setup(admin):
    data = admin.test_source / 'data'
    assert not data.exists()
    admin.install(admin.test_source)
    assert data.is_dir() and data.stat().st_mode & 0o7777 == 0o750
    assert admin.test_ownership_calls == [(10001, 10001)]
    assert not list(data.iterdir())


def test_repeated_install_preserves_existing_history_files_and_subdirectory_permissions(admin):
    data = admin.test_source / 'data'
    data.mkdir(mode=0o700)
    database = data / 'history.sqlite'
    database.write_bytes(b'existing history must remain unchanged')
    database.chmod(0o600)
    nested = data / 'other'
    nested.mkdir(mode=0o700)
    (nested / 'keep.txt').write_text('keep')
    before = (database.read_bytes(), database.stat().st_mode, database.stat().st_uid,
              database.stat().st_gid, database.stat().st_mtime_ns, nested.stat().st_mode)
    admin.install(admin.test_source)
    admin.install(admin.test_source)
    assert (database.read_bytes(), database.stat().st_mode, database.stat().st_uid,
            database.stat().st_gid, database.stat().st_mtime_ns, nested.stat().st_mode) == before
    assert (nested / 'keep.txt').read_text() == 'keep'
    assert all(pair == (10001, 10001) for pair in admin.test_ownership_calls)
    assert data.stat().st_mode & 0o7777 == 0o750


@pytest.mark.parametrize('kind', ['file', 'symlink', 'dangling_symlink'])
def test_invalid_data_path_is_rejected_before_existing_service_changes(admin, kind):
    data = admin.test_source / 'data'
    target = admin.test_source / 'untouched'
    if kind == 'file':
        data.write_text('retain file')
    elif kind == 'symlink':
        target.mkdir(mode=0o700)
        data.symlink_to(target, target_is_directory=True)
    else:
        data.symlink_to(target)
    before = state(admin)
    with pytest.raises(ValueError, match='Project data must be a directory'):
        admin.install(admin.test_source)
    assert state(admin) == before
    assert not admin.test_ownership_calls
    assert not any(call[0] == 'systemctl' for call in admin.test_calls)
    if kind == 'file': assert data.read_text() == 'retain file'
    if kind == 'symlink': assert target.stat().st_mode & 0o7777 == 0o700


def test_data_ownership_failure_leaves_existing_collector_untouched(admin, monkeypatch):
    def denied(*args):
        raise PermissionError('Cannot prepare container data directory')
    monkeypatch.setattr(admin.os, 'fchown', denied)
    before = state(admin)
    with pytest.raises(PermissionError):
        admin.install(admin.test_source)
    assert state(admin) == before
    assert not any(call[0] == 'systemctl' for call in admin.test_calls)


def test_temporary_source_upgrade_uses_explicit_data_and_preserves_it_for_next_install(admin):
    data = admin.test_source.parent / 'existing deployment' / 'data'
    data.mkdir(parents=True)
    (data / 'history.sqlite').write_bytes(b'keep history')
    (data / 'calibration.json').write_text('{"preserve": true}')
    admin.test_expected_data = data
    admin.install(admin.test_source, data_dir=data)
    assert not (admin.test_source / 'data').exists()
    assert json.dumps(str(data / 'calibration.json')) in admin.CONFIGS['env'].read_text()
    admin.install(admin.test_source)
    assert (data / 'history.sqlite').read_bytes() == b'keep history'
    assert (data / 'calibration.json').read_text() == '{"preserve": true}'
    assert not (admin.test_source / 'data').exists()


def test_relative_data_directory_is_rejected_before_service_changes(admin):
    before = state(admin)
    with pytest.raises(ValueError, match='absolute path'):
        admin.install(admin.test_source, data_dir=Path('relative/data'))
    assert state(admin) == before
    assert not any(call[0] == 'systemctl' for call in admin.test_calls)
