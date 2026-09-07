import assert from 'node:assert/strict';
import { test } from 'node:test';
import {
  cellBalancePresentation, cellNumberLabel, energyBasisLabel, energyCoverageLabel,
  energyEstimateValue, energyReasonLabels, energyStatusLabel, energyWhLabel, shortDurationLabel,
} from '../src/batteryDisplay.ts';

function energy(changes = {}) {
  return { schema: 1, estimate_wh: 1.23456789, covered_duration_sec: 120,
    observed_duration_sec: 120, coverage_ratio: 1, interval_count: 60,
    status: 'available', reasons: [], basis: { profile: 'custom', revision: 'test-revision',
      battery_gain: 1.234567890123, estimate_basis: 'us3000_battery_custom_test',
      source: 'usbmon', device: { serial: 'test' }, decoder_version: 4, formula_version: 2 },
    start_soc: 90, end_soc: 89, ...changes };
}

function balance(changes = {}) {
  return { schema: 1, state: 'assessed', level: 'good', candidate_level: 'good', delta_mv: 12,
    standby_duration_sec: 1800, required_standby_sec: 1800, persistence_sec: 120,
    candidate_duration_sec: 1800, lowest_cells: [1], frequent_lowest_cell: 1,
    recent_max_delta_mv: 18, recent_window_sec: 1800, recent_sample_count: 901,
    sample_timestamp: 10000, observed: true, reason: null,
    thresholds: { good_below_mv: 20, minor_below_mv: 50, elevated_below_mv: 100 },
    reference_only: true, ...changes };
}

test('energy formatting distinguishes missing measurements, true zero and small positive estimates', () => {
  for (const value of [null, undefined, NaN, Infinity, -1]) assert.equal(energyWhLabel(value), '—');
  assert.equal(energyWhLabel(0), '0.00');
  assert.equal(energyWhLabel(.001), '< 0.01');
  assert.equal(energyWhLabel(.01), '0.01');
  assert.equal(energyWhLabel(12.3456789), '12.35');
});

test('no valid integration interval cannot masquerade as a zero-energy observation', () => {
  assert.equal(energyEstimateValue(undefined), null);
  assert.equal(energyEstimateValue(null), null);
  assert.equal(energyEstimateValue(energy({ interval_count: 0, estimate_wh: 0 })), null);
  assert.equal(energyEstimateValue(energy({ status: 'unavailable', estimate_wh: 0 })), null);
  assert.equal(energyEstimateValue(energy({ estimate_wh: null })), null);
  assert.equal(energyEstimateValue(energy({ schema: 2 })), null);
  assert.equal(energyEstimateValue(energy({ estimate_wh: 0 })), 0);
  assert.equal(energyEstimateValue(energy({ status: 'partial' })), 1.23456789);
});

test('partial energy coverage never rounds up to full coverage', () => {
  for (const value of [null, undefined, NaN, Infinity, -.1, 1.1]) assert.equal(energyCoverageLabel(value), '—');
  assert.equal(energyCoverageLabel(0), '0%');
  assert.equal(energyCoverageLabel(.0005), '< 0.1%');
  assert.equal(energyCoverageLabel(.375), '37.5%');
  assert.equal(energyCoverageLabel(.999999), '99.9%');
  assert.equal(energyCoverageLabel(1), '100%');
});

test('legacy records and missing calibration explain why the estimate is unavailable', () => {
  assert.equal(energyStatusLabel(undefined), '旧记录未记录能量');
  assert.equal(energyStatusLabel(null), '旧记录未记录能量');
  assert.equal(energyStatusLabel(energy({ interval_count: 0, status: 'unavailable', estimate_wh: null, reasons: ['not_configured'] })), '电池放电尚未校准');
  assert.equal(energyStatusLabel(energy()), '观测区间已覆盖');
  assert.equal(energyStatusLabel(energy({ status: 'partial', coverage_ratio: .5 })), '覆盖不完整');
  assert.equal(energyStatusLabel(energy({ schema: 2 })), '此记录的能量暂不可展示');
});

test('every energy gap reason has a readable label and unknown reasons never leak technical fields', () => {
  const known = ['not_configured', 'invalid_basis', 'invalid_power', 'sample_gap', 'timestamp_rollback',
    'context_changed', 'continuity_lost', 'invalid_energy', 'insufficient_samples'];
  const labels = energyReasonLabels(known);
  assert.equal(labels.length, known.length);
  assert.ok(labels.every(label => /[\u4e00-\u9fff]/.test(label)));
  assert.ok(labels.every(label => !known.includes(label)));
  assert.deepEqual(energyReasonLabels(['future_internal_code', 'constructor', '__proto__']), ['部分观测存在数据缺口或尚未确认的原因']);
  assert.deepEqual(energyReasonLabels(['sample_gap', 'sample_gap']), ['采样间隔过长，缺口未补算']);
  assert.deepEqual(energyReasonLabels(null), []);
});

test('power-basis explanations keep calibration assumptions distinct', () => {
  assert.equal(energyBasisLabel(null), '尚无有效的功率依据');
  assert.equal(energyBasisLabel(energy().basis), '自定义倍率 · 未独立验证');
  assert.equal(energyBasisLabel({ ...energy().basis, profile: 'local-19v-v1', estimate_basis: 'nominal_43_2wh_soc_v1' }), '标称容量假设 · 未独立验证');
  assert.equal(energyBasisLabel({ ...energy().basis, profile: 'future-profile', estimate_basis: 'internal_basis_v9' }), '已记录的电池放电倍率 · 依据待确认');
});

test('display helpers do not mutate or round stored energy and coefficient values', () => {
  const value = energy();
  const original = structuredClone(value);
  energyWhLabel(energyEstimateValue(value)); energyCoverageLabel(value.coverage_ratio);
  energyStatusLabel(value); energyBasisLabel(value.basis); energyReasonLabels(value.reasons);
  assert.deepEqual(value, original);
});

test('only assessed backend levels receive a reference label and color', () => {
  const labels = { good: '一致性较好', minor: '轻微差异', elevated: '压差偏大', check: '建议检查' };
  for (const [level, label] of Object.entries(labels)) {
    const result = cellBalancePresentation(balance({ level }), 'online', true);
    assert.equal(result.label, label);
    assert.equal(result.tone, level);
    assert.equal(result.showStandby, true);
    assert.equal(result.showConfirmation, false);
    assert.doesNotMatch(result.label + result.advice, /优秀|严重故障|健康\s*\d/);
  }
});

test('settling and a changing level stay neutral even if a prior good level remains in a response', () => {
  const settling = cellBalancePresentation(balance({ state: 'settling', standby_duration_sec: 1799 }), 'online', true);
  assert.equal(settling.label, '等待待机稳定');
  assert.equal(settling.tone, 'neutral');
  assert.equal(settling.showStandby, true);
  const changing = cellBalancePresentation(balance({ state: 'observing', level: 'good', candidate_level: 'check', candidate_duration_sec: 40, delta_mv: 120 }), 'online', true);
  assert.equal(changing.label, '等级观察中');
  assert.equal(changing.tone, 'neutral');
  assert.equal(changing.showConfirmation, true);
  assert.doesNotMatch(changing.label + changing.advice, /一致性较好/);
});

test('charging and discharging suppress static reference grades even with an old assessed report', () => {
  const charging = cellBalancePresentation(balance(), 'charging', true);
  assert.equal(charging.label, '充电中 · 仅显示读数');
  assert.equal(charging.tone, 'neutral');
  assert.equal(charging.showStandby, false);
  const battery = cellBalancePresentation(balance({ level: 'check' }), 'battery', true);
  assert.equal(battery.label, '放电中 · 仅显示读数');
  assert.equal(battery.tone, 'neutral');
});

test('stale, unobserved and old-API reports cannot show a current confirmed grade', () => {
  const stale = cellBalancePresentation(balance(), 'online', false);
  assert.equal(stale.label, '等待实时数据');
  assert.equal(stale.tone, 'neutral');
  assert.equal(stale.showStandby, false);
  const unseen = cellBalancePresentation(balance({ state: 'observing', observed: false, reason: 'not_observed' }), 'online', true);
  assert.equal(unseen.label, '等待后台观测');
  assert.equal(unseen.tone, 'neutral');
  assert.equal(unseen.showStandby, false);
  for (const report of [undefined, null, balance({ schema: 2 })]) {
    const result = cellBalancePresentation(report, 'online', true);
    assert.equal(result.label, '参考状态暂未提供');
    assert.match(result.advice, /更新面板/);
    assert.equal(result.tone, 'neutral');
  }
});

test('unknown modes, levels, states and unavailable reasons use neutral readable fallbacks', () => {
  assert.equal(cellBalancePresentation(balance(), 'internal_mode', true).label, '等待工况确认');
  for (const report of [balance({ level: 'future_level' }), balance({ level: '__proto__' }), balance({ state: 'future_state' })]) {
    const result = cellBalancePresentation(report, 'online', true);
    assert.equal(result.label, '参考状态待确认');
    assert.equal(result.tone, 'neutral');
    assert.doesNotMatch(JSON.stringify(result), /future_|__proto__/);
  }
  const invalid = cellBalancePresentation(balance({ state: 'unavailable', reason: 'invalid_sample' }), 'online', true);
  assert.equal(invalid.label, '读数暂不可评估');
  assert.equal(invalid.tone, 'neutral');
});

test('cell indices and progress durations keep missing data distinct from observed zero', () => {
  assert.equal(cellNumberLabel(1), '电芯 01');
  assert.equal(cellNumberLabel(4), '电芯 04');
  for (const value of [null, undefined, 0, 5, 1.5, NaN]) assert.equal(cellNumberLabel(value), '无单一结果');
  assert.equal(shortDurationLabel(null), '—');
  assert.equal(shortDurationLabel(-1), '—');
  assert.equal(shortDurationLabel(0), '0 秒');
  assert.equal(shortDurationLabel(90), '1 分 30 秒');
  assert.equal(shortDurationLabel(1800), '30 分 0 秒');
  assert.equal(shortDurationLabel(3661.9), '1 小时 1 分 1 秒');
});
