"""Update transactions with temporary files, a fake network and a fake host helper."""
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import shutil
import socket
import tempfile
import threading
import time
from types import SimpleNamespace

import pytest

from ups_panel import updater
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
        'sample': {'timestamp': now}, 'collector': {'build': build}}))


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
        helper=tmp_path / 'trusted' / 'scripts' / 'collector-admin.py', trusted_uid=os.getuid(), socket_gid=os.getgid())
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
