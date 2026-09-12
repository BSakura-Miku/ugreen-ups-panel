import assert from 'node:assert/strict';
import { test } from 'node:test';
import { mergeObservation } from '../src/observationStream.ts';

test('incremental observation handles update, eviction and reset without timestamp assumptions', () => {
  const first = {schema:1,cursor:'one',reset:true,first_sequence:1,points:[{sequence:1,timestamp:100},{sequence:2,timestamp:98}]};
  const loaded = mergeObservation(null, first);
  const next = mergeObservation(loaded, {...first,cursor:'two',reset:false,first_sequence:2,points:[{sequence:3,timestamp:99}]});
  assert.deepEqual(next.points.map(p => p.sequence), [2,3]);
  const replaced = mergeObservation(next, {...first,reset:false,points:[{sequence:2,timestamp:97}]});
  assert.equal(replaced.points[0].timestamp,97);
  assert.deepEqual(mergeObservation(next,first).points,first.points);
  assert.throws(() => mergeObservation(next,{...first,points:[{sequence:NaN,timestamp:1}]}));
});
