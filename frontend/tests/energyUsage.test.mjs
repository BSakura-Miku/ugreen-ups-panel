import assert from 'node:assert/strict';
import { test } from 'node:test';
import {
  energyCalendarCells, energyCalendarFocus, energyCoverage, energyDayState, energyDuration, energyHeatLevel,
  energyKwh, energyLocalDate, energyMonthDays, energyPower, energyUsageNotice, parseEnergyUsageDay,
  parseEnergyUsageMonth, readEnergyUsageDay, readEnergyUsageMonth, shiftEnergyMonth, validEnergyDate, validEnergyMonth,
} from '../src/energyUsageDisplay.ts';

const CURRENT = '2026-09-09';
const NOW = Date.parse('2026-09-09T12:00:00+08:00') / 1000;
const metadata = () => ({ schema: 1, timezone: 'Asia/Shanghai', utc_offset: '+08:00', generated_at: NOW,
  current_date: CURRENT, capture_fresh: true, storage_error: null, dropped_intervals: 0 });
function day(date, overrides = {}) {
  return { date, status: date > CURRENT ? 'future' : 'no_data', estimate_kwh: null, covered_sec: 0,
    expected_sec: date > CURRENT ? 0 : date === CURRENT ? 43200 : 86400, coverage_ratio: date > CURRENT ? null : 0,
    average_power_w: null, basis_count: 0, ...overrides };
}
function month(overrides = {}) {
  const days = Array.from({ length: 30 }, (_, index) => day(`2026-09-${String(index + 1).padStart(2, '0')}`));
  return { ...metadata(), month: '2026-09', tracking_started_at: null, first_month: null, last_recorded_at: null,
    reason: null, today: days[8], summary: { estimate_kwh: null, covered_sec: 0, expected_sec: 734400,
      coverage_ratio: 0, complete_days: 0, complete_day_average_kwh: null, recorded_days: 0, basis_count: 0 }, days, ...overrides };
}
function observedMonth() {
  const value = month();
  value.days[0] = day('2026-09-01', { status: 'complete', estimate_kwh: 1.44, covered_sec: 86400, coverage_ratio: 1, average_power_w: 60, basis_count: 1 });
  value.days[1] = day('2026-09-02', { status: 'partial', estimate_kwh: .6, covered_sec: 36000, coverage_ratio: 36000 / 86400, average_power_w: 60, basis_count: 1 });
  value.days[3] = day('2026-09-04', { status: 'complete', estimate_kwh: 0, covered_sec: 86400, coverage_ratio: 1, average_power_w: 0, basis_count: 1 });
  value.days[8] = day(CURRENT, { status: 'today', estimate_kwh: .06, covered_sec: 3600, coverage_ratio: 1 / 12, average_power_w: 60, basis_count: 1 });
  value.today = value.days[8];
  value.tracking_started_at = NOW - 9 * 86400; value.last_recorded_at = NOW; value.first_month = '2026-08';
  value.summary = { estimate_kwh: 2.1, covered_sec: 212400, expected_sec: 734400, coverage_ratio: 212400 / 734400,
    complete_days: 2, complete_day_average_kwh: .72, recorded_days: 4, basis_count: 2 };
  return value;
}
function detail() {
  const start = Date.parse(`${CURRENT}T00:00:00+08:00`) / 1000;
  const hours = Array.from({ length: 24 }, (_, hour) => ({ hour, start_ts: start + hour * 3600, end_ts: start + (hour + 1) * 3600,
    estimate_kwh: null, covered_sec: 0, expected_sec: hour < 12 ? 3600 : 0,
    coverage_ratio: hour < 12 ? 0 : null, average_power_w: null, basis_count: 0 }));
  Object.assign(hours[0], { estimate_kwh: .06, covered_sec: 3600, coverage_ratio: 1, average_power_w: 60, basis_count: 1 });
  Object.assign(hours[1], { estimate_kwh: 0, covered_sec: 3600, coverage_ratio: 1, average_power_w: 0, basis_count: 1 });
  return { ...metadata(), date: CURRENT,
    day: day(CURRENT, { status: 'today', estimate_kwh: .06, covered_sec: 7200, coverage_ratio: 1 / 6, average_power_w: 30, basis_count: 1 }),
    hours, bases: [{ id: 'basis1', profile: 'custom', revision: 'test', estimate_kwh: .06, covered_sec: 7200, source: 'replay', ac_model: 'test' }] };
}

test('calendar dates use the report day and fixed Beijing fallback, including leap days and year boundaries', () => {
  assert.equal(energyLocalDate(new Date('2026-09-08T15:59:59Z')), '2026-09-08');
  assert.equal(energyLocalDate(new Date('2026-09-08T16:00:00Z')), '2026-09-09');
  assert.equal(energyMonthDays('2024-02'), 29); assert.equal(energyMonthDays('2026-02'), 28);
  assert.equal(shiftEnergyMonth('2026-01', -1), '2025-12');
  assert.equal(shiftEnergyMonth('2026-12', 1), '2027-01');
  assert.equal(validEnergyDate('2024-02-29'), true); assert.equal(validEnergyDate('2026-02-29'), false);
  for (const invalid of ['2026-00', '2026-13', '2026-9', '1969-12', '9999-01', '../2026-09']) assert.equal(validEnergyMonth(invalid), false);
  const cells = energyCalendarCells('2026-09');
  assert.equal(cells.length, 35); assert.deepEqual(cells.slice(0, 3), [null, '2026-09-01', '2026-09-02']);
  assert.equal(cells.filter(Boolean).length, 30); assert.equal(cells.at(-1), null);
});

test('calendar keyboard movement follows seven-day rows and clamps at the month and today', () => {
  assert.equal(energyCalendarFocus('2026-09-08', 'ArrowUp', '2026-09', CURRENT), '2026-09-01');
  assert.equal(energyCalendarFocus('2026-09-09', 'ArrowRight', '2026-09', CURRENT), CURRENT);
  assert.equal(energyCalendarFocus('2026-09-01', 'Home', '2026-09', CURRENT), '2026-09-01');
  assert.equal(energyCalendarFocus('2026-09-08', 'Home', '2026-09', CURRENT), '2026-09-07');
  assert.equal(energyCalendarFocus('2026-09-08', 'End', '2026-09', CURRENT), CURRENT);
  assert.equal(energyCalendarFocus('2026-08-29', 'ArrowDown', '2026-08', CURRENT), '2026-08-31');
  for (const key of ['Enter', 'Tab', 'constructor', '__proto__']) assert.equal(energyCalendarFocus(CURRENT, key, '2026-09', CURRENT), null);
});

test('no data, tiny positive estimates and confirmed zero remain distinct in numbers and heat', () => {
  for (const missing of [null, undefined, NaN, Infinity, -1]) {
    assert.equal(energyKwh(missing), '—'); assert.equal(energyPower(missing), '—');
  }
  assert.equal(energyKwh(.00004), '<0.001'); assert.equal(energyKwh(.001), '0.001');
  assert.equal(energyKwh(0), '0.000'); assert.equal(energyPower(0), '0.0');
  assert.equal(energyHeatLevel(day('2026-09-01'), 1), 0);
  assert.equal(energyHeatLevel(day('2026-09-10'), 1), 0);
  const zero = day('2026-09-01', { status: 'complete', estimate_kwh: 0, covered_sec: 86400, average_power_w: 0 });
  assert.equal(energyHeatLevel(zero, 0), 1);
  assert.equal(energyHeatLevel({ ...zero, estimate_kwh: 1 }, 1), 5);
});

test('coverage never rounds an actual gap into 100 percent and small coverage never becomes zero', () => {
  assert.equal(energyCoverage(1), '100%'); assert.equal(energyCoverage(.99999), '>99.9%');
  assert.equal(energyCoverage(.00001), '<0.1%'); assert.equal(energyCoverage(0), '0%');
  assert.equal(energyCoverage(null), '—'); assert.equal(energyCoverage(1.1), '—');
  assert.equal(energyDuration(.1), '<1秒'); assert.equal(energyDuration(0), '0秒');
  assert.equal(energyDuration(3660), '1小时1分');
  assert.equal(energyDuration(3598), '59分58秒');
  assert.equal(energyDuration(86398), '23小时59分58秒');
  assert.equal(energyDuration(86400), '24小时');
});

test('month parsing retains server complete-day mean without counting today or partial dates', () => {
  const value = observedMonth(), original = structuredClone(value);
  const parsed = parseEnergyUsageMonth(value);
  assert.ok(parsed); assert.equal(parsed.summary.complete_days, 2);
  assert.equal(parsed.summary.complete_day_average_kwh, .72);
  assert.equal(energyKwh(parsed.summary.complete_day_average_kwh), '0.720');
  assert.equal(energyDayState(parsed.today, CURRENT), '今天 · 截至当前');
  assert.equal(energyDayState(day(CURRENT), CURRENT), '今天 · 暂无记录');
  assert.equal(parsed.days[9].status, 'future'); assert.equal(parsed.days[9].estimate_kwh, null);
  assert.deepEqual(value, original);
  assert.ok(parseEnergyUsageMonth(month({ reason: 'not_configured' })));
});

test('unknown schemas, incomplete calendars and invented zero or complete-day averages are rejected', () => {
  const value = observedMonth();
  for (const invalid of [null, {}, { ...value, schema: 2 }, { ...value, days: value.days.slice(1) },
    { ...value, days: [value.days[0], ...value.days.slice(0, -1)] }, { ...value, dropped_intervals: -1 },
    { ...value, today: { ...value.today, status: 'complete' } },
    { ...value, summary: { ...value.summary, complete_days: 0, complete_day_average_kwh: 0 } },
    { ...month(), summary: { ...month().summary, estimate_kwh: 0 } },
    { ...value, summary: { ...value.summary, estimate_kwh: Infinity } }]) assert.equal(parseEnergyUsageMonth(invalid), null);
});

test('hour detail preserves confirmed battery zero, leaves missing hours null and requires all 24 bins', () => {
  const value = detail(), parsed = parseEnergyUsageDay(value);
  assert.ok(parsed); assert.equal(parsed.hours[0].average_power_w, 60);
  assert.equal(parsed.hours[1].average_power_w, 0); assert.equal(parsed.hours[1].estimate_kwh, 0);
  assert.equal(parsed.hours[2].average_power_w, null); assert.equal(parsed.hours[12].expected_sec, 0);
  assert.equal(parseEnergyUsageDay({ ...value, hours: value.hours.slice(0, 23) }), null);
  assert.equal(parseEnergyUsageDay({ ...value, hours: [value.hours[0], ...value.hours.slice(0, -1)] }), null);
  const falseZero = structuredClone(value); falseZero.hours[2].estimate_kwh = 0;
  assert.equal(parseEnergyUsageDay(falseZero), null);
  const wrongDay = structuredClone(value); wrongDay.day.date = '2026-09-08';
  assert.equal(parseEnergyUsageDay(wrongDay), null);
});

test('recording failures and stale capture keep a readable recorded-only notice without exposing raw errors', () => {
  assert.equal(energyUsageNotice(month()), null);
  assert.match(energyUsageNotice(month({ storage_error: 'private raw exception' })), /已记录累计/);
  assert.doesNotMatch(energyUsageNotice(month({ storage_error: 'private raw exception' })), /private raw exception/);
  assert.match(energyUsageNotice(month({ dropped_intervals: 1 })), /缺口未补算/);
  assert.match(energyUsageNotice(month({ capture_fresh: false })), /上次已记录/);
  assert.match(energyUsageNotice(month({ reason: 'not_configured' })), /配置市电功率估算/);
  assert.match(energyUsageNotice(month({ reason: 'charge_not_configured' })), /回充尚未校准，充电时段暂不能累计/);
});

test('reads use bounded same-origin endpoints and reject a response from the wrong month or date', async t => {
  const requests = [];
  t.mock.method(globalThis, 'fetch', async (url, options) => {
    requests.push({ url, options });
    return Response.json(url.includes('/day?') ? detail() : month());
  });
  const signal = new AbortController().signal;
  await readEnergyUsageMonth(null, signal);
  await readEnergyUsageMonth('2026-09', signal);
  await readEnergyUsageDay(CURRENT, signal);
  assert.deepEqual(requests.map(item => item.url), ['/api/energy-usage', '/api/energy-usage?month=2026-09', '/api/energy-usage/day?date=2026-09-09']);
  assert.ok(requests.every(item => item.options.cache === 'no-store' && item.options.mode === 'same-origin' && item.options.redirect === 'error'));
  await assert.rejects(readEnergyUsageMonth('2026-08', signal), /month_mismatch/);
  await assert.rejects(readEnergyUsageDay('2026-09-08', signal), /date_mismatch/);
  const calls = requests.length;
  await assert.rejects(readEnergyUsageMonth('2026-13', signal), /invalid_energy_month/);
  await assert.rejects(readEnergyUsageDay('2026-02-29', signal), /invalid_energy_date/);
  assert.equal(requests.length, calls);
});

test('an aborted old date cannot return a usable late result even when a transport ignores cancellation', async t => {
  let finish, requestSignal;
  t.mock.method(globalThis, 'fetch', (_url, options) => {
    requestSignal = options.signal;
    return new Promise(resolve => { finish = resolve; });
  });
  const parent = new AbortController();
  const pending = readEnergyUsageDay(CURRENT, parent.signal);
  parent.abort(); assert.equal(requestSignal.aborted, true);
  finish(Response.json(detail()));
  await assert.rejects(pending, error => error.name === 'AbortError');
  await assert.rejects(readEnergyUsageDay(CURRENT, parent.signal), error => error.name === 'AbortError');
});

test('eight-second timeout cancels its transport, clears the timer and leaves the owner eligible to retry', async t => {
  let timeoutCallback, delay, cleared = 0, requestSignal;
  t.mock.method(globalThis, 'setTimeout', (callback, milliseconds) => { timeoutCallback = callback; delay = milliseconds; return 41; });
  t.mock.method(globalThis, 'clearTimeout', id => { assert.equal(id, 41); cleared++; });
  t.mock.method(globalThis, 'fetch', (_url, options) => {
    requestSignal = options.signal;
    return new Promise((_resolve, reject) => requestSignal.addEventListener('abort', () => reject(new DOMException('timeout', 'AbortError'))));
  });
  const parent = new AbortController();
  const pending = readEnergyUsageMonth(null, parent.signal);
  assert.equal(delay, 8000); timeoutCallback();
  await assert.rejects(pending, error => error.name === 'AbortError');
  assert.equal(requestSignal.aborted, true); assert.equal(parent.signal.aborted, false); assert.equal(cleared, 1);
});

test('HTTP failures are errors, not zero or successful empty ledgers', async t => {
  t.mock.method(globalThis, 'fetch', async () => new Response('private backend error', { status: 503 }));
  await assert.rejects(readEnergyUsageMonth(null, new AbortController().signal), error => error.message === 'energy_usage_unavailable');
});
