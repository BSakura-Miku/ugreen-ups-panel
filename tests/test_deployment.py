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
    module.test_validate_fresh = module.validate_fresh
    monkeypatch.setattr(module, 'validate_fresh', lambda started: None)
    source = tmp_path / 'source'
    (source / 'ups_panel').mkdir(parents=True)
    for filename in ('__init__.py', 'collector.py', 'protocol.py', 'power.py', 'usbmon.py'):
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
    module.test_systemd = systemd
    module.test_calls = calls
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
    assert admin.CONFIGS['env'].read_text() == original
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
