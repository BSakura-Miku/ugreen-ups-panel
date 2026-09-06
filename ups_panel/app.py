import asyncio
from contextlib import asynccontextmanager
import csv
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
from .power import finite_number
from .calibration import (CalibrationError, DEFAULT_COEFFICIENTS, default_config,
                          load_config, normalize_config, save_config)

LOG = logging.getLogger('panel')


def load_snapshot(path, now=None):
    now = time.time() if now is None else now
    try:
        with Path(path).open('rb') as source:
            encoded = source.read(65537)
        if len(encoded) > 65536:
            raise ValueError('Snapshot too large')
        def reject_constant(value):
            raise ValueError(f'Invalid JSON number: {value}')
        def parse_number(value):
            number = float(value)
            if not finite_number(number):
                raise ValueError('Non-finite JSON number')
            return number
        value = json.loads(encoded, parse_constant=reject_constant, parse_float=parse_number)
        if not isinstance(value, dict) or type(value.get('schema')) is not int or value['schema'] != 1:
            raise ValueError('Unknown snapshot schema')
        if not finite_number(value.get('heartbeat')):
            raise ValueError('Invalid heartbeat')
        sample = value.get('sample')
        if sample is not None:
            if (not isinstance(sample, dict) or sample.get('mode') not in ('online', 'charging', 'battery', 'unknown')
                    or not finite_number(sample.get('timestamp')) or sample['timestamp'] < 0
                    or not isinstance(sample.get('cells'), list) or len(sample['cells']) != 4):
                raise ValueError('Invalid sample')
            if (not finite_number(sample.get('soc')) or not 0 <= sample['soc'] <= 100
                    or any(not finite_number(v) or not 1 <= v <= 5 for v in sample['cells'])):
                raise ValueError('Invalid battery field')
            for key in METRICS:
                if sample.get(key) is not None and not finite_number(sample[key]):
                    raise ValueError('Invalid numeric field')
            for key in ('formula_version', 'decoder_version'):
                if key in sample and (type(sample[key]) is not int or sample[key] < 1):
                    raise ValueError('Invalid sample version')
            for key in set(CONTEXT_FIELDS) - {'formula_version', 'decoder_version', 'calibration_coefficients'}:
                if sample.get(key) is not None and (not isinstance(sample[key], str) or len(sample[key]) > 128):
                    raise ValueError('Invalid calibration metadata')
            if 'calibration_coefficients' in sample:
                config = normalize_config({'schema': 1, 'profile': sample.get('calibration_profile'),
                                           'coefficients': sample['calibration_coefficients']})
                if sample.get('calibration_revision') != config['revision']:
                    raise ValueError('Calibration revision does not match coefficients')
            if (not isinstance(sample.get('warnings', []), list)
                    or any(not isinstance(warning, str) for warning in sample.get('warnings', []))):
                raise ValueError('Invalid warnings')
        age = now - sample['timestamp'] if sample else None
        heartbeat_age = now - value['heartbeat']
        value.update(fresh=sample is not None and 0 <= age <= 10 and 0 <= heartbeat_age <= 10,
                     age_sec=round(max(0, age), 1) if age is not None else None,
                     server_time=now)
        nut = value.get('nut')
        if (not isinstance(nut, dict) or not isinstance(nut.get('available', False), bool)
                or not finite_number(nut.get('timestamp', 0))):
            value['nut'] = {'available': False}
        return value
    except (OSError, ValueError, TypeError, KeyError, RecursionError) as exc:
        return {'fresh': False, 'sample': None, 'age_sec': None, 'server_time': now,
                'source': 'unavailable', 'nut': {'available': False},
                'diagnostics': {'error': '尚未收到有效采集数据'}, 'read_error': type(exc).__name__}


def create_app(snapshot=None, database=None, static=None, calibration=None):
    snapshot = snapshot or os.getenv('UPS_SNAPSHOT', '/run/ugreen-ups-panel/latest.json')
    database = database or os.getenv('UPS_DATABASE', '/data/history.sqlite')
    static = Path(static or os.getenv('UPS_STATIC', 'frontend/dist'))
    calibration = Path(calibration or os.getenv('UPS_CALIBRATION_CONFIG') or Path(database).with_name('calibration.json'))
    calibration_lock = asyncio.Lock()
    state = {'store': None, 'storage_error': None, 'last_success': 0}

    async def record():
        while True:
            try:
                if state['store'] is None:
                    state['store'] = await asyncio.to_thread(Store, database)
                await asyncio.to_thread(state['store'].ingest, load_snapshot(snapshot))
                state['storage_error'] = None
                state['last_success'] = time.time()
            except (OSError, sqlite3.Error, ValueError, TypeError, KeyError) as exc:
                state['storage_error'] = '历史记录暂不可用'
                LOG.warning('History writer: %s', exc)
            await asyncio.sleep(2)

    @asynccontextmanager
    async def lifespan(app):
        task = asyncio.create_task(record())
        yield
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        if state['store']:
            try:
                await asyncio.to_thread(state['store'].flush)
            except (OSError, sqlite3.Error):
                LOG.exception('History flush failed')

    app = FastAPI(title='US3000 监控面板', lifespan=lifespan, docs_url=None, redoc_url=None)

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
    def live():
        view = load_snapshot(snapshot)
        view['storage_error'] = state['storage_error']
        view['storage_dropped_buckets'] = state['store'].dropped_buckets if state['store'] else 0
        nut = view.setdefault('nut', {})
        nut['fresh'] = bool(nut.get('available') and 0 <= time.time() - nut.get('timestamp', 0) <= 45)
        return view

    @app.get('/api/health')
    def health():
        return {'service': 'ok', 'capture_fresh': load_snapshot(snapshot)['fresh'],
                'storage_error': state['storage_error'],
                'storage_dropped_buckets': state['store'].dropped_buckets if state['store'] else 0}

    def calibration_status():
        view = load_snapshot(snapshot)
        metadata = view.get('calibration')
        metadata = metadata if isinstance(metadata, dict) else {}
        active = None
        error = None
        try:
            if metadata.get('config') is not None:
                active = normalize_config(metadata['config'])
            else:
                profile = (view.get('sample') or {}).get('calibration_profile')
                if profile in ('none', 'local-19v-v1'):
                    active = default_config(profile)
        except CalibrationError:
            error = '采集器的校准配置无效，请检查采集器。'
        try:
            desired = load_config(calibration) or active or default_config()
        except CalibrationError:
            desired = active or default_config()
            error = '校准配置文件无法读取，可重新保存有效配置。'
        if metadata.get('error'):
            error = '采集器未能读取新配置，仍保留上一次有效配置。'
        ready = bool(view['fresh'] and metadata.get('configurable') is True and active
                     and (view.get('sample') or {}).get('calibration_revision') == active['revision'])
        return {'schema': 1, 'defaults': DEFAULT_COEFFICIENTS, 'desired': desired,
                'active': active, 'collector_ready': ready,
                'pending': active is None or desired['revision'] != active['revision'],
                'error': error}

    @app.get('/api/calibration')
    def get_calibration():
        return calibration_status()

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
        encoded = bytearray()
        async for part in request.stream():
            encoded.extend(part)
            if len(encoded) > 4096:
                raise HTTPException(413, '校准配置过大。')
        try:
            payload = json.loads(encoded)
            if not isinstance(payload, dict) or set(payload) != {'profile', 'coefficients', 'expected_revision'}:
                raise CalibrationError('Invalid calibration request')
            if not isinstance(payload['expected_revision'], str):
                raise CalibrationError('Invalid revision')
            config = normalize_config({'schema': 1, 'profile': payload['profile'],
                                       'coefficients': payload['coefficients']})
        except (ValueError, TypeError, RecursionError, OverflowError):
            raise HTTPException(400, '校准配置无效：交流基底和电池放电系数须大于 0 且不超过 10，回充补偿须在 0–10 之间。') from None
        async with calibration_lock:
            current = calibration_status()
            if not current['collector_ready']:
                raise HTTPException(503, '需要已更新并正常采集的宿主机采集器，请等待连接或更新采集器。')
            if payload['expected_revision'] != current['desired']['revision']:
                raise HTTPException(409, '配置已在其他页面改变，请重新载入后再保存。')
            try:
                await asyncio.to_thread(save_config, calibration, config)
            except (CalibrationError, OSError):
                raise HTTPException(503, '校准配置保存失败，请检查数据目录是否可写。') from None
            return calibration_status()

    def store():
        if state['store'] is None or state['storage_error']:
            raise HTTPException(503, '历史记录暂不可用')
        return state['store']

    @app.get('/api/history')
    def history(hours: int = Query(24, ge=1, le=2160)):
        try:
            return store().history(hours)
        except (sqlite3.Error, ValueError, TypeError, KeyError):
            raise HTTPException(503, '历史查询失败')

    @app.get('/api/events')
    def events():
        try:
            return store().events()
        except (sqlite3.Error, ValueError, TypeError, KeyError):
            raise HTTPException(503, '事件查询失败')

    @app.get('/api/export.csv')
    def export(hours: int = Query(24, ge=1, le=2160)):
        rows = history(hours)['points']
        fields = ['timestamp', 'mode', 'count', *METRICS, 'cell_1', 'cell_2', 'cell_3', 'cell_4',
                  'power_quality', *CONTEXT_FIELDS, 'provenance']
        output = io.StringIO()
        writer = csv.DictWriter(output, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            context = dict(row['context'])
            if isinstance(context.get('calibration_coefficients'), dict):
                context['calibration_coefficients'] = json.dumps(context['calibration_coefficients'], sort_keys=True, separators=(',', ':'))
            writer.writerow({'timestamp': row['timestamp'], 'mode': row['mode'], 'count': row['count'], **row['values'],
                             **context,
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
