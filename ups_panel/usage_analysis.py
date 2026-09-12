"""Comparable complete-hour energy periods and effective-dated single tariffs."""
import calendar
from datetime import datetime, timedelta, date
from decimal import Decimal, InvalidOperation
import re

from .energy_usage import TZ, _start, _month


def initialize(db):
    db.execute('''CREATE TABLE IF NOT EXISTS energy_tariffs (
        effective_date TEXT PRIMARY KEY, rate_micros INTEGER NOT NULL,
        currency TEXT NOT NULL, created_at REAL NOT NULL)''')


def tariffs(db):
    return [dict(zip(('effective_date', 'rate', 'currency'), row)) for row in db.execute(
        'SELECT effective_date, rate_micros / 1000000.0, currency FROM energy_tariffs ORDER BY effective_date')]


def add_tariff(db, value, now):
    if not isinstance(value, dict) or set(value) != {'effective_date', 'rate', 'currency'}:
        raise ValueError('请填写生效日期、单一电价和币种。')
    try:
        day = date.fromisoformat(value['effective_date'])
        if value['effective_date'] != day.isoformat() or not 1970 <= day.year <= 9998:
            raise ValueError()
        rate = Decimal(str(value['rate']))
        if not rate.is_finite() or not 0 <= rate <= 10000 or rate * 1000000 != (rate * 1000000).to_integral_value():
            raise ValueError()
        currency = value['currency']
        if not isinstance(currency, str) or not re.fullmatch('[A-Z]{3}', currency):
            raise ValueError()
    except (ValueError, TypeError, InvalidOperation):
        raise ValueError('电价应为 0–10000、最多六位小数，币种使用三个大写字母。') from None
    existing = tariffs(db)
    if len(existing) >= 1000:
        raise ValueError('电价记录已达上限。')
    if existing and (day < datetime.fromtimestamp(now, TZ).date() or value['effective_date'] <= existing[-1]['effective_date']):
        raise ValueError('已有电价保留不变；新电价须从今天或未来、且晚于最后一条记录的日期生效。')
    db.execute('INSERT INTO energy_tariffs VALUES(?,?,?,?)', (day.isoformat(), int(rate * 1000000), currency, now))
    return tariffs(db)


def period(usage, db, start, end, now):
    rows, overlap = usage._rows(db, start, end, now)
    rows = [row for row in rows if row['last_ts'] <= end]
    result = usage._totals(rows, max(0, end - start))
    result.update(start=start, end=end, basis_ids=sorted({row['basis_id'] for row in rows}), cutoff_overlap=overlap)
    return result


def compare(usage, db, key, current_start, previous_start, duration, now):
    current = period(usage, db, current_start, current_start + duration, now)
    previous = period(usage, db, previous_start, previous_start + duration, now)
    reason = ('insufficient_coverage' if duration <= 0 or any((item['coverage_ratio'] or 0) < .99 or item['cutoff_overlap'] for item in (current, previous))
              else 'basis_changed' if len(current['basis_ids']) != 1 or current['basis_ids'] != previous['basis_ids']
              else None)
    delta = current['estimate_kwh'] - previous['estimate_kwh'] if reason is None else None
    return {'key': key, 'current': current, 'previous': previous, 'reason': reason, 'delta_kwh': delta,
            'change_percent': delta / previous['estimate_kwh'] * 100 if delta is not None and previous['estimate_kwh'] > 0 else None}


def analysis(usage, db, month, now):
    today = datetime.fromtimestamp(now, TZ).date()
    day_start = _start(today)
    cutoff = int(now // 3600) * 3600
    week = today - timedelta(days=today.weekday())
    first = _month(month or today.strftime('%Y-%m'))
    prior = (first - timedelta(days=1)).replace(day=1)
    month_duration = max(0, min(cutoff - _start(first), calendar.monthrange(first.year, first.month)[1] * 86400,
                                calendar.monthrange(prior.year, prior.month)[1] * 86400))
    comparisons = [compare(usage, db, 'day', day_start, day_start - 86400, cutoff - day_start, now),
                   compare(usage, db, 'week', _start(week), _start(week - timedelta(days=7)), cutoff - _start(week), now),
                   compare(usage, db, 'month', _start(first), _start(prior), month_duration, now)]
    prices = tariffs(db)
    monthly = usage.month(db, first.strftime('%Y-%m'), now)
    costs, missing = [], 0.0
    for day in monthly['days']:
        price = next((item for item in reversed(prices) if item['effective_date'] <= day['date']), None)
        amount = day['estimate_kwh']
        cost = amount * price['rate'] if amount is not None and price else None
        if amount is not None and not price:
            missing += amount
        costs.append({'date': day['date'], 'estimate_cost': cost, 'currency': price['currency'] if price else None,
                      'rate': price['rate'] if price else None, 'coverage_ratio': day['coverage_ratio']})
    totals = {}
    for item in costs:
        if item['estimate_cost'] is not None:
            totals[item['currency']] = totals.get(item['currency'], 0) + item['estimate_cost']
    return {'schema': 1, 'generated_at': now, 'timezone': 'Asia/Shanghai', 'month': first.strftime('%Y-%m'),
            'current_date': today.isoformat(), 'comparison_cutoff': cutoff, 'comparisons': comparisons,
            'tariffs': prices, 'costs': costs, 'cost_totals': totals, 'unpriced_kwh': missing,
            'coverage_ratio': monthly['summary']['coverage_ratio']}
