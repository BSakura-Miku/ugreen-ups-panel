import assert from 'node:assert/strict';
import { test } from 'node:test';
import { buildRawTrace, diagnosticCount, diagnosticTime, diagnosticVersion, rawChannelLabel, rawChannelValue, runtimeObservation } from '../src/diagnosticDisplay.ts';

const point = (timestamp, channelA = 41, channelB = 48, changes = {}) => ({ timestamp, channelA, channelB, mode: 'online', segment: 'one', ...changes });

test('raw byte display distinguishes unknown, stale and true zero without a temperature unit', () => {
  for (const invalid of [null, undefined, NaN, Infinity, -1, 256, 1.5, '41', true]) {
    assert.equal(rawChannelValue(invalid), null);
    assert.equal(rawChannelLabel(invalid, true), '—');
  }
  for (const value of [0, 41, 255]) {
    assert.equal(rawChannelLabel(value, true), String(value));
    assert.equal(rawChannelLabel(value, false), '—');
  }
});

test('missing diagnostics never turn into zero counts or invented versions', () => {
  assert.equal(diagnosticCount(0), '0');
  for (const invalid of [null, undefined, NaN, Infinity, -1, 2.2, '0']) assert.equal(diagnosticCount(invalid), '—');
  assert.equal(diagnosticVersion(null), '未知');
  assert.equal(diagnosticVersion(' '), '未知');
  assert.equal(diagnosticVersion('TOP HID 1.0'), 'TOP HID 1.0');
  assert.equal(diagnosticTime(Infinity), '—');
  assert.equal(diagnosticTime(1e20), '—');
  assert.equal(diagnosticTime(-1), '—');
});

test('known runtime sentinels never become a confident remaining duration', () => {
  for (const sentinel of [-1, '-1', 65535, '65535', 4294967295, '4294967295']) assert.match(runtimeObservation(sentinel), /疑似无效/);
  assert.match(runtimeObservation(null), /未提供/);
  assert.match(runtimeObservation('300'), /未独立验证/);
});

test('raw trace keeps constant samples and draws integer steps on both channels', () => {
  const points = [point(100, 40, 47), point(102, 40, 47), point(104, 41, 48)];
  const before = structuredClone(points);
  const result = buildRawTrace(points);
  assert.equal(result.paths.length, 2);
  for (const trace of result.paths) {
    assert.equal((trace.path.match(/ H /g) || []).length, 2);
    assert.equal((trace.path.match(/ V /g) || []).length, 2);
    assert.doesNotMatch(trace.path, /NaN|Infinity| C /);
  }
  assert.equal(result.first, 100); assert.equal(result.last, 104);
  assert.deepEqual(points, before);
});

test('gaps, mode changes, identity changes and explicit breaks cannot be connected', () => {
  for (const second of [point(106), point(102, 41, 48, { mode: 'battery' }), point(102, 41, 48, { segment: 'two' }), point(102, 41, 48, { breakBefore: true })]) {
    const trace = buildRawTrace([point(100), second]);
    assert.equal(trace.paths.length, 4);
    assert.ok(trace.paths.every(path => !path.path.includes(' H ')));
  }
  assert.equal(buildRawTrace([point(100), point(102, 41, 48, { mode: 'charging' })]).markers[0].mode, 'charging');
});

test('duplicates are ignored but timestamp conflicts and reversals break the trace', () => {
  assert.equal(buildRawTrace([point(100), point(100), point(102)]).paths[0].path.split(' H ').length, 2);
  for (const second of [point(100, 42), point(99)]) {
    const trace = buildRawTrace([point(100), second]);
    assert.equal(trace.paths.length, 4);
  }
});

test('invalid channel samples break only their own line and missing values are never plotted as zero', () => {
  const trace = buildRawTrace([point(100), point(102, null), point(104)]);
  assert.equal(trace.paths.filter(path => path.channel === 'A').length, 2);
  assert.equal(trace.paths.filter(path => path.channel === 'B').length, 1);
  assert.equal(trace.low, 40);
  assert.equal(buildRawTrace([]), null);
  assert.equal(buildRawTrace([point(100, null, null)]), null);
  assert.equal(buildRawTrace([point(NaN)]), null);
});

test('single sample and byte boundaries remain visible without an invented timespan', () => {
  for (const value of [0, 255]) {
    const trace = buildRawTrace([point(100, value, value)]);
    assert.equal(trace.first, trace.last);
    assert.equal(trace.dots.length, 2);
    assert.ok(trace.dots.every(dot => Number.isFinite(dot.x) && Number.isFinite(dot.y)));
    assert.ok(trace.low >= 0 && trace.high <= 255);
  }
});
