"""Same-origin HTTP bridge; the panel never reads the updater management key."""
import asyncio
import json
from urllib.parse import urlsplit

from fastapi import HTTPException, Request

from .update_client import KEY_PATTERN, UpdateClient, UpdateError, build_value


def install_update_routes(app, read_snapshot, client=None, *, calibration_status=None):
    client = client or UpdateClient()

    def panel_preflight_code():
        if calibration_status is None:
            return None
        try:
            value = calibration_status()
        except (OSError, ValueError, TypeError):
            return 'calibration_unreadable'
        readiness = value.get('readiness') if isinstance(value, dict) else None
        if not isinstance(readiness, dict):
            return None
        if readiness.get('target') == 'mismatched':
            return 'calibration_target_mismatch'
        if readiness.get('target') == 'unavailable':
            return 'calibration_unreadable'
        # Older collectors cannot prove target identity. They must remain able
        # to upgrade; reject only the definite wiring/file failures listed here.
        codes = {readiness.get('code')} if isinstance(readiness.get('code'), str) else set()
        issues = readiness.get('issues')
        if isinstance(issues, list):
            codes.update(item['code'] for item in issues
                         if isinstance(item, dict) and isinstance(item.get('code'), str))
        for code, public_code in (('path_unconfigured', 'calibration_unconfigured'),
                                  ('file_unreadable', 'calibration_unreadable'),
                                  ('unsupported_schema', 'calibration_incompatible')):
            if code in codes:
                return public_code
        return None

    @app.get('/api/collector-update')
    async def updater_status():
        status = await asyncio.to_thread(client.request)
        if status.get('runtime') is None:
            collector = read_snapshot().get('collector')
            if isinstance(collector, dict):
                status['runtime'] = build_value(collector.get('build'))
        code = await asyncio.to_thread(panel_preflight_code)
        if code:
            status['preflight'] = {'ready': False, 'code': code, 'target_verified': False}
        return status

    @app.post('/api/collector-update/{action}', status_code=202)
    async def updater_action(action: str, request: Request):
        def reject(code, status):
            raise HTTPException(status, UpdateError(code).public())
        if action not in ('check', 'install', 'rollback'):
            reject('invalid_request', 404)
        if (request.headers.get('x-ups-update') != '1'
                or request.headers.get('content-type', '').split(';', 1)[0].strip().lower() != 'application/json'
                or request.headers.get('sec-fetch-site') in ('cross-site', 'same-site')):
            reject('invalid_request', 403)
        origin = request.headers.get('origin')
        if origin is not None:
            try:
                parsed = urlsplit(origin)
                valid = (parsed.scheme in ('http', 'https') and parsed.username is None and parsed.password is None
                         and parsed.path in ('', '/') and not parsed.query and not parsed.fragment
                         and parsed.netloc.casefold() == request.headers.get('host', '').casefold())
            except ValueError:
                valid = False
            if not valid:
                reject('invalid_request', 403)
        key = request.headers.get('x-ups-update-key', '')
        if not KEY_PATTERN.fullmatch(key):
            reject('unauthorized', 401)
        body = bytearray()
        async for part in request.stream():
            body.extend(part)
            if len(body) > 1024:
                reject('invalid_request', 413)
        try:
            payload = json.loads(body, parse_constant=lambda _: (_ for _ in ()).throw(ValueError('Invalid number')))
            required = {'check': set(), 'install': {'version', 'release_id', 'sha256'},
                        'rollback': {'version', 'current_version'}}[action]
            if not isinstance(payload, dict) or set(payload) != required:
                raise ValueError
        except (ValueError, TypeError, RecursionError):
            reject('invalid_request', 400)
        if action != 'check':
            code = await asyncio.to_thread(panel_preflight_code)
            if code:
                reject(code, 409)
        try:
            return await asyncio.to_thread(client.request, action, payload, key)
        except UpdateError as exc:
            code = exc.code
            status = (401 if code == 'unauthorized' else 429 if code == 'rate_limited'
                      else 503 if code in ('service_unavailable', 'incompatible_service', 'internal_error')
                      else 409 if code in ('busy', 'check_required', 'stale_release', 'stale_current',
                                           'no_update', 'rollback_unavailable', 'collector_unavailable',
                                           'source_modified', 'source_unreadable', 'runtime_mismatch', 'calibration_unconfigured',
                                           'calibration_unreadable', 'calibration_incompatible', 'calibration_mismatch',
                                           'calibration_target_unverified', 'calibration_target_mismatch',
                                           'configuration_changed') else 400)
            raise HTTPException(status, exc.public()) from None
