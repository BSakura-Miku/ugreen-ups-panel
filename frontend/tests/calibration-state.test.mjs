import assert from 'node:assert/strict';
import { test } from 'node:test';
import { calibrationCanSave, calibrationEditRevision, calibrationEstimateIssue, calibrationLabel, calibrationReadinessMessage, parseCalibrationConfig, parseCalibrationState } from '../src/calibrationState.ts';
import { calibrationPayload, chooseProfile, chooseVoltage, confirmVoltage, createDraft, createStateDraft, editCoefficient } from '../src/calibrationDraft.ts';

const defaults = { base_gain: 1.123456789, charge_gain: 1.2, battery_gain: 1.3 };
const none = { schema: 1, profile: 'none', coefficients: null, revision: 'none-revision' };
const custom = { schema: 2, profile: 'custom', coefficients: { ...defaults, charge_gain: null }, ac_voltage_nominal_v: 12, revision: 'custom-revision' };
const response = (patch = {}) => ({ schema: 1, defaults, desired: custom, active: custom, collector_ready: true, pending: false, error: null, supported_config_schemas: [1, 2], ...patch });

test('real API numbers retain precision, zero recharge and optional modern coefficients', () => {
  const parsed = parseCalibrationState(response());
  assert.equal(parsed.active.coefficients.base_gain, defaults.base_gain);
  assert.equal(parsed.active.coefficients.charge_gain, null);
  assert.equal(parsed.active.ac_voltage_nominal_v, 12);
  assert.equal(calibrationCanSave(parsed), true);
  assert.equal(calibrationReadinessMessage(parsed), '');
  const zero = parseCalibrationConfig({ ...custom, coefficients: { ...defaults, charge_gain: 0, battery_gain: null } });
  assert.equal(zero.coefficients.charge_gain, 0);
  assert.equal(zero.coefficients.battery_gain, null);
});

test('unknown runtime profile is named and never creates inherited nineteen-volt coefficients or a blind save', () => {
  const unknown = { ...custom, profile: 'local-12v-v1' };
  const parsed = parseCalibrationState(response({ active: unknown, desired: unknown }));
  assert.equal(parsed.active.schema, null);
  assert.equal(parsed.active.coefficients, null);
  assert.match(calibrationLabel(parsed.active.profile), /不支持.*local-12v-v1/);
  assert.doesNotMatch(calibrationLabel(parsed.active.profile), /等待|19 V/);
  assert.equal(calibrationCanSave(parsed), false, 'a stale true collector flag cannot approve an unknown active configuration');
  const draft = createStateDraft(parsed);
  assert.equal(draft.profile, null);
  assert.equal(draft.voltage, null);
  assert.equal(draft.voltageConfirmed, false);
  assert.deepEqual(draft.values, { base_gain: '', charge_gain: '', battery_gain: '' });
  assert.equal(calibrationPayload(draft, true).payload, null);
  assert.equal(calibrationPayload({ ...draft, profile: 'local-12v-v1' }, true).payload, null);
  const chosen = chooseProfile(draft, 'custom');
  assert.equal(chosen.voltageConfirmed, false);
  assert.deepEqual(chosen.values, draft.values);
  assert.equal(calibrationPayload(chosen, true).payload, null);
});

test('missing, wrong-type and out-of-range API coefficients are rejected before draft creation', () => {
  for (const coefficients of [null, {}, { base_gain: 1 }, { ...defaults, base_gain: '1.1' }, { ...defaults, base_gain: Infinity },
    { ...defaults, base_gain: true }, { ...defaults, battery_gain: -1 }, { ...defaults, charge_gain: 10.1 }]) {
    const parsed = parseCalibrationConfig({ ...custom, coefficients });
    assert.equal(parsed.schema, null);
    assert.equal(parsed.coefficients, null);
    const draft = createDraft(parsed);
    assert.equal(draft.profile, null);
    assert.equal(calibrationPayload(draft, true).payload, null);
  }
  for (const patch of [{ schema: 3 }, { ac_voltage_nominal_v: '12' }, { ac_voltage_nominal_v: 24 }, { revision: null }]) {
    assert.equal(parseCalibrationConfig({ ...custom, ...patch }).schema, null);
  }
  assert.equal(parseCalibrationConfig({ schema: 1, profile: 'custom', coefficients: null, revision: 'a' }).schema, null);
});

test('desired file repair uses the content edit token and never pre-fills a fallback configuration', () => {
  const state = parseCalibrationState(response({ desired: none, active: none, collector_ready: false,
    edit_revision: 'invalid-file-content-token', configuration_problem: { source: 'desired', code: 'unsupported_profile', profile: 'local-12v-v1' },
    readiness: { ready: false, can_save: true, code: 'invalid_config', message: '校准文件需要重新配置。', issues: [] } }));
  assert.equal(calibrationCanSave(state), true);
  const draft = createStateDraft(state);
  assert.equal(draft.profile, null);
  assert.equal(draft.revision, 'invalid-file-content-token');
  assert.deepEqual(draft.values, { base_gain: '', charge_gain: '', battery_gain: '' });
  const filled = editCoefficient(confirmVoltage(chooseVoltage(chooseProfile(draft, 'custom'), 12)), 'base_gain', '1.2467890123');
  const payload = calibrationPayload(filled, true).payload;
  assert.equal(payload.expected_revision, 'invalid-file-content-token');
  assert.equal(payload.ac_voltage_nominal_v, 12);
  assert.equal(payload.coefficients.base_gain, 1.2467890123);
  assert.equal(payload.coefficients.charge_gain, null);
  assert.equal(payload.coefficients.battery_gain, null);
  assert.notEqual(calibrationEditRevision({ ...state, edit_revision: 'file-changed' }), draft.revision, 'file changes invalidate the editing token');
});

test('explicit readiness failures remain actionable and override a legacy collector-ready flag', () => {
  for (const [code, message] of [
    ['calibration_unconfigured', '采集器未配置校准文件路径，请补齐环境变量。'],
    ['calibration_unreadable', '采集器无法读取校准文件，请检查路径与权限。'],
    ['revision_mismatch', '采集器和面板的校准配置尚未匹配。'],
    ['stale_sample', '采集数据已过期，请先恢复实时采集。'],
  ]) {
    const state = parseCalibrationState(response({ readiness: { ready: false, can_save: false, code, message, issues: [{ code, message }] } }));
    assert.equal(calibrationCanSave(state), false);
    assert.equal(calibrationReadinessMessage(state), message);
  }
  assert.equal(calibrationCanSave(parseCalibrationState(response({ readiness: { ready: false, code: null, message: null, issues: [] } }))), false);
  assert.equal(calibrationCanSave(parseCalibrationState(response({ readiness: { ready: true, can_save: 'true', issues: [] } }))), false);
  const malformed = parseCalibrationState(response({ readiness: { ready: true, issues: 'not-a-list' } }));
  assert.equal(calibrationCanSave(malformed), false);
  assert.match(calibrationReadinessMessage(malformed), /响应无效/);
});

test('old API missing readiness gives a deployment hint without guessing the collector is offline', () => {
  const state = parseCalibrationState(response({ collector_ready: false }));
  assert.equal(calibrationCanSave(state), false);
  assert.match(calibrationReadinessMessage(state), /校准路径.*旧版接口未提供具体原因/);
  assert.doesNotMatch(calibrationReadinessMessage(state), /离线|请更新宿主机采集器/);
  assert.equal(calibrationCanSave(parseCalibrationState(response())), true);
});

test('malformed envelopes cannot become valid configurations and arbitrary fields are not copied', () => {
  for (const value of [null, [], {}, response({ schema: 2 }), response({ defaults: {} }), response({ collector_ready: 'true' }), response({ pending: null }), response({ configuration_problem: { source: 'other', code: 'x' } })]) {
    assert.equal(parseCalibrationState(value), null);
  }
  const state = parseCalibrationState(response({ private_key: 'secret', desired: { ...custom, private_data: 'secret' } }));
  assert.equal(JSON.stringify(state).includes('secret'), false);
});

test('estimate validation explains calibration failures without hiding or changing raw telemetry', () => {
  const view = { fresh: true, sample: { soc: 83, battery_voltage: 14.22 }, calibration_validation: { valid: false, reason: 'unsupported_profile' } };
  assert.match(calibrationEstimateIssue(view), /不支持/);
  assert.deepEqual(view.sample, { soc: 83, battery_voltage: 14.22 });
  assert.equal(view.fresh, true);
  assert.equal(calibrationEstimateIssue({ ...view, calibration_validation: { valid: true, reason: null } }), null);
  assert.equal(calibrationEstimateIssue({ ...view, calibration_validation: { valid: false, reason: 'new_code', message: '校准版本不一致。' } }), '校准版本不一致。');
  assert.equal(calibrationEstimateIssue({ ...view, calibration_validation: { valid: false, reason: 'secret_raw_exception' } }).includes('secret'), false);
});
