"""Bounded, authenticated IPC for this project's optional host updater."""
import json
import math
from pathlib import Path
import re
import socket

SOCKET = '/run/ugreen-ups-updater/control.sock'
SCHEMA = 1
MAX_REQUEST = 8192
MAX_RESPONSE = 16384
VERSION_PATTERN = re.compile(r'(0|[1-9][0-9]{0,5})\.(0|[1-9][0-9]{0,5})\.(0|[1-9][0-9]{0,5})')
KEY_PATTERN = re.compile(r'[A-Za-z0-9_-]{43}')
STAGES = ('checking', 'downloading', 'verifying', 'installing', 'restarting', 'validating',
          'rolling_back', 'succeeded', 'failed', 'interrupted')
ERRORS = {
    'invalid_request': '更新请求无效，请刷新页面后重试。',
    'unauthorized': '管理密钥不正确，请核对 NAS 上保存的密钥。',
    'busy': '已有更新操作正在进行，请等待完成。',
    'rate_limited': '尝试过于频繁，请稍后重试。',
    'service_unavailable': '宿主更新服务暂不可用，请检查安装和挂载。',
    'incompatible_service': '更新服务版本不兼容，请在 NAS 上更新服务。',
    'check_required': '请先检查更新，再选择已校验的发行版本。',
    'stale_release': '发行信息已变化，请重新检查更新。',
    'no_update': '已安装此版本或更高版本。',
    'collector_unavailable': '采集器未正常运行，恢复采集后再更新。',
    'rollback_unavailable': '没有可通过面板回退的兼容版本。',
    'stale_current': '采集器版本已变化，请刷新后重试。',
    'network_error': '无法连接发行服务器，请检查 NAS 网络后重试。',
    'install_failed': '采集器更新失败，已恢复先前版本。',
    'rollback_failed': '回退失败，已恢复回退前版本。',
    'recovery_failed': '采集恢复未通过，请在 NAS 上检查采集器服务。',
    'interrupted': '上次更新操作被中断，请核对当前版本和采集状态。',
    'internal_error': '更新操作未完成，请检查宿主更新服务。',
    'response_too_large': '发行服务器响应超过允许大小。',
    'untrusted_url': '发行下载地址不在本项目允许的来源内。',
    'invalid_response': '发行服务器响应无效，请稍后重新检查。',
    'no_stable_release': '暂未找到正式发行版本。',
    'invalid_release': '发行信息不符合采集器更新格式。',
    'asset_missing': '此发行版本尚未提供采集器更新包。',
    'invalid_digest': '更新包缺少有效的 SHA-256 校验信息。',
    'checksum_mismatch': '更新包校验未通过，采集器未改变。',
    'release_changed': '发行附件已变化，请重新检查更新。',
    'invalid_package': '更新包内容校验未通过，采集器未改变。',
    'incompatible_package': '更新包与当前更新服务不兼容，请先更新宿主更新服务。',
    'unsafe_destination': '更新暂存目录不符合要求，请检查宿主更新服务。',
    'package_write_failed': '无法写入更新暂存目录，请检查磁盘空间。',
}


class UpdateError(Exception):
    def __init__(self, code):
        self.code = code if isinstance(code, str) and code in ERRORS else 'internal_error'
        super().__init__(ERRORS[self.code])

    def public(self):
        return {'code': self.code, 'message': ERRORS[self.code]}


def version(value):
    return value if isinstance(value, str) and VERSION_PATTERN.fullmatch(value) else None


def version_tuple(value):
    return tuple(map(int, value.split('.'))) if version(value) else (-1, -1, -1)


def hex_value(value, length):
    return value if isinstance(value, str) and re.fullmatch('[0-9a-f]{' + str(length) + '}', value) else None


def timestamp(value):
    return value if type(value) in (int, float) and math.isfinite(value) and 0 <= value < 1e12 else None


def build_value(value):
    if not isinstance(value, dict) or not version(value.get('version')):
        return None
    revision = value.get('revision')
    return {'version': value['version'], 'revision': revision if isinstance(revision, str)
            and re.fullmatch(r'[0-9a-f]{7,40}', revision) else None,
            'source_sha256': hex_value(value.get('source_sha256'), 64)}


def unavailable(availability='not_installed'):
    return {'schema': SCHEMA, 'installed': False, 'availability': availability,
            'updater_version': None, 'updater_schema': SCHEMA, 'current': None,
            'latest': None, 'checked_at': None, 'update_available': False, 'auth_required': True,
            'rollback': {'available': False, 'version': None, 'reason': 'unknown'}, 'operation': None}


def public_status(value):
    """Validate the protocol and copy only the public fields; never forward secrets."""
    if (not isinstance(value, dict) or type(value.get('schema')) is not int or value['schema'] != SCHEMA
            or value.get('installed') is not True or value.get('availability') != 'ready'
            or not version(value.get('updater_version'))):
        raise UpdateError('incompatible_service')
    result = unavailable('ready')
    result.update(installed=True, updater_version=value['updater_version'],
                  current=build_value(value.get('current')), checked_at=timestamp(value.get('checked_at')))
    latest = value.get('latest')
    if latest is not None:
        if (not isinstance(latest, dict) or not version(latest.get('version'))
                or latest.get('tag') != 'v' + latest['version']
                or type(latest.get('release_id')) is not int or not 0 < latest['release_id'] < 2**63
                or type(latest.get('size')) is not int or not 0 < latest['size'] <= 4 * 1024 * 1024
                or not hex_value(latest.get('sha256'), 64)
                or latest.get('url') != 'https://github.com/BSakura-Miku/ugreen-ups-panel/releases/tag/v' + latest['version']):
            raise UpdateError('incompatible_service')
        published = latest.get('published_at')
        result['latest'] = {key: latest[key] for key in ('version', 'tag', 'release_id', 'size', 'sha256', 'url')}
        result['latest'].update(notes=latest.get('notes', '')[:2000] if isinstance(latest.get('notes', ''), str) else '',
                                published_at=published if isinstance(published, str) and len(published) <= 40 else None)
    result['update_available'] = bool(result['current'] and result['latest']
        and version_tuple(result['latest']['version']) > version_tuple(result['current']['version']))
    rollback = value.get('rollback')
    if isinstance(rollback, dict):
        reason = rollback.get('reason')
        result['rollback'] = {'available': rollback.get('available') is True and bool(version(rollback.get('version'))),
                              'version': version(rollback.get('version')),
                              'reason': reason if reason in ('available', 'no_previous', 'legacy_backup', 'incompatible', 'unknown') else 'unknown'}
    operation = value.get('operation')
    if operation is not None:
        if (not isinstance(operation, dict) or operation.get('action') not in ('check', 'install', 'rollback')
                or operation.get('stage') not in STAGES or type(operation.get('busy')) is not bool
                or not isinstance(operation.get('id'), str) or not re.fullmatch(r'[0-9a-f]{32}', operation['id'])
                or timestamp(operation.get('started_at')) is None or timestamp(operation.get('updated_at')) is None):
            raise UpdateError('incompatible_service')
        op = {key: operation[key] for key in ('id', 'action', 'stage', 'busy', 'started_at', 'updated_at')}
        op.update(finished_at=timestamp(operation.get('finished_at')), from_version=version(operation.get('from_version')),
                  to_version=version(operation.get('to_version')))
        outcome = operation.get('outcome')
        op['outcome'] = outcome if outcome in ('updated', 'rolled_back', 'restored', 'manual_required', 'checked') else None
        error = operation.get('error')
        op['error'] = UpdateError(error.get('code')).public() if isinstance(error, dict) else None
        result['operation'] = op
    return result


class UpdateClient:
    def __init__(self, path=SOCKET, timeout=3):
        self.path = str(path)
        self.timeout = timeout

    def request(self, action='status', payload=None, key=None):
        request = {'schema': SCHEMA, 'action': action}
        if action != 'status':
            request.update(payload=payload, key=key)
        try:
            encoded = json.dumps(request, allow_nan=False, separators=(',', ':')).encode() + b'\n'
        except (ValueError, TypeError, RecursionError):
            raise UpdateError('invalid_request') from None
        if len(encoded) > MAX_REQUEST:
            raise UpdateError('invalid_request')
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
                connection.settimeout(self.timeout)
                connection.connect(self.path)
                connection.sendall(encoded)
                with connection.makefile('rb') as stream:
                    response = stream.readline(MAX_RESPONSE + 1)
            if len(response) > MAX_RESPONSE or not response.endswith(b'\n'):
                raise UpdateError('incompatible_service')
            value = json.loads(response)
            if not isinstance(value, dict) or type(value.get('ok')) is not bool:
                raise UpdateError('incompatible_service')
            if not value['ok']:
                error = value.get('error')
                raise UpdateError(error.get('code') if isinstance(error, dict) else 'internal_error')
            return public_status(value.get('status'))
        except FileNotFoundError:
            if action == 'status':
                return unavailable('not_installed')
            raise UpdateError('service_unavailable') from None
        except (OSError, TimeoutError):
            if action == 'status':
                return unavailable('unreachable')
            raise UpdateError('service_unavailable') from None
        except (ValueError, TypeError, RecursionError):
            if action == 'status':
                return unavailable('incompatible')
            raise UpdateError('incompatible_service') from None
        except UpdateError:
            if action == 'status':
                return unavailable('incompatible')
            raise
