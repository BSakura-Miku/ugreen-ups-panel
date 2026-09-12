"""Same-origin settings and read-only energy/timeline routes."""
import json
import sqlite3
from urllib.parse import urlsplit

from fastapi import HTTPException, Query, Request


async def mutation_body(request):
    if (request.headers.get('x-ups-settings') != '1'
            or request.headers.get('content-type', '').split(';')[0].strip().lower() != 'application/json'
            or request.headers.get('sec-fetch-site') in ('cross-site', 'same-site')):
        raise HTTPException(403, '请从当前面板提交设置。')
    origin = request.headers.get('origin')
    if origin:
        try:
            parsed = urlsplit(origin)
            if (parsed.scheme not in ('http', 'https') or parsed.username is not None or parsed.password is not None
                    or parsed.path not in ('', '/') or parsed.query or parsed.fragment
                    or parsed.netloc.casefold() != request.headers.get('host', '').casefold()):
                raise ValueError()
        except ValueError:
            raise HTTPException(403, '浏览器来源无效。') from None
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > 4096:
            raise HTTPException(413, '设置内容过大。')
    try:
        value = json.loads(body)
        if not isinstance(value, dict):
            raise ValueError()
        return value
    except ValueError:
        raise HTTPException(400, '设置格式无效。') from None


def install_analysis_routes(app, store):
    import asyncio

    @app.get('/api/energy-usage/analysis')
    def analysis(month: str = Query(None, min_length=7, max_length=7, pattern=r'^\d{4}-(0[1-9]|1[0-2])$')):
        try:
            return store().usage_analysis(month)
        except ValueError:
            raise HTTPException(422, '月份或记录无效。') from None
        except (OSError, sqlite3.Error):
            raise HTTPException(503, '用电分析暂不可用。') from None

    @app.post('/api/energy-usage/tariffs')
    async def tariff(request: Request):
        value = await mutation_body(request)
        try:
            return await asyncio.to_thread(store().add_tariff, value)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None
        except (OSError, sqlite3.Error):
            raise HTTPException(503, '电价暂未保存，请重新读取后再试。') from None

    @app.get('/api/timeline')
    def events(limit: int = Query(100, ge=1, le=200)):
        try:
            return store().timeline(limit)
        except (OSError, sqlite3.Error):
            raise HTTPException(503, '事件暂不可用。') from None

    @app.put('/api/timeline/{event_id}/note')
    async def note(event_id: str, request: Request):
        value = await mutation_body(request)
        if set(value) != {'note'}:
            raise HTTPException(400, '备注格式无效。')
        try:
            return await asyncio.to_thread(store().event_note, event_id, value['note'])
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None
        except (OSError, sqlite3.Error):
            raise HTTPException(503, '备注暂未保存，请重新读取后再试。') from None
