const {test} = require('node:test');
const assert = require('node:assert/strict');
const {requireCold, withRetention, requireOwnedChange} = require('./startup_retention_preflight.cjs');
const spec = {modelRef: 'cosmos3-nano', availability: {minReplicas: 0, maxReplicas: 1}, runtime: {image: 'unchanged'}};
const cold = {state: 'observed', observation: {status: {phase: 'Cold', replicas: {desired: 0, ready: 0, available: 0}}}};

test('only genuinely cold Cosmos with zero replicas is mutable', () => {
  requireCold(spec, cold);
  for (const field of ['desired', 'ready', 'available']) {
    const view = structuredClone(cold);
    view.observation.status.replicas[field] = 1;
    assert.throws(() => requireCold(spec, view));
  }
  assert.throws(() => requireCold({...spec, modelRef: 'qwen3-8b'}, cold));
  assert.throws(() => requireCold({...spec, availability: {minReplicas: 1}}, cold));
  assert.throws(() => requireCold(spec, {...cold, state: 'stale'}));
  assert.throws(() => requireCold(spec, {state: 'observed', observation: {status: {phase: 'RuntimeStarting'}}}));
});

test('only owned retention change is restored; original object remains untouched', () => {
  const changed = withRetention(spec, 1800);
  assert.equal(changed.availability.startupTimeoutSeconds, 1800);
  assert.equal(spec.availability.startupTimeoutSeconds, undefined);
  requireOwnedChange(changed, spec);
  requireOwnedChange(spec, spec);
  assert.throws(() => requireOwnedChange({...changed, runtime: {image: 'someone-else'}}, spec));
  assert.throws(() => requireOwnedChange(withRetention(spec, 3600), spec));
});
