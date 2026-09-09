import asyncio
from contextlib import asynccontextmanager
import csv
from datetime import date as calendar_date
import io
import json
import logging
import os
from pathlib import Path
import sqlite3
import time
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles

from .storage import CONTEXT_FIELDS, Store, METRICS
from .telemetry import load_snapshot
from .cell_balance import CellBalanceMonitor
from .raw_observation import RawObservationMonitor
from .diagnostics import diagnostic_view, diagnostic_export, observation_csv
from .update_api import install_update_routes
from .calibration import CalibrationError, normalize_config, save_config
from .calibration_status import calibration_status as assess_calibration
from .storage_health import storage_error_code, storage_message, storage_status

LOG = logging.getLogger('panel')


def create_app(snapshot=None, database=None, static=None, calibration=None):
    snapshot = snapshot or os.getenv('UPS_SNAPSHOT', '/run/ugreen-ups-panel/latest.json')
    database = database or os.getenv('UPS_DATABASE', '/data/history.sqlite')
    static = Path(static or os.getenv('UPS_STATIC', 'frontend/dist'))
    calibration = Path(calibration or os.getenv('UPS_CALIBRATION_CONFIG') or Path(database).with_name('calibration.json'))
    calibration_lock = asyncio.Lock()
    state = {'store': None, 'storage_error': None, 'storage_error_code': None, 'last_success': 0,
             'observation_ready': asyncio.Event()}
    cell_balance = CellBalanceMonitor()
    raw_observation = RawObservationMonitor()

    async def observe_cells():
        while True:
            # Disk writes can wait on SQLite locks; voltage observation must
            # keep its own cadence. HTTP reads never advance this clock.
            view = load_snapshot(snapshot)
            cell_balance.ingest(view)
            raw_observation.ingest(view)
            ready = state['observation_ready']
            state['observation_ready'] = asyncio.Event()
            ready.set()
            await asyncio.sleep(0.5)

    async def record():
        while True:
            view = load_snapshot(snapshot)
            try:
                if state['store'] is None:
                    state['store'] = await asyncio.to_thread(Store, database)
                await asyncio.to_thread(state['store'].ingest, view)
                state['storage_error'] = None
                state['storage_error_code'] = None
                state['last_success'] = time.time()
            except (OSError, sqlite3.Error, ValueError, TypeError, KeyError) as exc:
                state['storage_error_code'] = storage_error_code(exc)
                state['storage_error'] = storage_message(state['storage_error_code'])
                LOG.warning('History writer: %s', exc)
            await asyncio.sleep(2)

    @asynccontextmanager
    async def lifespan(app):
        tasks = [asyncio.create_task(observe_cells()), asyncio.create_task(record())]
        try:
            yield
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            if state['store']:
                try:
                    await asyncio.to_thread(state['store'].flush)
                except (OSError, sqlite3.Error):
                    LOG.exception('History flush failed')

    app = FastAPI(title='US3000 监控面板', lifespan=lifespan, docs_url=None, redoc_url=None)
    install_update_routes(app, lambda: load_snapshot(snapshot),
                          calibration_status=lambda: assess_calibration(load_snapshot(snapshot), calibration))

    @app.middleware('http')
    async def headers(request, call_next):
        response = await call_next(request)
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['Referrer-Policy'] = 'same-origin'
        response.headers['X-Frame-Options'] = 'SAMEORIGIN'
        if request.url.path.startswith('/api/'):
            response.headers['Cache-Control'] = 'no-store'
        return response

    @app.get('/api/live')
    async def live():
        view = load_snapshot(snapshot)
        view['cell_balance'] = cell_balance.snapshot(view)
        if view['fresh'] and view['cell_balance']['reason'] == 'not_observed':
            # A GET can land just after the collector replaces latest.json.
            # Wait for the independent observer rather than count the request
            # as telemetry or display a grade from a different sample.
            try:
                await asyncio.wait_for(state['observation_ready'].wait(), timeout=0.75)
            except asyncio.TimeoutError:
                pass
            view = load_snapshot(snapshot)
            view['cell_balance'] = cell_balance.snapshot(view)
        view['storage_error'] = state['storage_error']
        view['storage_error_code'] = state['storage_error_code']
        view['storage'] = storage_status(state)
        view['storage_dropped_buckets'] = state['store'].dropped_buckets if state['store'] else 0
        nut = view.setdefault('nut', {})
        nut['fresh'] = bool(nut.get('available') and 0 <= time.time() - nut.get('timestamp', 0) <= 45)
        return view

    @app.get('/api/health')
    def health():
        return {'service': 'ok', 'capture_fresh': load_snapshot(snapshot)['fresh'],
                'storage_error': state['storage_error'],
                'storage_error_code': state['storage_error_code'], 'storage': storage_status(state),
                'storage_dropped_buckets': state['store'].dropped_buckets if state['store'] else 0}

    def diagnostics_data(minutes):
        view = load_snapshot(snapshot)
        now = view['server_time']
        observation = raw_observation.snapshot(view, now=now, minutes=minutes)
        return diagnostic_view(view, observation, now=now,
                               storage_ready=bool(state['last_success']),
                               storage_error=state['storage_error'], storage_error_code=state['storage_error_code'],
                               calibration_readiness=assess_calibration(view, calibration)['readiness'])

    @app.get('/api/diagnostics')
    def diagnostics(minutes: int = Query(60, ge=1, le=60)):
        return diagnostics_data(minutes)

    @app.get('/api/diagnostics/export.json')
    def export_diagnostics(minutes: int = Query(60, ge=1, le=60)):
        payload = diagnostic_export(diagnostics_data(minutes))
        return Response(json.dumps(payload, ensure_ascii=False, allow_nan=False, indent=2) + '\n',
                        media_type='application/json',
                        headers={'Content-Disposition': 'attachment; filename="us3000-diagnostics.json"'})

    @app.get('/api/diagnostics/export.csv')
    def export_observation(minutes: int = Query(60, ge=1, le=60)):
        return Response('\ufeff' + observation_csv(diagnostics_data(minutes)),
                        media_type='text/csv; charset=utf-8',
                        headers={'Content-Disposition': 'attachment; filename="us3000-observation.csv"'})

    def calibration_status():
        return assess_calibration(load_snapshot(snapshot), calibration)

    def client_config_version(request):
        version = request.headers.get('x-ups-calibration-version', '1')
        if version not in ('1', '2'):
            raise HTTPException(400, '校准页面版本不受支持，请刷新页面后重试。')
        return int(version)

    def require_compatible_client(status, version):
        if any(config and config['schema'] > version for config in (status['desired'], status['active'])):
            raise HTTPException(409, '校准配置已升级，请刷新页面后再查看或修改，原配置已保留。')

    @app.get('/api/calibration')
    def get_calibration(request: Request):
        status = calibration_status()
        require_compatible_client(status, client_config_version(request))
        return status

    @app.put('/api/calibration')
    async def put_calibration(request: Request):
        # JSON plus a required custom header prevent cross-site HTML forms from
        # writing settings. No cross-origin CORS permission is granted.
        if (request.headers.get('x-ups-calibration') != '1'
                or request.headers.get('content-type', '').split(';', 1)[0].strip().lower() != 'application/json'):
            raise HTTPException(403, '请通过面板的功率校准页面保存。')
        origin = request.headers.get('origin')
        if origin is not None:
            try:
                parsed_origin = urlsplit(origin)
            except ValueError:
                raise HTTPException(403, '浏览器来源无效。') from None
            # Compare the external authority carried in Host, not the backend
            # transport scheme: a local TLS proxy may connect to us over HTTP.
            # The required custom header, absent CORS permission and Fetch
            # Metadata check below continue to reject cross-origin browser writes.
            if (parsed_origin.scheme not in ('http', 'https') or parsed_origin.username is not None
                    or parsed_origin.path not in ('', '/') or parsed_origin.query or parsed_origin.fragment
                    or parsed_origin.netloc.casefold() != request.headers.get('host', '').casefold()):
                raise HTTPException(403, '不允许跨站修改校准配置。')
        if request.headers.get('sec-fetch-site') in ('cross-site', 'same-site'):
            raise HTTPException(403, '请从当前面板页面保存配置。')
        client_version = client_config_version(request)
        encoded = bytearray()
        async for part in request.stream():
            encoded.extend(part)
            if len(encoded) > 4096:
                raise HTTPException(413, '校准配置过大。')
        try:
            payload = json.loads(encoded)
            required_fields = {'profile', 'coefficients', 'expected_revision'}
            if (not isinstance(payload, dict)
                    or set(payload) not in (required_fields, required_fields | {'ac_voltage_nominal_v'})):
                raise CalibrationError('Invalid calibration request')
            if not isinstance(payload['expected_revision'], str):
                raise CalibrationError('Invalid revision')
            schema = 2 if 'ac_voltage_nominal_v' in payload else 1
            if schema > client_version:
                raise HTTPException(409, '请刷新功率校准页面后再保存新版配置。')
            config_data = {'schema': schema, 'profile': payload['profile'], 'coefficients': payload['coefficients']}
            if schema == 2:
                config_data['ac_voltage_nominal_v'] = payload['ac_voltage_nominal_v']
            config = normalize_config(config_data)
        except (ValueError, TypeError, RecursionError, OverflowError):
            raise HTTPException(400, '校准配置无效：交流基底须大于 0 且不超过 10；回充补偿须在 0–10 之间，电池放电须大于 0 且不超过 10。新版自定义配置可将后两项留空，电压档位须为 12、19 或 20 V。') from None
        async with calibration_lock:
            current = calibration_status()
            require_compatible_client(current, client_version)
            if not current['readiness']['can_save']:
                raise HTTPException(503, current['readiness']['message'] or '采集器尚未准备好接收校准配置。')
            if config['schema'] not in current['supported_config_schemas']:
                raise HTTPException(503, '当前宿主机采集器尚不支持多电压校准，请先更新采集器；原配置未改变。')
            if (current['desired']['schema'] == 2 and config['profile'] == 'custom' and config['schema'] == 1):
                raise HTTPException(409, '新版自定义配置必须保留适配器电压档位，请刷新页面后再保存。')
            if payload['expected_revision'] != current['edit_revision']:
                raise HTTPException(409, '配置已在其他页面改变，请重新载入后再保存。')
            try:
                await asyncio.to_thread(save_config, calibration, config)
            except (CalibrationError, OSError):
                raise HTTPException(503, '校准配置保存失败，请检查数据目录是否可写。') from None
            return calibration_status()

    def store():
        if state['store'] is None:
            raise HTTPException(503, state['storage_error'] or '历史存储正在初始化，请稍后重试。')
        return state['store']

    @app.get('/api/history')
    def history(hours: int = Query(24, ge=1, le=8760)):
        try:
            return store().history(hours)
        except (sqlite3.Error, ValueError, TypeError, KeyError) as exc:
            raise HTTPException(503, storage_message(storage_error_code(exc)))

    @app.get('/api/events')
    def events():
        try:
            return store().events()
        except (sqlite3.Error, ValueError, TypeError, KeyError) as exc:
            raise HTTPException(503, storage_message(storage_error_code(exc)))

    @app.get('/api/battery-sessions')
    def battery_sessions(days: int = Query(90, ge=1, le=365), limit: int = Query(50, ge=1, le=500)):
        try:
            view = load_snapshot(snapshot)
            return store().battery_history(days, limit, now=view['server_time'], capture_fresh=view['fresh'])
        except (sqlite3.Error, ValueError, TypeError, KeyError) as exc:
            raise HTTPException(503, storage_message(storage_error_code(exc)))

    @app.get('/api/battery-capacity')
    def battery_capacity():
        try:
            return store().capacity_reference(load_snapshot(snapshot))
        except (sqlite3.Error, ValueError, TypeError, KeyError) as exc:
            raise HTTPException(503, storage_message(storage_error_code(exc)))

    @app.get('/api/energy-usage')
    def energy_usage(month: str = Query(None, min_length=7, max_length=7,
                                       pattern=r'^\d{4}-(0[1-9]|1[0-2])$')):
        if month is not None:
            try:
                parsed = calendar_date.fromisoformat(month + '-01')
                if not 1970 <= parsed.year <= 9998:
                    raise ValueError('unsupported year')
            except ValueError:
                raise HTTPException(422, '月份无效') from None
        try:
            result = store().usage_month(month, lambda: load_snapshot(snapshot))
            result['storage_error'] = state['storage_error']
            result['storage_error_code'] = state['storage_error_code']
            result['storage'] = storage_status(state)
            return result
        except (OSError, sqlite3.Error, ValueError, TypeError, KeyError) as exc:
            raise HTTPException(503, storage_message(storage_error_code(exc))) from None

    @app.get('/api/energy-usage/day')
    def energy_usage_day(date: str = Query(..., min_length=10, max_length=10,
                                          pattern=r'^\d{4}-\d{2}-\d{2}$')):
        try:
            parsed = calendar_date.fromisoformat(date)
            if not 1970 <= parsed.year <= 9998:
                raise ValueError('unsupported year')
        except ValueError:
            raise HTTPException(422, '日期无效') from None
        try:
            result = store().usage_day(date, lambda: load_snapshot(snapshot))
            result['storage_error'] = state['storage_error']
            result['storage_error_code'] = state['storage_error_code']
            result['storage'] = storage_status(state)
            return result
        except (OSError, sqlite3.Error, ValueError, TypeError, KeyError) as exc:
            raise HTTPException(503, storage_message(storage_error_code(exc))) from None

    @app.post('/api/battery-capacity/reset')
    async def reset_battery_capacity(request: Request):
        if (request.headers.get('x-ups-capacity') != '1'
                or request.headers.get('content-type', '').split(';', 1)[0].strip().lower() != 'application/json'):
            raise HTTPException(403, '请通过面板重新建立参考。')
        origin = request.headers.get('origin')
        if origin is not None:
            try:
                parsed = urlsplit(origin)
            except ValueError:
                raise HTTPException(403, '浏览器来源无效。') from None
            if (parsed.scheme not in ('http', 'https') or parsed.username is not None
                    or parsed.path not in ('', '/') or parsed.query or parsed.fragment
                    or parsed.netloc.casefold() != request.headers.get('host', '').casefold()):
                raise HTTPException(403, '不允许跨站重新建立参考。')
        if request.headers.get('sec-fetch-site') in ('cross-site', 'same-site'):
            raise HTTPException(403, '请从当前面板页面重新建立参考。')
        encoded = bytearray()
        async for part in request.stream():
            encoded.extend(part)
            if len(encoded) > 1024:
                raise HTTPException(413, '参考请求过大。')
        try:
            payload = json.loads(encoded)
            if (not isinstance(payload, dict) or set(payload) != {'expected_epoch_id'}
                    or payload['expected_epoch_id'] is not None and
                    (not isinstance(payload['expected_epoch_id'], str) or len(payload['expected_epoch_id']) > 128)):
                raise ValueError('invalid_request')
        except (ValueError, TypeError, RecursionError):
            raise HTTPException(400, '参考请求无效，请刷新页面。') from None
        try:
            if state['storage_error']:
                raise HTTPException(503, state['storage_error'])
            return await asyncio.to_thread(store().reset_capacity_reference,
                                           lambda: load_snapshot(snapshot), payload['expected_epoch_id'])
        except ValueError as exc:
            if str(exc) == 'reference_changed':
                raise HTTPException(409, '参考已在其他页面改变，请刷新后重试。') from None
            raise HTTPException(409, '需要新鲜、有效的电池放电校准配置才能建立参考。') from None
        except (OSError, sqlite3.Error, TypeError, KeyError):
            raise HTTPException(503, '参考保存失败，原参考已保留。') from None

    @app.get('/api/export.csv')
    def export(hours: int = Query(24, ge=1, le=8760)):
        rows = history(hours)['points']
        metrics = [*METRICS, 'cell_1', 'cell_2', 'cell_3', 'cell_4']
        extrema = [f'{metric}_{edge}' for metric in metrics for edge in ('min', 'max')]
        fields = ['timestamp', 'mode', 'count', *metrics,
                  'power_quality', *CONTEXT_FIELDS, 'provenance', *extrema,
                  'first', 'last', 'bucket_start', 'bucket_end', 'partial_range']
        output = io.StringIO()
        writer = csv.DictWriter(output, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            context = dict(row['context'])
            if isinstance(context.get('calibration_coefficients'), dict):
                context['calibration_coefficients'] = json.dumps(context['calibration_coefficients'], sort_keys=True, separators=(',', ':'))
            ranges = {f'{metric}_{edge}': row[edge][metric]
                      for metric in metrics for edge in ('min', 'max') if metric in row[edge]}
            bounds = {key: row[key] for key in ('first', 'last', 'bucket_start', 'bucket_end', 'partial_range')}
            writer.writerow({'timestamp': row['timestamp'], 'mode': row['mode'], 'count': row['count'], **row['values'],
                             **context, **ranges, **bounds,
                             'power_quality': 'rail18_times_current24_hypothesis_v2' if 'dc_power_estimate_w' in row['values'] else 'legacy_protocol_estimate_uncalibrated'})
        return Response('\ufeff' + output.getvalue(), media_type='text/csv; charset=utf-8',
                        headers={'Content-Disposition': 'attachment; filename="us3000-history.csv"'})

    if (static / 'assets').exists():
        app.mount('/assets', StaticFiles(directory=static / 'assets'), name='assets')

    @app.get('/')
    def index():
        if not (static / 'index.html').exists():
            raise HTTPException(503, 'Frontend build missing')
        return FileResponse(static / 'index.html')

    return app


app = create_app()
