import assert from 'node:assert/strict';
import { test } from 'node:test';
import { capacityComparisonValues, capacityDate, capacityIndexLabel, capacityNumber, capacityPresentation, capacityReasonLabel, parseBatteryCapacityReport } from '../src/batteryCapacityDisplay.ts';

const basis = { profile: 'custom', revision: 'test', battery_gain: 1.5, estimate_basis: 'test', source: 'usbmon',
  device: { serial: 'test-ups' }, decoder_version: 4, formula_version: 2 };
const timestamp = 1788771600;
function observation(changes = {}) {
  return { id: 'base', start_ts: timestamp, end_ts: timestamp + 300, start_soc: 90, end_soc: 80,
    estimate_wh: 4, duration_sec: 300, avg_power_w: 48, power_cv: .02, min_power_w: 46, max_power_w: 50,
    min_battery_voltage_v: 14.8, max_battery_voltage_v: 15.2, max_cell_delta_mv: 19,
    sample_count: 151, interval_count: 150, coverage_ratio: 1, accepted: true, reason: null,
    index_pct: 100, change_pct: 0, ...changes };
}
function report(changes = {}) {
  return { schema: 1, status: 'waiting_reference', reason: 'waiting_start', epoch: { id: 'phase1', activated_at: timestamp - 30, basis },
    current_basis: basis, baseline: null, comparison: null, progress: null, recent: [],
    criteria: { soc_start: 90, soc_end: 80, max_gap_sec: 5, load_tolerance: .1, max_power_cv: .15, trend_samples: 3, min_duration_sec: 60 },
    temperature_known: false, capture_fresh: true, sample_timestamp: timestamp + 300, generated_at: timestamp + 302, ...changes };
}
function compared(changes = {}) {
  const rows = [
    observation({ id: 'third', end_ts: timestamp + 3300, estimate_wh: 3.92, avg_power_w: 49, index_pct: 98, change_pct: -2 }),
    observation({ id: 'second', end_ts: timestamp + 2300, estimate_wh: 3.84, avg_power_w: 47, index_pct: 96, change_pct: -4 }),
    observation({ id: 'first', end_ts: timestamp + 1300, estimate_wh: 4.04, avg_power_w: 48, index_pct: 101, change_pct: 1 }),
  ];
  return report({ status: 'trend', reason: null, baseline: observation(),
    comparison: { sample_count: 3, index_pct: 98, change_pct: -2, latest_end_ts: rows[0].end_ts }, recent: rows, ...changes });
}

test('reference activation never fabricates the baseline before a complete observation', () => {
  const waiting = capacityPresentation(report());
  assert.equal(waiting.index, null);
  assert.equal(waiting.label, '基线建立中');
  assert.match(waiting.advice, /参考起点已固定/);
  const collecting = capacityPresentation(report({ status: 'collecting', progress: {
    start_ts: timestamp, current_soc: 86, completed_pp: 4, target_drop_pp: 10, estimate_wh: 1.6, duration_sec: 120 } }));
  assert.equal(collecting.index, null);
  assert.equal(collecting.showProgress, true);
  assert.equal(collecting.sampleCount, 0);
});

test('baseline is explicitly 100 while comparison count only includes later independent records', () => {
  const baseline = capacityPresentation(report({ status: 'reference_ready', reason: null, baseline: observation() }));
  assert.equal(baseline.index, 100);
  assert.equal(baseline.sampleCount, 0);
  assert.equal(baseline.isBaseline, true);
  assert.equal(baseline.label, '初步观察');
  assert.equal(baseline.observationTimestamp, timestamp + 300);
  const first = capacityPresentation(compared({ status: 'preliminary', comparison: {
    sample_count: 1, index_pct: 98, change_pct: -2, latest_end_ts: timestamp + 3300 } }));
  assert.equal(first.index, 98);
  assert.equal(first.isBaseline, false);
  assert.equal(first.sampleCount, 1);
  assert.equal(first.label, '初步观察');
  assert.equal(capacityPresentation(compared()).label, '可比较');
});

test('a new active interval does not erase or falsely graduate the confirmed reference', () => {
  const active = compared({ status: 'collecting', progress: {
    start_ts: timestamp + 5000, current_soc: 88, completed_pp: 2, target_drop_pp: 10, estimate_wh: .8, duration_sec: 60 } });
  const view = capacityPresentation(active);
  assert.equal(view.index, 98);
  assert.equal(view.showProgress, true);
  assert.equal(view.label, '可比较');
  active.comparison.sample_count = 2;
  assert.equal(capacityPresentation(active).label, '初步观察');
});

test('offline records retain only their dated historical interpretation and never a current progress bar', () => {
  const offline = compared({ capture_fresh: false, reason: 'stale', progress: {
    start_ts: timestamp + 5000, current_soc: 88, completed_pp: 2, target_drop_pp: 10, estimate_wh: .8, duration_sec: 60 } });
  const view = capacityPresentation(offline);
  assert.equal(view.index, 98);
  assert.equal(view.historical, true);
  assert.equal(view.observationTimestamp, timestamp + 3300);
  assert.equal(view.showProgress, false);
  assert.match(view.advice, /上次已完成/);
  assert.equal(capacityPresentation(report({ capture_fresh: false, reason: 'stale' })).index, null);
  assert.equal(capacityPresentation(compared({ reason: 'stale' })).historical, true);
});

test('API failure, incompatible basis and missing calibration hide otherwise valid historical main values', () => {
  for (const data of [compared({ status: 'incompatible', reason: 'context_changed' }),
    compared({ status: 'not_configured', reason: 'not_configured' }), compared({ reason: 'invalid_basis' })]) {
    const view = capacityPresentation(data);
    assert.equal(view.index, null); assert.equal(view.label, '暂不可比较');
    assert.equal(capacityComparisonValues(data), null);
  }
  const failed = capacityPresentation(compared(), true);
  assert.equal(failed.index, null); assert.equal(failed.observationTimestamp, null);
  assert.equal(failed.label, '暂不可比较'); assert.equal(failed.showProgress, false);
  assert.equal(capacityPresentation(null).index, null);
});

test('comparison values use the matching accepted records and median, leaving source rows unchanged', () => {
  const data = compared();
  data.recent.unshift(observation({ id: 'rejected', end_ts: timestamp + 4300, estimate_wh: 20, avg_power_w: 100,
    accepted: false, reason: 'load_mismatch', index_pct: null, change_pct: null }));
  const saved = structuredClone(data);
  assert.deepEqual(capacityComparisonValues(data), { estimateWh: 3.92, avgPowerW: 48, count: 3 });
  assert.deepEqual(data, saved);
  const two = compared({ status: 'preliminary', comparison: { sample_count: 2, index_pct: 97, change_pct: -3, latest_end_ts: timestamp + 3300 } });
  assert.deepEqual(capacityComparisonValues(two), { estimateWh: 3.88, avgPowerW: 48, count: 2 });
});

test('incomplete or mismatched summary rows never become invented Wh and load metrics', () => {
  assert.equal(capacityComparisonValues(compared({ recent: [] })), null);
  const inconsistent = compared(); inconsistent.comparison.latest_end_ts += 1;
  assert.equal(capacityComparisonValues(inconsistent), null);
  inconsistent.comparison.latest_end_ts -= 1; inconsistent.comparison.index_pct = 96;
  assert.equal(capacityComparisonValues(inconsistent), null);
  inconsistent.comparison.sample_count = 4;
  assert.equal(capacityComparisonValues(inconsistent), null);
  assert.equal(capacityComparisonValues(report({ baseline: observation() })), null);
});

test('only the supported bounded schema and numeric observation summaries are accepted', () => {
  const data = compared();
  assert.equal(parseBatteryCapacityReport(data), data);
  assert.ok(parseBatteryCapacityReport(report()));
  for (const input of [null, [], {}, { ...data, schema: 2 }, { ...data, status: 'future_status' },
    { ...data, recent: null }, { ...data, capture_fresh: 'true' }, { ...data, epoch: {} },
    { ...data, comparison: { ...data.comparison, sample_count: 4 } },
    { ...data, comparison: { ...data.comparison, index_pct: Infinity } },
    { ...data, baseline: observation({ index_pct: 99 }) },
    { ...data, baseline: null }, { ...data, recent: [observation({ index_pct: null })] },
    { ...data, recent: [observation({ estimate_wh: NaN })] },
    { ...data, criteria: { ...data.criteria, soc_start: 95 } },
    { ...data, progress: { start_ts: timestamp, current_soc: 88, completed_pp: 12, target_drop_pp: 10, estimate_wh: .8, duration_sec: 60 } }]) {
    assert.equal(parseBatteryCapacityReport(input), null);
  }
  const rejected = observation({ accepted: false, index_pct: null, change_pct: null, reason: 'short_window' });
  assert.ok(parseBatteryCapacityReport(report({ recent: [rejected] })));
});

test('formatters distinguish unavailable values and allow observed indices above 100', () => {
  for (const value of [undefined, null, NaN, Infinity, -2]) {
    assert.equal(capacityNumber(value), '—'); assert.equal(capacityIndexLabel(value), '—');
  }
  assert.equal(capacityNumber(0, 2), '0.00');
  assert.equal(capacityIndexLabel(100), '100');
  assert.equal(capacityIndexLabel(103.26), '103.3');
  assert.equal(capacityDate(null), '—'); assert.equal(capacityDate(Infinity), '—');
  assert.match(capacityDate(timestamp), /2026/);
});

test('rejection and waiting reasons remain understandable without exposing internal codes', () => {
  for (const reason of ['sample_gap', 'context_changed', 'short_window', 'unstable_load', 'load_mismatch', 'not_observed']) {
    const label = capacityReasonLabel(reason);
    assert.match(label, /[\u4e00-\u9fff]/); assert.ok(!label.includes(reason));
  }
  for (const reason of ['constructor', '__proto__', 'future_internal_reason']) {
    assert.equal(capacityReasonLabel(reason), '本轮尚不满足比较条件，等待后续连续放电记录。');
  }
  assert.equal(capacityReasonLabel(null), null);
});
