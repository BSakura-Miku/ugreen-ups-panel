"""Update transactions with temporary files, a fake network and a fake host helper."""
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import shutil
import socket
import stat
import tempfile
import threading
import time
from types import SimpleNamespace

import pytest

from ups_panel import updater
from ups_panel.calibration import default_config
from ups_panel.config_target import target_identity
from ups_panel.update_client import UpdateClient, UpdateError, public_status

KEY = 'A' * 43
FILES = ('__init__.py', 'collector.py', 'protocol.py', 'power.py', 'calibration.py', 'usbmon.py', 'build_info.py', 'doctor.py')


def source(directory, version, *, metadata=False):
    package = directory / 'ups_panel'
    package.mkdir(parents=True)
    for name in FILES:
        (package / name).write_text(f"VERSION = '{version}'\n" if name == 'build_info.py' else '# example collector\n')
    digest = hashlib.sha256()
    for path in sorted(package.glob('*.py')):
        digest.update(path.name.encode() + b'\0' + path.read_bytes() + b'\0')
    build = {'version': version, 'revision': 'c' * 40 if metadata else None, 'source_sha256': digest.hexdigest()}
    if metadata:
        (directory / 'collector-release.json').write_text(json.dumps(dict(build, schema=1,
            minimum_updater_schema=1, calibration_schemas=[1, 2])))
    return build


def snapshot(paths, build, *, age=0):
    now = time.time() - age
    paths.snapshot.write_text(json.dumps({'schema': 1, 'source': 'usbmon', 'heartbeat': now,
        'sample': {'timestamp': now, 'calibration_profile': 'none', 'calibration_revision': default_config()['revision']},
        'collector': {'build': build}, 'calibration': {'configurable': True, 'config': default_config(),
        'error': None, 'file_state': 'missing', 'config_target': target_identity(paths.config_env.parent / 'calibration.json')}}))


def mutation(manager, action, payload=None, key=KEY):
    return manager.request({'schema': 1, 'action': action, 'key': key, 'payload': {} if payload is None else payload})


def wait(manager):
    manager.worker.join(timeout=5)
    assert not manager.worker.is_alive()
    return manager.status()


@pytest.fixture
def rig(tmp_path, monkeypatch):
    socket_directory = tempfile.TemporaryDirectory(prefix='ups-ipc-', dir='/tmp')
    Path(socket_directory.name).chmod(0o750)
    os.chown(socket_directory.name, os.getuid(), os.getgid())
    paths = updater.UpdaterPaths(base=tmp_path / 'collector', state=tmp_path / 'state', key=tmp_path / 'key',
        socket=Path(socket_directory.name) / 'control.sock', snapshot=tmp_path / 'snapshot.json',
        helper=tmp_path / 'trusted' / 'scripts' / 'collector-admin.py', config_env=tmp_path / 'collector.env',
        trusted_uid=os.getuid(), socket_gid=os.getgid())
    paths.config_env.write_text(f'UPS_CALIBRATION_CONFIG="{tmp_path}/calibration.json"\n')
    paths.key.write_text(KEY + '\n')
    paths.key.chmod(0o600)
    old = paths.base / 'releases' / 'old'
    old_build = source(old, '0.8.0')
    (paths.base / 'current').symlink_to(old)
    snapshot(paths, old_build)
    bundle = tmp_path / 'downloaded-source'
    new_build = source(bundle, '0.9.0', metadata=True)
    release = SimpleNamespace(version='0.9.0', tag='v0.9.0', release_id=12, sha256='b' * 64,
        size=100, published_at='2026-09-07T00:00:00Z', notes='Collector update',
        release_url='https://github.com/BSakura-Miku/ugreen-ups-panel/releases/tag/v0.9.0')
    client = SimpleNamespace(checks=0, downloads=0)
    def latest():
        client.checks += 1
        return release
    def download(value):
        assert value is release
        client.downloads += 1
        return b'verified package bytes'
    client.latest, client.download = latest, download
    def extract(encoded, destination, **kwargs):
        assert encoded == b'verified package bytes'
        assert kwargs == {'expected_version': '0.9.0', 'updater_schema': 1}
        shutil.copytree(bundle, destination)
        return json.loads((destination / 'collector-release.json').read_text())
    monkeypatch.setattr(updater, 'extract_package', extract)
    state = SimpleNamespace(calls=[], failure=None)
    def helper(paths, action, current_hash, source=None, progress=None):
        state.calls.append(action)
        assert updater.read_build(paths)['source_sha256'] == current_hash
        if state.failure == 'restored':
            snapshot(paths, old_build)
            # A sample produced before helper return cannot confirm restoration;
            # simulate the next report from the recovered collector separately.
            threading.Timer(0.05, lambda: snapshot(paths, old_build)).start()
            return {'returncode': 1, 'outcome': 'restored'}
        if action == 'install':
            previous = (paths.base / 'current').resolve()
            new = paths.base / 'releases' / 'new'
            shutil.copytree(source, new)
            (new / 'rollback').mkdir(mode=0o700)
            (new / 'rollback' / 'state.json').write_text(json.dumps({'preserve_host_config': True, 'current': str(previous)}))
            (paths.base / 'current').unlink()
            (paths.base / 'current').symlink_to(new)
            snapshot(paths, new_build, age=100 if state.failure == 'unhealthy' else 0)
        else:
            (paths.base / 'current').unlink()
            (paths.base / 'current').symlink_to(old)
            snapshot(paths, old_build)
        progress('validating')
        return {'returncode': 0, 'outcome': None}
    manager = updater.UpdateManager(paths, client, helper)
    yield SimpleNamespace(manager=manager, paths=paths, client=client, release=release, old=old, bundle=bundle,
                          old_build=old_build, new_build=new_build, helper_state=state)
    manager.close()
    socket_directory.cleanup()


def checked(rig):
    mutation(rig.manager, 'check')
    status = wait(rig.manager)
    assert status['operation']['outcome'] == 'checked'
    return {key: getattr(rig.release, key) for key in ('version', 'release_id', 'sha256')}


def test_gets_are_read_only_and_never_check_network_or_write_state(rig):
    for _ in range(3):
        status = rig.manager.request({'schema': 1, 'action': 'status'})
    assert status['current'] == rig.old_build
    assert not (rig.paths.state / 'state.json').exists()
    assert rig.client.checks == rig.client.downloads == 0
    assert not rig.helper_state.calls
    assert KEY not in json.dumps(status)


@pytest.mark.parametrize('key', [None, '', 'B' * 43, {'key': KEY}, KEY + '\n'])
def test_invalid_auth_never_starts_network_or_host_work(rig, key):
    with pytest.raises(UpdateError, match='管理密钥'):
        mutation(rig.manager, 'check', key=key)
    assert rig.client.checks == 0 and not rig.helper_state.calls
    assert not (rig.paths.state / 'state.json').exists()


def test_update_then_rollback_uses_only_trusted_helper_and_retains_versions(rig):
    payload = checked(rig)
    mutation(rig.manager, 'install', payload)
    result = wait(rig.manager)
    assert result['operation']['outcome'] == 'updated'
    assert result['current'] == rig.new_build
    assert result['rollback'] == {'available': True, 'version': '0.8.0', 'reason': 'available'}
    assert rig.old.is_dir() and not list(rig.paths.state.glob('stage-*'))
    mutation(rig.manager, 'rollback', {'version': '0.8.0', 'current_version': '0.9.0'})
    result = wait(rig.manager)
    assert result['operation']['outcome'] == 'rolled_back'
    assert result['current'] == rig.old_build
    assert rig.helper_state.calls == ['install', 'rollback']
    assert KEY not in (rig.paths.state / 'state.json').read_text()


@pytest.mark.parametrize('change,code', [({'release_id': True}, 'invalid_request'),
    ({'release_id': 99}, 'stale_release'), ({'sha256': 'c' * 64}, 'stale_release'),
    ({'version': '0.10.0'}, 'stale_release'), ({'version': '../bad'}, 'invalid_request'),
    ({'url': 'https://evil.invalid'}, 'invalid_request')])
def test_forged_or_stale_install_selection_never_downloads_or_installs(rig, change, code):
    payload = checked(rig)
    payload.update(change)
    with pytest.raises(UpdateError) as caught:
        mutation(rig.manager, 'install', payload)
    assert caught.value.code == code
    assert rig.client.downloads == 0 and not rig.helper_state.calls


def test_old_check_and_missing_current_telemetry_do_not_allow_update(rig):
    payload = checked(rig)
    rig.manager.checked_at -= 3601
    with pytest.raises(UpdateError) as error:
        mutation(rig.manager, 'install', payload)
    assert error.value.code == 'check_required'
    rig.manager.checked_at = time.time()
    snapshot(rig.paths, rig.old_build, age=11)
    with pytest.raises(UpdateError) as error:
        mutation(rig.manager, 'install', payload)
    assert error.value.code == 'collector_unavailable'


def test_busy_job_rejects_duplicate_requests_and_status_survives_refresh(rig):
    entered, unblock = threading.Event(), threading.Event()
    latest = rig.client.latest
    def blocked():
        entered.set()
        assert unblock.wait(3)
        return latest()
    rig.client.latest = blocked
    mutation(rig.manager, 'check')
    assert entered.wait(2)
    try:
        assert rig.manager.status()['operation']['busy']
        with pytest.raises(UpdateError) as error:
            mutation(rig.manager, 'check')
        assert error.value.code == 'busy'
        assert rig.client.checks == 0
    finally:
        unblock.set()
    assert wait(rig.manager)['operation']['stage'] == 'succeeded'


def test_manual_collector_change_during_download_is_detected_before_helper(rig):
    payload = checked(rig)
    download = rig.client.download
    def changed(release):
        data = download(release)
        (rig.old / 'ups_panel' / 'collector.py').write_text('# manually changed\n')
        return data
    rig.client.download = changed
    mutation(rig.manager, 'install', payload)
    status = wait(rig.manager)
    assert status['operation']['error']['code'] == 'stale_current'
    assert not rig.helper_state.calls and not list(rig.paths.state.glob('stage-*'))


def test_shutdown_during_download_does_not_begin_a_host_transaction(rig):
    payload = checked(rig)
    download = rig.client.download
    def stop_before_install(release):
        encoded = download(release)
        rig.manager.stopping = True
        return encoded
    rig.client.download = stop_before_install
    mutation(rig.manager, 'install', payload)
    status = wait(rig.manager)
    assert status['operation']['error']['code'] == 'interrupted'
    assert status['current'] == rig.old_build and not rig.helper_state.calls
    assert not list(rig.paths.state.glob('stage-*'))


def test_recovery_waits_for_a_new_sample_instead_of_accepting_cached_data(rig):
    after = time.time()
    assert not updater.fresh_snapshot(rig.paths, rig.old_build, after=after)
    timer = threading.Timer(0.05, lambda: snapshot(rig.paths, rig.old_build))
    timer.start()
    try:
        assert rig.manager._wait_recovered(rig.old_build, after, timeout=1)
    finally:
        timer.join()


@pytest.mark.parametrize('failure,code,outcome', [('restored', 'install_failed', 'restored'),
    ('unhealthy', 'recovery_failed', 'manual_required')])
def test_failed_update_reports_verified_recovery_or_required_attention(rig, failure, code, outcome):
    payload = checked(rig)
    rig.helper_state.failure = failure
    mutation(rig.manager, 'install', payload)
    status = wait(rig.manager)
    assert status['operation']['stage'] == 'failed'
    assert status['operation']['error']['code'] == code
    assert status['operation']['outcome'] == outcome
    assert not list(rig.paths.state.glob('stage-*'))


def test_restart_preserves_result_but_requires_new_release_check(rig):
    checked(rig)
    restarted = updater.UpdateManager(rig.paths, rig.client, rig.manager.helper)
    assert restarted.status()['operation']['outcome'] == 'checked'
    assert restarted.latest is None and restarted.checked_at is None
    assert rig.client.checks == 1


def test_crash_record_does_not_trigger_unattended_commands_on_restart(rig):
    checked(rig)
    rig.manager.operation.update(action='install', stage='installing', busy=True)
    rig.manager._save()
    restarted = updater.UpdateManager(rig.paths, rig.client, rig.manager.helper)
    status = restarted.status()
    assert status['operation']['stage'] == 'interrupted'
    assert status['operation']['busy'] is False
    assert status['operation']['outcome'] == 'manual_required'
    assert not rig.helper_state.calls


def test_legacy_or_outside_release_rollback_is_not_exposed(rig):
    backup = rig.old / 'rollback'
    backup.mkdir()
    state = backup / 'state.json'
    state.write_text(json.dumps({'current': str(rig.bundle)}))
    assert updater.rollback_status(rig.paths)['reason'] == 'legacy_backup'
    state.write_text(json.dumps({'preserve_host_config': True, 'current': str(rig.bundle)}))
    assert updater.rollback_status(rig.paths)['available'] is False


@pytest.mark.parametrize('kind', ['writable_key', 'symlink_key', 'symlink_state'])
def test_unsafe_local_secret_or_state_paths_are_rejected(rig, kind):
    if kind == 'writable_key':
        rig.paths.key.chmod(0o644)
    elif kind == 'symlink_key':
        other = rig.paths.key.with_name('key2')
        rig.paths.key.rename(other)
        rig.paths.key.symlink_to(other)
    else:
        other = rig.paths.state.with_name('state2')
        rig.paths.state.rename(other)
        rig.paths.state.symlink_to(other)
    with pytest.raises((OSError, ValueError)):
        updater.UpdateManager(rig.paths, rig.client, rig.manager.helper)


def simulated_systemd_directory(tmp_path, monkeypatch, *, uid=0, gid=0, mode=0o750):
    """Model root/group identities while keeping chmod on an actual local directory."""
    directory = tmp_path / 'runtime'
    directory.mkdir()
    directory.chmod(mode)
    inode = directory.stat().st_ino
    actual_fstat = os.fstat
    actual_fchmod = os.fchmod
    identity = {'uid': uid, 'gid': gid}
    mutations, descriptors = [], []
    def inspect(fd):
        value = actual_fstat(fd)
        if value.st_ino == inode:
            descriptors.append(fd)
            fields = list(value)
            fields[4], fields[5] = identity['uid'], identity['gid']
            return os.stat_result(fields)
        return value
    def chown(fd, owner, group):
        assert actual_fstat(fd).st_ino == inode
        mutations.append(('chown', owner, group))
        identity.update(uid=owner, gid=group)
    def chmod(fd, value):
        assert actual_fstat(fd).st_ino == inode
        mutations.append(('chmod', value))
        actual_fchmod(fd, value)
    monkeypatch.setattr(updater.os, 'fstat', inspect)
    monkeypatch.setattr(updater.os, 'fchown', chown)
    monkeypatch.setattr(updater.os, 'fchmod', chmod)
    paths = updater.UpdaterPaths(socket=directory / 'control.sock')
    return SimpleNamespace(paths=paths, directory=directory, identity=identity, mutations=mutations,
                           descriptors=descriptors, actual_fstat=actual_fstat)


@pytest.mark.parametrize('initial_gid,mode', [(0, 0o750), (0, 0o700), (10001, 0o750)])
def test_runtime_setup_handles_systemd_root_group_without_group_database(tmp_path, monkeypatch, initial_gid, mode):
    runtime = simulated_systemd_directory(tmp_path, monkeypatch, gid=initial_gid, mode=mode)
    updater.prepare_socket_directory(runtime.paths)
    assert runtime.identity == {'uid': 0, 'gid': 10001}
    assert stat.S_IMODE(runtime.directory.stat().st_mode) == 0o750
    assert runtime.mutations == [('chown', 0, 10001), ('chmod', 0o750)]
    assert not runtime.paths.socket.exists()
    for descriptor in set(runtime.descriptors):
        with pytest.raises(OSError):
            runtime.actual_fstat(descriptor)


@pytest.mark.parametrize('uid,gid,mode', [(1, 0, 0o750), (0, 10002, 0o750),
                                       (0, 0, 0o770), (0, 0, 0o751), (0, 0, 0o755)])
def test_runtime_setup_never_repairs_untrusted_owner_group_or_permissions(tmp_path, monkeypatch, uid, gid, mode):
    runtime = simulated_systemd_directory(tmp_path, monkeypatch, uid=uid, gid=gid, mode=mode)
    with pytest.raises(ValueError, match='socket directory permissions'):
        updater.prepare_socket_directory(runtime.paths)
    assert not runtime.mutations
    assert runtime.identity == {'uid': uid, 'gid': gid}
    assert stat.S_IMODE(runtime.directory.stat().st_mode) == mode
    assert not runtime.paths.socket.exists()
    for descriptor in set(runtime.descriptors):
        with pytest.raises(OSError):
            runtime.actual_fstat(descriptor)


def test_runtime_setup_does_not_follow_parent_symlink_or_modify_its_target(tmp_path, monkeypatch):
    target = tmp_path / 'private-target'
    target.mkdir(mode=0o700)
    (target / 'keep').write_text('unchanged')
    link = tmp_path / 'runtime-link'
    link.symlink_to(target, target_is_directory=True)
    before = target.stat()
    mutations = []
    monkeypatch.setattr(updater.os, 'fchown', lambda *args: mutations.append(args))
    monkeypatch.setattr(updater.os, 'fchmod', lambda *args: mutations.append(args))
    with pytest.raises(OSError):
        updater.prepare_socket_directory(updater.UpdaterPaths(socket=link / 'control.sock'))
    after = target.stat()
    assert (after.st_uid, after.st_gid, after.st_mode) == (before.st_uid, before.st_gid, before.st_mode)
    assert (target / 'keep').read_text() == 'unchanged' and not mutations


def test_runtime_setup_stops_and_closes_descriptor_if_numeric_chown_fails(tmp_path, monkeypatch):
    runtime = simulated_systemd_directory(tmp_path, monkeypatch, mode=0o700)
    monkeypatch.setattr(updater.os, 'fchown', lambda *args: (_ for _ in ()).throw(PermissionError('chown unavailable')))
    with pytest.raises(PermissionError):
        updater.prepare_socket_directory(runtime.paths)
    assert runtime.identity == {'uid': 0, 'gid': 0} and not runtime.mutations
    assert stat.S_IMODE(runtime.directory.stat().st_mode) == 0o700
    for descriptor in set(runtime.descriptors):
        with pytest.raises(OSError):
            runtime.actual_fstat(descriptor)


def test_real_unix_socket_round_trip_permissions_auth_and_bounded_frames(rig):
    with updater.UpdateServer(rig.manager) as server:
        thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': 0.05})
        thread.start()
        try:
            client = UpdateClient(rig.paths.socket)
            assert client.request()['installed'] is True
            assert rig.paths.socket.stat().st_mode & 0o777 == 0o660
            with pytest.raises(UpdateError) as error:
                client.request('check', {}, 'B' * 43)
            assert error.value.code == 'unauthorized'
            client.request('check', {}, KEY)
            wait(rig.manager)
            for data in [b'{' + b'x' * 8193 + b'\n', b'[]\n', b'{"schema":1,"action":"shell"}\n']:
                with socket.socket(socket.AF_UNIX) as connection:
                    connection.settimeout(2)
                    connection.connect(str(rig.paths.socket))
                    connection.sendall(data)
                    response = json.loads(connection.makefile('rb').readline())
                assert response['ok'] is False and KEY not in json.dumps(response)
        finally:
            server.shutdown()
            thread.join()


def test_source_tamper_retains_actual_installed_identity_and_separate_runtime(rig):
    payload = checked(rig)
    (rig.old / 'collector-release.json').write_text(json.dumps(dict(rig.old_build, revision='c' * 40)))
    (rig.old / 'ups_panel/collector.py').write_text('# local patch\n')
    status = rig.manager.status()
    assert status['source_status'] == 'modified'
    assert status['source_error']['code'] == 'source_modified'
    assert status['runtime'] == rig.old_build
    assert status['current']['source_sha256'] != rig.old_build['source_sha256']
    with pytest.raises(UpdateError) as error:
        mutation(rig.manager, 'install', payload)
    assert error.value.code == 'source_modified'
    assert not rig.helper_state.calls and rig.client.downloads == 0


def test_unreadable_source_is_not_replaced_with_runtime_identity(rig):
    (rig.old / 'ups_panel').rename(rig.old / 'missing-package')
    status = rig.manager.status()
    assert status['current'] is None and status['runtime'] == rig.old_build
    assert status['source_status'] == 'unreadable'
    assert status['preflight']['code'] == 'source_unreadable'


def test_old_running_process_is_distinguished_from_modified_installed_source(rig):
    payload = checked(rig)
    value = json.loads(rig.paths.snapshot.read_text())
    value['collector']['build']['version'] = '0.7.0'
    rig.paths.snapshot.write_text(json.dumps(value))
    status = rig.manager.status()
    assert status['source_status'] == 'unverified' and status['source_error'] is None
    assert status['current'] == rig.old_build
    assert status['runtime']['version'] == '0.7.0'
    assert status['preflight']['code'] == 'runtime_mismatch'
    with pytest.raises(UpdateError) as error:
        mutation(rig.manager, 'install', payload)
    assert error.value.code == 'runtime_mismatch'
    assert not rig.helper_state.calls and rig.client.downloads == 0


@pytest.mark.parametrize('fault,code', [('no_path', 'calibration_unconfigured'),
    ('unreadable_parent', 'calibration_unreadable'), ('unknown_profile', 'calibration_incompatible'),
    ('unknown_schema', 'calibration_incompatible'), ('missing_metadata', 'calibration_unconfigured'),
    ('wrong_target', 'calibration_mismatch'), ('wrong_revision', 'calibration_mismatch')])
def test_preflight_blocks_configuration_faults_before_download_or_service_change(rig, fault, code):
    payload = checked(rig)
    value = json.loads(rig.paths.snapshot.read_text())
    if fault == 'no_path': rig.paths.config_env.write_text('UPS_CALIBRATION_PROFILE=none\n')
    elif fault == 'unreadable_parent':
        rig.paths.config_env.write_text(f'UPS_CALIBRATION_CONFIG="{rig.paths.config_env.parent}/absent/calibration.json"\n')
    elif fault in ('unknown_profile', 'unknown_schema'):
        config = default_config()
        config['profile' if fault == 'unknown_profile' else 'schema'] = 'local-12v-v1' if fault == 'unknown_profile' else 99
        (rig.paths.config_env.parent / 'calibration.json').write_text(json.dumps(config))
    elif fault == 'missing_metadata': value.pop('calibration')
    elif fault == 'wrong_target': value['calibration']['config_target']['identity'] = 'a' * 64
    else: value['sample']['calibration_revision'] = 'a' * 64
    rig.paths.snapshot.write_text(json.dumps(value))
    assert rig.manager.status()['preflight']['code'] == code
    with pytest.raises(UpdateError) as error:
        mutation(rig.manager, 'install', payload)
    assert error.value.code == code
    assert not rig.helper_state.calls and rig.client.downloads == 0
    assert str(rig.paths.config_env.parent) not in json.dumps(rig.manager.status())


def test_legacy_metadata_can_upgrade_with_matching_content_and_no_saved_file(rig):
    value = json.loads(rig.paths.snapshot.read_text())
    value['calibration'].pop('config_target')
    value['calibration'].pop('file_state')
    rig.paths.snapshot.write_text(json.dumps(value))
    status = rig.manager.status()
    assert status['source_status'] == 'unverified'
    assert status['preflight'] == {'ready': True, 'code': None, 'target_verified': False}
    mutation(rig.manager, 'install', checked(rig))
    assert wait(rig.manager)['operation']['outcome'] == 'updated'


def test_configuration_change_during_download_is_detected_even_when_new_content_is_valid(rig):
    payload = checked(rig)
    download = rig.client.download
    def changed(release):
        encoded = download(release)
        config = default_config('local-19v-v1')
        (rig.paths.config_env.parent / 'calibration.json').write_text(json.dumps(config))
        value = json.loads(rig.paths.snapshot.read_text())
        value['calibration'].update(config=config, file_state='loaded')
        value['sample'].update(calibration_profile=config['profile'], calibration_revision=config['revision'],
                               calibration_coefficients=config['coefficients'])
        rig.paths.snapshot.write_text(json.dumps(value))
        return encoded
    rig.client.download = changed
    mutation(rig.manager, 'install', payload)
    status = wait(rig.manager)
    assert status['operation']['error']['code'] == 'configuration_changed'
    assert status['current'] == rig.old_build and not rig.helper_state.calls


def test_post_update_configuration_fault_does_not_trigger_blind_rollback_or_claim_history_fault(rig):
    helper = rig.manager.helper
    def changed(*args, **kwargs):
        result = helper(*args, **kwargs)
        value = json.loads(rig.paths.snapshot.read_text())
        value['calibration']['config_target']['identity'] = 'a' * 64
        rig.paths.snapshot.write_text(json.dumps(value))
        return result
    rig.manager.helper = changed
    mutation(rig.manager, 'install', checked(rig))
    status = wait(rig.manager)
    assert status['current'] == rig.new_build
    assert status['operation']['error']['code'] == 'calibration_mismatch'
    assert status['operation']['outcome'] == 'manual_required'
    assert rig.helper_state.calls == ['install']
