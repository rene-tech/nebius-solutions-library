import assert from 'node:assert/strict';
import test from 'node:test';
import {CommandGate, newestRun, submissionState} from '../src/fs2_workshop/static/controls.js';

const run = (status, takeover = 'patient', next = 'patient', version = 4) => ({id: 'run', version, status, state: {takeover_role: takeover, next_role: next}});
test('typed Say requires actual takeover and selected/current role match', () => {
  for (const status of ['queued', 'running', 'paused', 'interrupted', 'completed', 'failed', 'aborted']) {
    assert.equal(submissionState(run(status), 'patient').canSay, false);
  }
  assert.equal(submissionState(run('takeover'), 'patient').canSay, true);
  assert.equal(submissionState(run('takeover'), 'clinician').canSay, false);
  assert.equal(submissionState(run('takeover', 'patient', 'clinician'), 'patient').canSay, false);
  assert.equal(submissionState(null, 'patient').canSay, false);
});
test('polling cannot reenable submissions while a command is pending; duplicate work is rejected', async () => {
  const gate = new CommandGate(); let finish; let calls = 0; const states = [];
  const changed = () => states.push(submissionState(run('takeover'), 'patient', gate.busy));
  const pending = gate.run(async () => { calls++; await new Promise(resolve => { finish = resolve; }); }, changed);
  assert.equal(gate.busy, true);
  changed(); // Same render path used by a concurrent refresh.
  assert(states.every(state => state.blocked && !state.canSay && !state.canRecord));
  assert.equal(await gate.run(() => { calls++; }), false);
  assert.equal(calls, 1); finish(); await pending;
  assert.equal(gate.busy, false); assert.equal(states.at(-1).canSay, true);
});
test('HTTP error clears the command gate and allows a later attempt', async () => {
  const gate = new CommandGate(); const states = [];
  await assert.rejects(gate.run(async () => { throw new Error('HTTP409'); }, () => states.push(gate.busy)), /409/);
  assert.deepEqual(states, [true, false]); assert.equal(gate.busy, false);
  assert.equal(await gate.run(async () => {}), true);
});
test('stale in-flight polling cannot replace a newer intervention result', () => {
  const completed = run('queued', 'patient', 'clinician', 5);
  assert.equal(newestRun(completed, run('takeover', 'patient', 'patient', 4)), completed);
  assert.equal(submissionState(newestRun(completed, run('takeover')), 'patient').canSay, false);
  const newRun = {...run('takeover'), id: 'another-run'};
  assert.equal(newestRun(completed, newRun), newRun);
});
