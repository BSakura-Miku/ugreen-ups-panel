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

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles

from .storage import CONTEXT_FIELDS, Store, METRICS
from .power import finite_number

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
            for key in set(CONTEXT_FIELDS) - {'formula_version', 'decoder_version'}:
                if sample.get(key) is not None and (not isinstance(sample[key], str) or len(sample[key]) > 128):
                    raise ValueError('Invalid calibration metadata')
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


def create_app(snapshot=None, database=None, static=None):
    snapshot = snapshot or os.getenv('UPS_SNAPSHOT', '/run/ugreen-ups-panel/latest.json')
    database = database or os.getenv('UPS_DATABASE', '/data/history.sqlite')
    static = Path(static or os.getenv('UPS_STATIC', 'frontend/dist'))
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
            writer.writerow({'timestamp': row['timestamp'], 'mode': row['mode'], 'count': row['count'], **row['values'],
                             **row['context'],
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
