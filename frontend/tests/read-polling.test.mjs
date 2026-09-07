import assert from 'node:assert/strict';
import { test } from 'node:test';
import { historyPollInterval, startVisiblePolling } from '../src/readPolling.ts';

function environment(hidden = false) {
  const listeners = new Set(), scheduled = new Map();
  let counter = 0;
  return {
    visibility: { hidden, addEventListener: (_, listener) => listeners.add(listener), removeEventListener: (_, listener) => listeners.delete(listener) },
    clock: { schedule: (callback, delay) => { scheduled.set(++counter, { callback, delay }); return counter; }, cancel: id => scheduled.delete(id) },
    scheduled, listeners,
    setHidden(value) { this.visibility.hidden = value; for (const listener of listeners) listener(); },
    tick() { const [id, task] = scheduled.entries().next().value; scheduled.delete(id); task.callback(); },
  };
}
const settle = async () => { await Promise.resolve(); await Promise.resolve(); };

test('short, long and daily aggregation windows have distinct polling cadence', () => {
  assert.equal(historyPollInterval(1, 10), 10000);
  assert.equal(historyPollInterval(24, 80), 10000);
  assert.equal(historyPollInterval(168, 600), 30000);
  assert.equal(historyPollInterval(720, 3600), 30000);
  assert.equal(historyPollInterval(8760, 86400), 60000);
});

test('hidden pages do not read; returning reads once and unmount cancels the next read', async () => {
  const env = environment(true); let calls = 0;
  const stop = startVisiblePolling(async () => { calls++; }, () => 30000, env.visibility, env.clock);
  await settle(); assert.equal(calls, 0); assert.equal(env.scheduled.size, 0);
  env.setHidden(false); await settle();
  assert.equal(calls, 1); assert.equal(env.scheduled.size, 1);
  stop(); assert.equal(env.scheduled.size, 0); assert.equal(env.listeners.size, 0);
  env.setHidden(false); await settle(); assert.equal(calls, 1);
});

test('hiding aborts an in-flight read and its late completion cannot replace the resumed timer', async () => {
  const env = environment(); const reads = [];
  const stop = startVisiblePolling(signal => new Promise(resolve => reads.push({ signal, resolve })), () => 10000, env.visibility, env.clock);
  assert.equal(reads.length, 1);
  env.setHidden(true); assert.equal(reads[0].signal.aborted, true);
  env.setHidden(false); assert.equal(reads.length, 2);
  reads[0].resolve(); await settle(); assert.equal(env.scheduled.size, 0);
  reads[1].resolve(); await settle(); assert.equal(env.scheduled.size, 1);
  stop(); assert.equal(env.scheduled.size, 0);
});

test('pending reads are aborted on section exit; successful resolution chooses the current interval', async () => {
  const env = environment(); let resolve, signal;
  const stop = startVisiblePolling(value => { signal = value; return new Promise(done => { resolve = done; }); }, () => 60000, env.visibility, env.clock);
  stop(); assert.equal(signal.aborted, true); resolve(); await settle(); assert.equal(env.scheduled.size, 0);
  const next = startVisiblePolling(async () => {}, () => 60000, env.visibility, env.clock);
  await settle(); assert.equal([...env.scheduled.values()][0].delay, 60000);
  next();
});


test('default timers retain browser-compatible receivers across recurring reads and cancellation', async t => {
  const env = environment(); let reads = 0, scheduledCalls = 0, cancelledCalls = 0;
  const assertReceiver = receiver => assert.ok(receiver === undefined || receiver === globalThis,
    'browser timers must not receive the injected clock object as this');
  // Native Window timers reject arbitrary method receivers. Node's ordinary
  // timers and injected arrow functions do not, so exercise the default clock.
  t.mock.method(globalThis, 'setTimeout', function (callback, delay) {
    assertReceiver(this); scheduledCalls++;
    return env.clock.schedule(callback, delay);
  });
  t.mock.method(globalThis, 'clearTimeout', function (timer) {
    assertReceiver(this); cancelledCalls++;
    env.clock.cancel(timer);
  });
  const stop = startVisiblePolling(async () => { reads++; }, () => 10000, env.visibility);
  try {
    await settle(); assert.equal(reads, 1); assert.equal(env.scheduled.size, 1);
    env.tick(); await settle(); assert.equal(reads, 2); assert.equal(env.scheduled.size, 1);
    env.tick(); await settle(); assert.equal(reads, 3); assert.equal(env.scheduled.size, 1);
    assert.equal(scheduledCalls, 3);
    env.setHidden(true); assert.equal(env.scheduled.size, 0); assert.equal(cancelledCalls, 1);
    env.setHidden(false); await settle(); assert.equal(reads, 4); assert.equal(env.scheduled.size, 1);
    stop(); assert.equal(env.scheduled.size, 0); assert.equal(cancelledCalls, 2); assert.equal(env.listeners.size, 0);
  } finally { stop(); }
});
