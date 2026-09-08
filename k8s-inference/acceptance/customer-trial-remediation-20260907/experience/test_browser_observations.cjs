const test = require('node:test');
const assert = require('node:assert/strict');
const {summarizeQueries} = require('./export_browser_observations.cjs');

function response(at, status, artifacts, validation) {
  return {at, status: 200, body: {meta: {generated_at: at}, data: {
    run: {id: 'example-operation', model: {model_id: 'protenix-v2'}, status,
      queue: {shard_counts: {succeeded: status === 'succeeded' ? 1 : 0}}, gpu_accounting: {allocated: {value: 35}}},
    artifacts: Array(artifacts).fill({}), semantic_validation: {status: validation}, stages: [],
    lifecycle_phases: [{phase: 'restore', duration: {value: 4.196746, unit: 'seconds', evidence: 'estimated'}}],
  }}};
}

test('export retains the terminal-without-results race and later publication as separate observations', () => {
  const result = summarizeQueries([
    response('20:13:00', 'running', 0, 'not-run'),
    response('20:13:05', 'running', 0, 'not-run'),
    response('20:13:33', 'succeeded', 0, 'not-run'),
    response('20:14:14', 'succeeded', 9, 'passed'),
  ]);
  assert.equal(result[0].query_count, 4);
  assert.deepEqual(result[0].transitions.map(row => [row.status, row.artifact_count, row.semantic_validation]),
    [['running', 0, 'not-run'], ['succeeded', 0, 'not-run'], ['succeeded', 9, 'passed']]);
  assert.equal(result[0].transitions[1].restore.value, 4.196746);
  assert(!JSON.stringify(result).includes('last_fingerprint'));
});

test('capability responses and failed queries are not mistaken for completed runs', () => {
  assert.deepEqual(summarizeQueries([{status: 200, body: {data: {items: []}}},
    {...response('20:13:33', 'succeeded', 9, 'passed'), status: 503}]), []);
});
