import assert from 'node:assert/strict';
import { test } from 'node:test';
import {
  advanceSampling, calculateBase, calculateCharge, createSamplingState,
  samplingIdentity, suggestVoltage,
} from '../src/calibrationSampler.ts';

const timestamps = [0, 2, 5, 8, 11, 14, 17, 20, 23, 26, 28, 30];

function frame(timestamp, changes = {}) {
  return { timestamp, mode: 'online', inputVoltage: 12, baseRaw: 50,
    chargePower: null, device: 'UPS-TEST', source: 'usbmon', revision: 'revision-a',
    decoderVersion: 4, formulaVersion: 2, fresh: true, ...changes };
}

function collect({ mode = 'online', voltage = 12, times = timestamps, values = () => ({}) } = {}) {
  return times.reduce((state, timestamp, index) => advanceSampling(state, frame(100 + timestamp, {
    mode, inputVoltage: voltage, chargePower: mode === 'charging' ? 10 : null, ...values(index),
  })), createSamplingState(mode, voltage));
}

function windowFor(mode, changes = {}) {
  const state = collect({ mode });
  assert.equal(state.phase, 'complete');
  return { ...state.window, ...changes };
}

function partial() {
  let state = createSamplingState('online', 12);
  for (const timestamp of [100, 102]) state = advanceSampling(state, frame(timestamp));
  return state;
}

function rejected(result) {
  assert.equal(result.value, null);
  assert.equal(typeof result.error, 'string');
  assert.ok(result.error.length > 0);
}

test('voltage suggestions choose the closest supported nominal and reject unsupported inputs', () => {
  for (const [input, expected] of [[12.2, 12], [18.8, 19], [20.1, 20], [11, 12], [13, 12], [18, 19], [21, 20]]) {
    assert.equal(suggestVoltage(input), expected);
  }
  for (const input of [10.999, 13.001, 17.999, 21.001, null, undefined, NaN, Infinity, true, '12']) {
    assert.equal(suggestVoltage(input), null);
  }
});

for (const voltage of [12, 19, 20]) {
  test(`${voltage} V sampling accepts both inclusive one-volt boundaries`, () => {
    for (const inputVoltage of [voltage - 1, voltage, voltage + 1]) {
      const result = collect({ voltage, values: () => ({ inputVoltage }) });
      assert.equal(result.phase, 'complete');
      assert.equal(result.window.voltage, voltage);
    }
    for (const inputVoltage of [voltage - 1.001, voltage + 1.001]) {
      const result = advanceSampling(createSamplingState('online', voltage), frame(100, { inputVoltage }));
      assert.equal(result.phase, 'collecting');
      assert.equal(result.count, 0);
      assert.equal(result.window, null);
      assert.match(result.reason, /采样已重置/);
    }
  });
}

test('a completed window requires thirty seconds and twelve independent samples', () => {
  const before = collect({ times: timestamps.slice(0, -1) });
  assert.equal(before.phase, 'collecting');
  assert.equal(before.count, 11);
  assert.equal(before.elapsed, 28);
  const complete = advanceSampling(before, frame(130));
  assert.equal(complete.phase, 'complete');
  assert.equal(complete.count, 12);
  assert.deepEqual({ first: complete.window.first, last: complete.window.last,
    count: complete.window.count, meanBase: complete.window.meanBase, meanCharge: complete.window.meanCharge },
  { first: 100, last: 130, count: 12, meanBase: 50, meanCharge: null });
  assert.equal(before.count, 11, 'advancing a window must not mutate the prior state');

  const undersampled = collect({ times: Array.from({ length: 11 }, (_, index) => index * 3) });
  assert.equal(undersampled.elapsed, 30);
  assert.equal(undersampled.phase, 'rejected');
  assert.equal(undersampled.window, null);
  assert.match(undersampled.reason, /不足 12/);
});

test('duplicate reports do not increase the count or complete a short window', () => {
  const start = partial();
  let result = start;
  for (let index = 0; index < 30; index++) result = advanceSampling(result, frame(102));
  assert.strictEqual(result, start);
  assert.equal(result.count, 2);
  assert.equal(result.elapsed, 2);
});

test('clock rollback and gaps above five seconds start a new observation window', () => {
  const start = partial();
  const exactGap = advanceSampling(start, frame(107));
  assert.equal(exactGap.count, 3);
  assert.equal(exactGap.reason, null);
  const gap = advanceSampling(exactGap, frame(112.001));
  assert.equal(gap.count, 1);
  assert.equal(gap.elapsed, 0);
  assert.equal(gap.points[0].timestamp, 112.001);
  assert.match(gap.reason, /超过 5 秒/);
  const rollback = advanceSampling(start, frame(101));
  assert.equal(rollback.count, 1);
  assert.equal(rollback.points[0].timestamp, 101);
  assert.match(rollback.reason, /倒序/);
});

test('staleness, mode changes and invalid raw inputs clear an unfinished window', () => {
  for (const changes of [
    { fresh: false }, { mode: 'charging' }, { mode: 'battery' }, { mode: 'unknown' },
    { timestamp: NaN }, { inputVoltage: null }, { inputVoltage: Infinity },
    { baseRaw: 0 }, { baseRaw: -1 }, { baseRaw: null }, { baseRaw: NaN },
  ]) {
    const result = advanceSampling(partial(), frame(104, changes));
    assert.equal(result.count, 0);
    assert.equal(result.identity, null);
    assert.equal(result.window, null);
    assert.ok(result.reason);
  }
  for (const chargePower of [null, 0, -1, NaN, Infinity]) {
    const state = advanceSampling(createSamplingState('charging', 12), frame(100, { mode: 'charging', chargePower: 10 }));
    assert.equal(advanceSampling(state, frame(102, { mode: 'charging', chargePower })).count, 0);
  }
});

test('device, source and calculation identity changes cannot be combined, even at a duplicate timestamp', () => {
  const original = frame(102);
  for (const changes of [{ device: 'UPS-OTHER' }, { source: 'replay' }, { revision: 'revision-b' },
    { decoderVersion: 5 }, { formulaVersion: 3 }]) {
    const changed = frame(102, changes);
    assert.notEqual(samplingIdentity(changed), samplingIdentity(original));
    const result = advanceSampling(partial(), changed);
    assert.equal(result.count, 1);
    assert.equal(result.elapsed, 0);
    assert.equal(result.identity, samplingIdentity(changed));
    assert.match(result.reason, /发生变化/);
  }
});

test('completed and rejected windows stay frozen until a new sampling state is created', () => {
  const complete = collect();
  const preserved = structuredClone(complete);
  for (const next of [frame(132, { baseRaw: 99 }), frame(150, { fresh: false }), frame(90, { device: 'OTHER' })]) {
    assert.strictEqual(advanceSampling(complete, next), complete);
    assert.deepEqual(complete, preserved);
  }
  const unstable = collect({ values: index => ({ baseRaw: index % 2 ? 80 : 20 }) });
  assert.equal(unstable.phase, 'rejected');
  assert.strictEqual(advanceSampling(unstable, frame(132)), unstable);
  assert.equal(createSamplingState('online', 12).count, 0);
});

test('ten-percent spread is accepted while either raw channel above it is rejected', () => {
  const boundary = collect({ values: index => ({ baseRaw: index % 2 ? 21 : 19 }) });
  assert.equal(boundary.phase, 'complete');
  assert.equal(boundary.window.baseSpread, .1);
  const tooWide = collect({ values: index => ({ baseRaw: index % 2 ? 21.01 : 19 }) });
  assert.equal(tooWide.phase, 'rejected');
  assert.match(tooWide.reason, /10%/);
  const chargingBoundary = collect({ mode: 'charging', values: index => ({ chargePower: index % 2 ? 21 : 19 }) });
  assert.equal(chargingBoundary.phase, 'complete');
  assert.equal(chargingBoundary.window.chargeSpread, .1);
  const decimalBoundary = collect({ mode: 'charging', values: index => ({ chargePower: index % 2 ? 2.1 : 1.9 }) });
  assert.equal(decimalBoundary.phase, 'complete', 'decimal measurements at exactly ten percent must tolerate floating-point rounding');
  assert.equal(decimalBoundary.window.meanCharge, 2);
  const justAbove = collect({ mode: 'charging', values: index => ({ chargePower: index % 2 ? 2.100001 : 1.899999 }) });
  assert.equal(justAbove.phase, 'rejected', 'a real excess above ten percent must not be treated as rounding');
  const chargingWide = collect({ mode: 'charging', values: index => ({ chargePower: index % 2 ? 21.01 : 19 }) });
  assert.equal(chargingWide.phase, 'rejected');
  assert.match(chargingWide.reason, /10%/);
});

test('charging needs at least two watts of mean raw charge power', () => {
  const accepted = collect({ mode: 'charging', values: () => ({ chargePower: 2 }) });
  assert.equal(accepted.phase, 'complete');
  assert.equal(accepted.window.meanCharge, 2);
  const low = collect({ mode: 'charging', values: () => ({ chargePower: 1.999 }) });
  assert.equal(low.phase, 'rejected');
  assert.match(low.reason, /低于 2 W/);
  const justBelow = collect({ mode: 'charging', values: () => ({ chargePower: 1.999999 }) });
  assert.equal(justBelow.phase, 'rejected', 'a real deficit below two watts must not be treated as rounding');
});

test('a mathematically two-watt mean remains usable when summation rounds slightly below two', () => {
  const boundary = collect({ mode: 'charging', values: index => ({ chargePower: index < 11 ? 1.99 : 2.11 }) });
  assert.equal(boundary.phase, 'complete');
  assert.ok(Math.abs(boundary.window.meanCharge - 2) < 1e-14);
  const result = calculateCharge(boundary.window, 52, 1);
  assert.equal(result.error, null, 'calculation must accept the same two-watt boundary as sampling');
  assert.ok(Math.abs(result.value - 1) < 1e-14);
  rejected(calculateCharge({ ...boundary.window, meanCharge: 1.999999 }, 52, 1));
});

test('the second formula uses its own charging-window base reading', () => {
  const first = windowFor('online', { meanBase: 40 });
  const base = calculateBase(first, 48);
  assert.deepEqual(base, { value: 1.2, error: null });
  const second = windowFor('charging', { meanBase: 50, meanCharge: 8 });
  assert.deepEqual(calculateCharge(second, 72, base.value), { value: 1.5, error: null });
  assert.deepEqual(calculateCharge(second, 60, base.value), { value: 0, error: null });
});

test('calculations reject invalid meter watts, invalid base gains and mismatched windows', () => {
  const online = windowFor('online'), charging = windowFor('charging');
  for (const watts of [0, -1, NaN, Infinity, -Infinity, null, undefined, true, '60']) {
    rejected(calculateBase(online, watts));
    rejected(calculateCharge(charging, watts, 1.2));
  }
  for (const base of [0, -1, 10.001, NaN, Infinity, null, true, '1.2']) {
    rejected(calculateCharge(charging, 80, base));
  }
  rejected(calculateBase(charging, 80));
  rejected(calculateCharge(online, 80, 1.2));
  rejected(calculateBase({ ...online, meanBase: 0 }, 80));
  rejected(calculateCharge({ ...charging, meanCharge: 1.999 }, 80, 1.2));
});

test('gain limits accept ten and zero recharge without clamping invalid results', () => {
  const online = windowFor('online', { meanBase: 10 });
  assert.deepEqual(calculateBase(online, 100), { value: 10, error: null });
  rejected(calculateBase(online, 100.001));
  const charging = windowFor('charging', { meanBase: 50, meanCharge: 8 });
  assert.deepEqual(calculateCharge(charging, 130, 1), { value: 10, error: null });
  rejected(calculateCharge(charging, 130.001, 1));
  rejected(calculateCharge(charging, 49, 1));
});
