import assert from 'node:assert/strict';
import { test } from 'node:test';
import {
  applySuggestion, calibrationPayload, chooseVoltage, confirmVoltage, createDraft,
  editCoefficient, formatCoefficient, presetDraft,
} from '../src/calibrationDraft.ts';

const precise = { base_gain: 1.1234567890123457, charge_gain: 1.2843154306288043,
  battery_gain: 1.2091130139203523 };
const none = { schema: 1, profile: 'none', coefficients: null, revision: 'none-revision' };

function legacy(profile = 'custom') {
  return { schema: 1, profile, coefficients: { ...precise }, revision: 'legacy-revision' };
}

function modern(voltage = 19, coefficients = precise) {
  return { schema: 2, profile: 'custom', ac_voltage_nominal_v: voltage,
    coefficients: { ...coefficients }, revision: 'modern-revision' };
}

function firstStep(draft = createDraft(none), voltage = 12, base = precise.base_gain) {
  return applySuggestion(draft, { voltage, base, charge: null, includeCharge: false });
}

function payload(draft, supportsV2 = true) {
  const result = calibrationPayload(draft, supportsV2);
  assert.equal(result.error, null);
  assert.ok(result.payload);
  return result.payload;
}

function invalid(draft, supportsV2 = true, field) {
  const result = calibrationPayload(draft, supportsV2);
  assert.equal(result.payload, null);
  assert.equal(typeof result.error, 'string');
  assert.ok(result.error.length > 0);
  if (field) assert.equal(result.field, field);
}

test('none starts empty and a first-step-only suggestion produces a nullable custom request', () => {
  const original = createDraft(none);
  assert.deepEqual(original.values, { base_gain: '', charge_gain: '', battery_gain: '' });
  assert.equal(original.voltage, null);
  assert.equal(original.voltageConfirmed, false);
  assert.deepEqual(payload(original), { profile: 'none', coefficients: null, expected_revision: none.revision });
  const draft = firstStep(original);
  assert.equal(draft.schema, 2);
  assert.equal(draft.profile, 'custom');
  assert.equal(draft.voltage, 12);
  assert.equal(draft.voltageConfirmed, true);
  assert.equal(draft.sources.base_gain, 'assistant');
  assert.equal(draft.sources.charge_gain, 'empty');
  assert.equal(draft.sources.battery_gain, 'empty');
  assert.deepEqual(payload(draft), { profile: 'custom', ac_voltage_nominal_v: 12,
    coefficients: { base_gain: precise.base_gain, charge_gain: null, battery_gain: null },
    expected_revision: none.revision });
  assert.equal(original.profile, 'none', 'applying a suggestion must not mutate the source draft');
  assert.equal(original.values.base_gain, '');
});

test('display rounding and unchanged saves preserve all coefficient precision', () => {
  const config = modern();
  const draft = createDraft(config);
  assert.equal(draft.values.base_gain, String(precise.base_gain));
  assert.equal(draft.values.charge_gain, String(precise.charge_gain));
  assert.equal(draft.values.battery_gain, String(precise.battery_gain));
  assert.equal(draft.displayValues.base_gain, '1.1235');
  assert.equal(draft.displayValues.charge_gain, '1.2843');
  assert.equal(draft.displayValues.battery_gain, '1.2091');
  assert.deepEqual(payload(draft).coefficients, precise);
  assert.equal(payload(draft).expected_revision, config.revision);
  assert.equal(formatCoefficient(null), '未校准');
  assert.notEqual(Number(formatCoefficient(.00000012)), 0);
});

for (const profile of ['custom', 'local-19v-v1']) {
  test(`schema-one ${profile} never silently assumes nineteen volts during migration`, () => {
    const draft = createDraft(legacy(profile));
    assert.equal(draft.voltage, null);
    assert.equal(draft.voltageConfirmed, false);
    if (profile === 'custom') invalid(draft, true, 'voltage');
    const chosen = chooseVoltage(draft, 19);
    assert.equal(chosen.schema, 2);
    assert.equal(chosen.profile, 'custom');
    assert.equal(chosen.voltageConfirmed, false);
    assert.deepEqual(chosen.values, { base_gain: '', charge_gain: '', battery_gain: String(precise.battery_gain) });
    invalid(chosen, true, 'voltage');
    invalid(confirmVoltage(chosen), true, 'base_gain');
    const suggested = firstStep(draft, 19);
    assert.deepEqual(payload(suggested).coefficients, {
      base_gain: precise.base_gain, charge_gain: null, battery_gain: precise.battery_gain,
    });
    assert.equal(suggested.sources.charge_gain, 'empty');
  });
}

for (const charge of [0, precise.charge_gain]) {
  test(`first-step suggestions on the same v2 voltage preserve the existing charge gain ${charge}`, () => {
    const original = createDraft(modern(20, { ...precise, charge_gain: charge }));
    const draft = firstStep(original, 20, 1.7654321098765432);
    assert.equal(draft.values.charge_gain, String(charge));
    assert.equal(draft.sources.charge_gain, 'existing');
    assert.equal(draft.values.battery_gain, String(precise.battery_gain));
    assert.deepEqual(payload(draft).coefficients, {
      base_gain: 1.7654321098765432, charge_gain: charge, battery_gain: precise.battery_gain,
    });
    assert.equal(original.values.base_gain, String(precise.base_gain));
  });
}

test('changing voltage clears both AC gains and preserves the battery gain and revision', () => {
  const original = createDraft(modern(19));
  assert.strictEqual(chooseVoltage(original, 19), original);
  const changed = chooseVoltage(original, 12);
  assert.equal(changed.voltageConfirmed, false);
  assert.deepEqual(changed.values, { base_gain: '', charge_gain: '', battery_gain: String(precise.battery_gain) });
  assert.deepEqual(changed.displayValues, { base_gain: '', charge_gain: '', battery_gain: '1.2091' });
  assert.deepEqual(changed.sources, { base_gain: 'empty', charge_gain: 'empty', battery_gain: 'existing' });
  assert.equal(changed.revision, original.revision);
  const manual = editCoefficient(confirmVoltage(changed), 'base_gain', '1.9876543210987654');
  assert.deepEqual(payload(manual).coefficients, {
    base_gain: 1.9876543210987654, charge_gain: null, battery_gain: precise.battery_gain,
  });
  const suggested = firstStep(original, 12, 2);
  assert.equal(suggested.sources.charge_gain, 'empty');
  assert.deepEqual(payload(suggested).coefficients, {
    base_gain: 2, charge_gain: null, battery_gain: precise.battery_gain,
  });
});

test('changing the base invalidates an assistant-derived charge gain but not equal numeric text', () => {
  const draft = applySuggestion(createDraft(none), { voltage: 12, base: 1.2, charge: 1.3, includeCharge: true });
  assert.equal(draft.sources.charge_gain, 'assistant');
  const same = editCoefficient(draft, 'base_gain', '1.2000');
  assert.equal(same.values.charge_gain, '1.3');
  const changed = editCoefficient(draft, 'base_gain', '1.25');
  assert.equal(changed.values.charge_gain, '');
  assert.equal(changed.displayValues.charge_gain, '');
  assert.equal(changed.sources.charge_gain, 'empty');
  assert.equal(payload(changed).coefficients.charge_gain, null);
  const recaptured = firstStep(draft, 12, 1.4);
  assert.equal(recaptured.values.charge_gain, '');
  assert.equal(recaptured.sources.charge_gain, 'empty');
  assert.equal(draft.values.charge_gain, '1.3');
});

test('a new two-step result installs its charge gain and does not discard the existing battery gain', () => {
  const draft = applySuggestion(createDraft(modern()), { voltage: 12, base: 1.8,
    charge: 1.3456789012345678, includeCharge: true });
  assert.equal(draft.sources.base_gain, 'assistant');
  assert.equal(draft.sources.charge_gain, 'assistant');
  assert.deepEqual(payload(draft).coefficients, { base_gain: 1.8,
    charge_gain: 1.3456789012345678, battery_gain: precise.battery_gain });
});

test('an old collector still receives a complete legacy request and rejects a version-two draft', () => {
  const draft = createDraft(legacy());
  const request = payload(draft, false);
  assert.deepEqual(request, { profile: 'custom', coefficients: precise, expected_revision: 'legacy-revision' });
  assert.equal(Object.hasOwn(request, 'ac_voltage_nominal_v'), false);
  invalid(editCoefficient(draft, 'charge_gain', ''), false, 'charge_gain');
  invalid(firstStep(), false);
});

test('modern saves accept explicit zero recharge and optional blank gains while requiring the base', () => {
  let draft = firstStep();
  draft = editCoefficient(draft, 'charge_gain', '0');
  draft = editCoefficient(draft, 'battery_gain', '   ');
  assert.deepEqual(payload(draft).coefficients, { base_gain: precise.base_gain, charge_gain: 0, battery_gain: null });
  invalid(editCoefficient(draft, 'base_gain', ''), true, 'base_gain');
  invalid(editCoefficient(draft, 'base_gain', '0'), true, 'base_gain');
  invalid(editCoefficient(draft, 'battery_gain', '0'), true, 'battery_gain');
  const maximum = ['base_gain', 'charge_gain', 'battery_gain'].reduce(
    (current, key) => editCoefficient(current, key, '10'), draft);
  assert.deepEqual(payload(maximum).coefficients, { base_gain: 10, charge_gain: 10, battery_gain: 10 });
});

test('invalid or out-of-range coefficient text cannot be sent or silently clamped', () => {
  for (const key of ['base_gain', 'charge_gain', 'battery_gain']) {
    for (const text of ['-1', '10.0001', 'NaN', 'Infinity', '1e309', '0x2', '1,2', '1 W']) {
      invalid(editCoefficient(firstStep(), key, text), true, key);
    }
  }
  const scientific = editCoefficient(firstStep(), 'base_gain', ' 1.2345678901234e0 ');
  assert.equal(payload(scientific).coefficients.base_gain, 1.2345678901234);
});

test('explicit preset filling preserves the edit revision and supports legacy requests', () => {
  const draft = createDraft(modern(12));
  const filled = presetDraft(draft, precise, false);
  assert.equal(filled.schema, 1);
  assert.equal(filled.voltage, null);
  assert.equal(filled.revision, draft.revision);
  assert.deepEqual(payload(filled, false).coefficients, precise);
  const modernPreset = presetDraft(draft, precise, true);
  assert.equal(modernPreset.voltage, 19);
  assert.equal(modernPreset.voltageConfirmed, true);
  assert.deepEqual(modernPreset.sources, { base_gain: 'preset', charge_gain: 'preset', battery_gain: 'preset' });
  assert.equal(payload(modernPreset).expected_revision, draft.revision);
});
