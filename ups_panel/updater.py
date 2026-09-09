"""Optional host service for authenticated, explicit collector updates only."""
import argparse
import ast
from collections import deque
from dataclasses import dataclass
import hashlib
import hmac
import json
import os
from pathlib import Path
import selectors
import shutil
import signal
import socket
import socketserver
import stat
import subprocess
import tempfile
import threading
import time
import uuid

from .build_info import VERSION
from .collector_health import calibration_readiness, environment_config
from .update_client import (SCHEMA, SOCKET, MAX_REQUEST, MAX_RESPONSE, KEY_PATTERN, UpdateError,
                            build_value, hex_value, public_status, unavailable, version, version_tuple)
from .update_release import ReleaseClient, ReleaseError, extract_package


@dataclass(frozen=True)
class UpdaterPaths:
    base: Path = Path('/opt/ugreen-ups-panel')
    state: Path = Path('/var/lib/ugreen-ups-updater')
    key: Path = Path('/etc/ugreen-ups-updater/key')
    socket: Path = Path(SOCKET)
    snapshot: Path = Path('/run/ugreen-ups-panel/latest.json')
    config_env: Path = Path('/etc/ugreen-ups-panel.env')
    helper: Path = Path(__file__).resolve().parents[1] / 'scripts' / 'collector-admin.py'
    trusted_uid: int = 0
    socket_gid: int = 10001


def read_regular(path, maximum, uid=None):
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_size > maximum
                or (uid is not None and (info.st_uid != uid or info.st_mode & 0o022))):
            raise ValueError('Invalid local file')
        value = stream.read(maximum + 1)
        if len(value) > maximum:
            raise ValueError('Local file too large')
        return value


def read_json(path, maximum=16384, uid=None):
    value = json.loads(read_regular(path, maximum, uid),
                       parse_constant=lambda _: (_ for _ in ()).throw(ValueError('Invalid number')))
    if not isinstance(value, dict):
        raise ValueError('Expected object')
    return value


def release_path(paths, target=None):
    directory = (Path(target) if target is not None else paths.base / 'current').resolve(strict=True)
    releases = (paths.base / 'releases').resolve(strict=True)
    if directory.parent != releases or not directory.is_dir():
        raise ValueError('Invalid collector release')
    for parent in (releases, directory, directory / 'ups_panel'):
        info = parent.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != paths.trusted_uid or info.st_mode & 0o022:
            raise ValueError('Untrusted collector release')
    return directory


class SourceModified(ValueError):
    def __init__(self, build):
        self.build = build
        super().__init__('Collector identity changed')


def read_build(paths, target=None):
    directory = release_path(paths, target)
    digest = hashlib.sha256()
    source = None
    files = sorted((directory / 'ups_panel').glob('*.py'))
    if not 8 <= len(files) <= 128:
        raise ValueError('Invalid collector source')
    total = 0
    for path in files:
        encoded = read_regular(path, 1024 * 1024, paths.trusted_uid)
        total += len(encoded)
        if total > 4 * 1024 * 1024:
            raise ValueError('Collector source too large')
        digest.update(path.name.encode() + b'\0' + encoded + b'\0')
        if path.name == 'build_info.py':
            source = encoded.decode('utf-8')
    build_version = None
    for node in ast.parse(source or '').body:
        if isinstance(node, ast.Assign) and any(isinstance(item, ast.Name) and item.id == 'VERSION' for item in node.targets):
            build_version = version(ast.literal_eval(node.value))
    if build_version is None:
        raise ValueError('Missing collector version')
    result = {'version': build_version, 'source_sha256': digest.hexdigest(), 'revision': None}
    metadata = directory / 'collector-release.json'
    if metadata.exists() or metadata.is_symlink():
        value = read_json(metadata, uid=paths.trusted_uid)
        if value.get('version') != build_version or value.get('source_sha256') != result['source_sha256']:
            raise SourceModified(result)
        result['revision'] = hex_value(value.get('revision'), 40)
    return result


def fresh_snapshot(paths, expected=None, after=0):
    try:
        value = read_json(paths.snapshot, 65536)
        sample = value.get('sample')
        if type(value.get('schema')) is not int or value['schema'] != 1 or value.get('source') != 'usbmon' or not isinstance(sample, dict):
            return False
        now = time.time()
        for timestamp in (value.get('heartbeat'), sample.get('timestamp')):
            if type(timestamp) not in (int, float) or not after <= timestamp <= now or not now - timestamp < 10:
                return False
        if expected is not None:
            collector = value.get('collector')
            build = build_value(collector.get('build')) if isinstance(collector, dict) else None
            if not build or any(build[key] != expected[key] for key in ('version', 'source_sha256')):
                return False
        return True
    except (OSError, ValueError, TypeError, RecursionError):
        return False


def rollback_status(paths):
    result = {'available': False, 'version': None, 'reason': 'no_previous'}
    try:
        directory = release_path(paths)
        state = read_json(directory / 'rollback' / 'state.json', uid=paths.trusted_uid)
        if state.get('preserve_host_config') is not True:
            return dict(result, reason='legacy_backup')
        target = state.get('current')
        if not isinstance(target, str):
            return result
        previous = Path(target) if Path(target).is_absolute() else paths.base / target
        previous = release_path(paths, previous)
        if previous == directory:
            return result
        build = read_build(paths, previous)
        # v0.8 is the oldest supported baseline; it reads both saved calibration schemas.
        if version_tuple(build['version']) < (0, 8, 0):
            return dict(result, version=build['version'], reason='incompatible')
        return {'available': True, 'version': build['version'], 'reason': 'available'}
    except (OSError, ValueError, TypeError, RecursionError, SyntaxError):
        return result


def run_helper(paths, action, current_hash, source=None, progress=None):
    """Execute the installed helper, never a script supplied by a release archive."""
    read_regular(paths.helper, 256 * 1024, paths.trusted_uid)
    command = ['/usr/bin/python3', str(paths.helper), action, '--expected-current-sha256', current_hash]
    if action == 'install':
        command += ['--source', str(source), '--preserve-host-config']
    environment = {'PATH': '/usr/sbin:/usr/bin:/sbin:/bin', 'PYTHONDONTWRITEBYTECODE': '1',
                   'PYTHONUNBUFFERED': '1', 'LANG': 'C.UTF-8'}
    outcome = None
    lines = bytearray()
    started = time.monotonic()
    terminated = False
    with subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                          stderr=subprocess.STDOUT, env=environment, cwd=paths.helper.parents[1]) as process:
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            while selector.get_map():
                elapsed = time.monotonic() - started
                if elapsed > 120 and not terminated:
                    process.terminate()
                    terminated = True
                if elapsed > 148:
                    process.kill()
                    break
                for key, _ in selector.select(0.25):
                    chunk = os.read(key.fileobj.fileno(), 4096)
                    if not chunk:
                        selector.unregister(key.fileobj)
                        break
                    lines.extend(chunk)
                    while b'\n' in lines:
                        line, _, tail = lines.partition(b'\n')
                        lines = bytearray(tail)
                        try:
                            item = json.loads(line)
                            if isinstance(item, dict):
                                if item.get('stage') in ('installing', 'restarting', 'validating', 'rolling_back') and progress:
                                    progress(item['stage'])
                                if item.get('outcome') in ('restored', 'recovery_failed'):
                                    outcome = item['outcome']
                        except (ValueError, TypeError, RecursionError):
                            pass
                    if len(lines) > 8192:
                        lines.clear()
            returncode = process.wait(timeout=2)
    return {'returncode': returncode, 'outcome': outcome}


class UpdateManager:
    def __init__(self, paths=None, release_client=None, helper=None):
        self.paths = paths or UpdaterPaths()
        self.client = release_client or ReleaseClient()
        self.helper = helper or run_helper
        self.lock = threading.RLock()
        self.worker = None
        self.stopping = False
        self.requests = deque(maxlen=10)
        self.failed_auth = deque(maxlen=10)
        self.latest = None
        self.checked_at = None
        self.operation = None
        self._ensure_state()
        secret = read_regular(self.paths.key, 128, self.paths.trusted_uid).strip().decode('ascii')
        key_info = self.paths.key.lstat()
        if not KEY_PATTERN.fullmatch(secret) or key_info.st_mode & 0o077 or key_info.st_nlink != 1:
            raise ValueError('Updater key must be a private regular file')
        self._key = secret
        self._load_state()

    def _ensure_state(self):
        self.paths.state.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = self.paths.state.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != self.paths.trusted_uid or info.st_mode & 0o077:
            raise ValueError('Updater state must be a private directory')

    def _load_state(self):
        try:
            saved = read_json(self.paths.state / 'state.json', uid=self.paths.trusted_uid)
            # Latest release objects are deliberately not trusted after service restart.
            # A new authenticated check is required before any further installation.
            status = unavailable('ready')
            status.update(installed=True, updater_version=VERSION, operation=saved.get('operation'))
            self.operation = public_status(status)['operation']
            if self.operation and self.operation['busy']:
                self.operation.update(stage='interrupted', busy=False, finished_at=time.time(),
                                      updated_at=time.time(), error=UpdateError('interrupted').public(),
                                      outcome='manual_required' if self.operation['action'] != 'check' else None)
                self._save()
        except FileNotFoundError:
            pass
        except (OSError, ValueError, TypeError, UpdateError, RecursionError):
            # Corrupt state cannot authorize a version or a recovery command.
            self.operation = None

    def _save(self):
        data = json.dumps({'schema': SCHEMA, 'operation': self.operation}, allow_nan=False).encode()
        descriptor, temporary = tempfile.mkstemp(prefix='.state-', dir=self.paths.state)
        try:
            with os.fdopen(descriptor, 'wb') as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.paths.state / 'state.json')
            descriptor = os.open(self.paths.state, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        finally:
            Path(temporary).unlink(missing_ok=True)

    def _current(self):
        return self._source_state()[0]

    def _source_state(self):
        try:
            build = read_build(self.paths)
            has_metadata = (release_path(self.paths) / 'collector-release.json').is_file()
            return build, 'verified' if has_metadata else 'unverified', None
        except SourceModified as exc:
            return exc.build, 'modified', 'source_modified'
        except (OSError, ValueError, TypeError, RecursionError, SyntaxError):
            return None, 'unreadable', 'source_unreadable'

    def _preflight(self):
        current, source_status, code = self._source_state()
        result = {'ready': False, 'code': code, 'target_verified': False}
        if code:
            return result
        if not fresh_snapshot(self.paths):
            return dict(result, code='collector_unavailable')
        if not fresh_snapshot(self.paths, current):
            return dict(result, code='runtime_mismatch')
        try:
            path, profile = environment_config(self.paths.config_env)
            require_target = (release_path(self.paths) / 'ups_panel' / 'config_target.py').is_file()
            return calibration_readiness(read_json(self.paths.snapshot, 65536), path, profile, require_target=require_target)
        except (OSError, ValueError, TypeError, RecursionError):
            return dict(result, code='calibration_unreadable')

    def _wait_recovered(self, expected, after, timeout=20):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._current() != expected:
                return False
            if fresh_snapshot(self.paths, expected, after=after):
                return True
            time.sleep(0.25)
        return False

    def status(self):
        with self.lock:
            result = unavailable('ready')
            current, source_status, source_code = self._source_state()
            try:
                snapshot = read_json(self.paths.snapshot, 65536)
                collector = snapshot.get('collector')
                runtime = build_value(collector.get('build')) if isinstance(collector, dict) else None
            except (OSError, ValueError, TypeError, RecursionError):
                runtime = None
            preflight = self._preflight()
            result.update(installed=True, updater_version=VERSION, current=current,
                          source_status=source_status, source_error=UpdateError(source_code).public() if source_code else None,
                          runtime=runtime, preflight=preflight,
                          checked_at=self.checked_at, rollback=rollback_status(self.paths),
                          operation=dict(self.operation) if self.operation else None)
            if self.latest is not None:
                latest = self.latest
                result['latest'] = {key: getattr(latest, key) for key in
                    ('version', 'tag', 'release_id', 'sha256', 'size', 'published_at', 'notes')}
                result['latest']['url'] = latest.release_url
            return public_status(result)

    def _authenticate(self, key):
        valid = isinstance(key, str) and KEY_PATTERN.fullmatch(key) and hmac.compare_digest(key, self._key)
        if valid:
            return
        now = time.monotonic()
        while self.failed_auth and now - self.failed_auth[0] > 60:
            self.failed_auth.popleft()
        if len(self.failed_auth) >= 10:
            raise UpdateError('rate_limited')
        self.failed_auth.append(now)
        raise UpdateError('unauthorized')

    def request(self, request):
        if not isinstance(request, dict) or type(request.get('schema')) is not int or request['schema'] != SCHEMA:
            raise UpdateError('invalid_request')
        action = request.get('action')
        if action == 'status':
            if set(request) - {'schema', 'action'}:
                raise UpdateError('invalid_request')
            return self.status()
        if action not in ('check', 'install', 'rollback') or set(request) != {'schema', 'action', 'key', 'payload'}:
            raise UpdateError('invalid_request')
        with self.lock:
            self._authenticate(request.get('key'))
            if self.stopping or (self.operation and self.operation['busy']):
                raise UpdateError('busy')
            payload = request['payload']
            required = {'check': set(), 'install': {'version', 'release_id', 'sha256'},
                        'rollback': {'version', 'current_version'}}[action]
            if not isinstance(payload, dict) or set(payload) != required:
                raise UpdateError('invalid_request')
            current = self._current()
            configuration_before = None
            target = None
            if action != 'check':
                configuration_before = self._preflight()
                if not configuration_before['ready']:
                    raise UpdateError(configuration_before['code'])
                if not version(payload.get('version')):
                    raise UpdateError('invalid_request')
                target = payload['version']
            if action == 'install':
                if type(payload.get('release_id')) is not int or not hex_value(payload.get('sha256'), 64):
                    raise UpdateError('invalid_request')
                if self.latest is None or self.checked_at is None or time.time() - self.checked_at > 3600:
                    raise UpdateError('check_required')
                if any(payload[field] != getattr(self.latest, field) for field in ('version', 'release_id', 'sha256')):
                    raise UpdateError('stale_release')
                if version_tuple(target) <= version_tuple(current['version']):
                    raise UpdateError('no_update')
            if action == 'rollback':
                if not version(payload.get('current_version')) or payload['current_version'] != current['version']:
                    raise UpdateError('stale_current')
                previous = rollback_status(self.paths)
                if not previous['available'] or previous['version'] != target:
                    raise UpdateError('rollback_unavailable')
            now = time.monotonic()
            while self.requests and now - self.requests[0] > 60:
                self.requests.popleft()
            if len(self.requests) >= 10:
                raise UpdateError('rate_limited')
            self.requests.append(now)
            timestamp = time.time()
            self.operation = {'id': uuid.uuid4().hex, 'action': action,
                'stage': {'check': 'checking', 'install': 'downloading', 'rollback': 'rolling_back'}[action],
                'busy': True, 'started_at': timestamp, 'updated_at': timestamp, 'finished_at': None,
                'from_version': current['version'] if current else None, 'to_version': target,
                'outcome': None, 'error': None}
            try:
                self._save()
            except OSError:
                self.operation = None
                raise UpdateError('internal_error') from None
            self.worker = threading.Thread(target=self._work, args=(action, current, self.latest, configuration_before), daemon=False)
            self.worker.start()
            return self.status()

    def _stage(self, stage):
        with self.lock:
            self.operation.update(stage=stage, updated_at=time.time())
            self._save()

    def _finish(self, code=None, outcome=None):
        with self.lock:
            self.operation.update(stage='failed' if code else 'succeeded', busy=False,
                finished_at=time.time(), updated_at=time.time(), outcome=outcome,
                error=UpdateError(code).public() if code else None)
            try:
                self._save()
            except OSError:
                self.operation.update(stage='failed', error=UpdateError('internal_error').public())

    def _work(self, action, before, release, configuration_before=None):
        staging = None
        helper_started = False
        try:
            if action == 'check':
                latest = self.client.latest()
                with self.lock:
                    self.latest = latest
                    self.checked_at = time.time()
                self._finish(outcome='checked')
                return
            source = None
            if action == 'install':
                encoded = self.client.download(release)
                self._stage('verifying')
                staging = Path(tempfile.mkdtemp(prefix='stage-', dir=self.paths.state))
                source = staging / 'source'
                metadata = extract_package(encoded, source, expected_version=release.version, updater_schema=SCHEMA)
                target = {'version': metadata['version'], 'source_sha256': metadata['source_sha256'],
                          'revision': metadata['revision']}
            else:
                current_dir = release_path(self.paths)
                saved = read_json(current_dir / 'rollback' / 'state.json', uid=self.paths.trusted_uid)
                previous = Path(saved['current'])
                target = read_build(self.paths, previous if previous.is_absolute() else self.paths.base / previous)
            if self._current() != before:
                raise UpdateError('stale_current')
            if self.stopping:
                raise UpdateError('interrupted')
            if not fresh_snapshot(self.paths, before):
                raise UpdateError('collector_unavailable')
            health = self._preflight()
            if not health['ready']:
                raise UpdateError(health['code'])
            if any(health.get(key) != configuration_before.get(key) for key in ('revision', 'target_identity', 'file_state')):
                raise UpdateError('configuration_changed')
            self._stage('installing' if action == 'install' else 'rolling_back')
            helper_started = True
            result = self.helper(self.paths, action, before['source_sha256'], source=source, progress=self._stage)
            helper_finished_at = time.time()
            current = self._current()
            if result['returncode'] != 0:
                if current == before and self._wait_recovered(before, helper_finished_at):
                    raise UpdateError('install_failed' if action == 'install' else 'rollback_failed')
                raise UpdateError('recovery_failed')
            if current != target or not fresh_snapshot(self.paths, target):
                raise UpdateError('recovery_failed')
            health = self._preflight()
            if not health['ready']:
                raise UpdateError(health['code'])
            if any(health.get(key) != configuration_before.get(key) for key in ('revision', 'target_identity', 'file_state')):
                raise UpdateError('configuration_changed')
            self._finish(outcome='updated' if action == 'install' else 'rolled_back')
        except ReleaseError as exc:
            self._finish(code=exc.code)
        except UpdateError as exc:
            outcome = ('restored' if exc.code in ('install_failed', 'rollback_failed')
                       else 'manual_required' if helper_started else None)
            self._finish(code=exc.code, outcome=outcome)
        except Exception:
            self._finish(code='recovery_failed' if helper_started else 'internal_error',
                         outcome='manual_required' if helper_started else None)
        finally:
            if staging is not None:
                shutil.rmtree(staging, ignore_errors=True)

    def close(self):
        with self.lock:
            self.stopping = True
            worker = self.worker
        if worker:
            worker.join()


class UpdateHandler(socketserver.StreamRequestHandler):
    timeout = 3

    def handle(self):
        try:
            encoded = self.rfile.readline(MAX_REQUEST + 1)
            if len(encoded) > MAX_REQUEST or not encoded.endswith(b'\n'):
                raise UpdateError('invalid_request')
            request = json.loads(encoded)
            response = {'ok': True, 'status': self.server.manager.request(request)}
        except UpdateError as exc:
            response = {'ok': False, 'error': exc.public()}
        except (ValueError, TypeError, RecursionError):
            response = {'ok': False, 'error': UpdateError('invalid_request').public()}
        except (OSError, TimeoutError):
            return
        except Exception:
            response = {'ok': False, 'error': UpdateError('internal_error').public()}
        encoded = json.dumps(response, ensure_ascii=False, allow_nan=False, separators=(',', ':')).encode() + b'\n'
        if len(encoded) > MAX_RESPONSE:
            encoded = b'{"ok":false,"error":{"code":"internal_error"}}\n'
        try:
            self.wfile.write(encoded)
        except OSError:
            pass


def prepare_socket_directory(paths):
    """Apply the fixed socket group after systemd's per-exec directory setup."""
    descriptor = os.open(paths.socket.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        info = os.fstat(descriptor)
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != paths.trusted_uid
                or info.st_gid not in (0, paths.socket_gid) or info.st_mode & 0o027):
            raise ValueError('Invalid updater socket directory permissions')
        # Numeric fchown does not require a corresponding /etc/group entry.
        os.fchown(descriptor, paths.trusted_uid, paths.socket_gid)
        os.fchmod(descriptor, 0o750)
        prepared = os.fstat(descriptor)
        if (not stat.S_ISDIR(prepared.st_mode) or prepared.st_uid != paths.trusted_uid
                or prepared.st_gid != paths.socket_gid or stat.S_IMODE(prepared.st_mode) != 0o750
                or (prepared.st_dev, prepared.st_ino) != (info.st_dev, info.st_ino)):
            raise ValueError('Invalid updater socket directory permissions')
    finally:
        os.close(descriptor)


class UpdateServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = False
    request_queue_size = 8
    block_on_close = True

    def __init__(self, manager):
        self.manager = manager
        self.slots = threading.BoundedSemaphore(8)
        path = manager.paths.socket
        prepare_socket_directory(manager.paths)
        info = path.parent.lstat()
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != manager.paths.trusted_uid
                or info.st_gid != manager.paths.socket_gid or info.st_mode & 0o027):
            raise ValueError('Invalid updater socket directory permissions')
        if path.exists() or path.is_symlink():
            info = path.lstat()
            if not stat.S_ISSOCK(info.st_mode) or info.st_uid != manager.paths.trusted_uid:
                raise ValueError('Refusing to replace an untrusted socket path')
            with socket.socket(socket.AF_UNIX) as probe:
                probe.settimeout(1)
                try:
                    probe.connect(str(path))
                except ConnectionRefusedError:
                    path.unlink()
                else:
                    raise ValueError('Updater socket is already active')
        super().__init__(str(path), UpdateHandler)
        os.chown(path, manager.paths.trusted_uid, manager.paths.socket_gid)
        os.chmod(path, 0o660)

    def process_request(self, request, client_address):
        if not self.slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self.slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()

    def handle_error(self, request, client_address):
        # Never write raw request data, headers, keys or exception values to a log.
        pass


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args()
    if os.geteuid() != 0:
        parser.error('Run the installed host update service as root')
    manager = UpdateManager()
    with UpdateServer(manager) as server:
        def stop(signum, frame):
            manager.stopping = True
            threading.Thread(target=server.shutdown, daemon=True).start()
        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGINT, stop)
        try:
            server.serve_forever(poll_interval=0.25)
        finally:
            manager.close()
    manager.paths.socket.unlink(missing_ok=True)


if __name__ == '__main__':
    main()
