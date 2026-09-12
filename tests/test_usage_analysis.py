from datetime import date
import importlib
import json
import sqlite3
import time

import pytest
from fastapi.testclient import TestClient

from ups_panel.energy_usage import _start
from ups_panel.storage import Store
from ups_panel import timeline


def hour(db, start, wh=100, coverage=3600, basis='one'):
    db.execute('INSERT INTO energy_usage_hours VALUES(?,?,?,?,?,?,?,?)',
               (start, basis, '{}', wh, coverage, start, start + coverage, max(1, int(coverage / 2))))


def test_comparison_uses_complete_matching_hours_and_never_scales_partial_bucket(tmp_path):
    store = Store(tmp_path / 'history.sqlite')
    today = _start(date(2026, 9, 13))
    with store.connect() as db:
        hour(db, today, wh=120)
        hour(db, today - 86400, wh=100)
        hour(db, today + 3600, wh=999, coverage=900)
    result = store.usage_analysis(now=today + 4500)
    comparison = result['comparisons'][0]
    assert comparison['current']['end'] == today + 3600
    assert comparison['current']['estimate_kwh'] == .12
    assert comparison['change_percent'] == pytest.approx(20)
    with store.connect() as db:
        db.execute('UPDATE energy_usage_hours SET basis_id=? WHERE hour_start=?', ('other', today - 86400))
    assert store.usage_analysis(now=today + 4500)['comparisons'][0]['reason'] == 'basis_changed'
    with store.connect() as db:
        db.execute('UPDATE energy_usage_hours SET covered_sec=100 WHERE hour_start=?', (today - 86400,))
    assert store.usage_analysis(now=today + 4500)['comparisons'][0]['delta_kwh'] is None


def test_effective_tariffs_preserve_history_split_currencies_and_report_unpriced_records(tmp_path):
    store = Store(tmp_path / 'history.sqlite')
    start = _start(date(2026, 9, 12))
    with store.connect() as db:
        for index in range(3):
            hour(db, start + index * 86400, wh=1000)
    store.add_tariff({'effective_date': '2026-09-13', 'rate': '0.5', 'currency': 'CNY'}, now=start + 86400)
    store.add_tariff({'effective_date': '2026-09-14', 'rate': '0.2', 'currency': 'USD'}, now=start + 86400)
    result = store.usage_analysis(now=start + 3 * 86400)
    assert result['cost_totals'] == {'CNY': .5, 'USD': .2}
    assert result['unpriced_kwh'] == 1
    with pytest.raises(ValueError):
        store.add_tariff({'effective_date': '2026-09-13', 'rate': 2, 'currency': 'CNY'}, now=start + 86400)
    reopened = Store(store.path).usage_analysis(now=start + 3 * 86400)
    assert reopened['cost_totals'] == result['cost_totals']


def test_month_length_is_common_and_empty_or_zero_period_does_not_divide(tmp_path):
    store = Store(tmp_path / 'history.sqlite')
    result = store.usage_analysis('2026-03', now=_start(date(2026, 4, 1)))
    item = result['comparisons'][2]
    assert item['current']['end'] - item['current']['start'] == 28 * 86400
    assert item['change_percent'] is None


def test_timeline_records_only_new_transitions_retains_legacy_time_and_saves_notes(tmp_path):
    store = Store(tmp_path / 'history.sqlite')
    with store.connect() as db:
        db.execute('INSERT INTO events(timestamp,kind,detail) VALUES(?,?,?)', (100, 'power', 'online:battery'))
        first = {'fresh': True, 'sample': {'timestamp': 100, 'calibration_revision': 'a' * 64},
                 'nut': {'available': True, 'timestamp': 100, 'values': {'ups.status': 'OL'}}}
        timeline.observe(db, first, 100)
        timeline.observe(db, first, 102)
        changed = {'fresh': True, 'sample': {'timestamp': 103, 'calibration_revision': 'b' * 64},
                   'nut': {'available': True, 'timestamp': 103, 'values': {'ups.status': 'OB LB'}}}
        timeline.observe(db, changed, 104)
    result = store.timeline()
    assert len(result) == 3
    assert next(item for item in result if item['kind'] == 'power')['occurred_at'] is None
    assert next(item for item in result if item['kind'] == 'calibration')['occurred_at'] == 103
    assert next(item for item in result if item['kind'] == 'nut')['detail'] == 'LB OB'
    store.event_note('legacy-1', '<b>人工备注</b>')
    assert Store(store.path).timeline()[-1]['note'] == '<b>人工备注</b>'
    with pytest.raises(ValueError):
        store.event_note('legacy-999', 'missing')


def test_settings_http_rejects_cross_origin_and_overlarge_bodies(tmp_path):
    app = importlib.import_module('ups_panel.app').create_app(tmp_path / 'snapshot.json', tmp_path / 'history.sqlite', tmp_path)
    with TestClient(app) as client:
        deadline = time.monotonic() + 3
        while client.get('/api/health').json()['storage']['state'] != 'ready':
            assert time.monotonic() < deadline
            time.sleep(.01)
        body = {'effective_date': '2026-09-13', 'rate': '0.5', 'currency': 'CNY'}
        route = '/api/energy-usage/tariffs'
        assert client.post(route, json=body).status_code == 403
        assert client.post(route, json=body, headers={'X-UPS-Settings':'1', 'Origin':'https://evil.invalid'}).status_code == 403
        assert client.post(route, content=' ' * 4097, headers={'X-UPS-Settings':'1','Content-Type':'application/json'}).status_code == 413
        assert client.post(route, json=body, headers={'X-UPS-Settings':'1'}).status_code == 200
        assert client.get('/api/energy-usage/analysis?month=2026-09').status_code == 200
        result = client.get('/api/timeline').json()
        event = next(item for item in result if item['kind'] == 'application')
        assert client.put('/api/timeline/' + event['id'] + '/note', json={'note':'测试'}, headers={'X-UPS-Settings':'1'}).status_code == 200
        assert client.get('/api/history?hours=2&end=100').status_code == 200
