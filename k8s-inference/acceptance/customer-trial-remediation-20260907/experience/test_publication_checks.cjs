const test = require('node:test');
const assert = require('node:assert/strict');
const {verifyAutomaticPublication} = require('./publication_checks.cjs');
const id = 'example-operation';
const navigation = {status: 'passed', command: {action: 'navigate', path: '/admin/scientific-runs/' + id}};
function query(status, semantic, output = false) {
  return {path: '/admin/api/v1/scientific-runs/' + id, status: 200, at: '2026-09-08T07:00:00Z', body: {data: {
    run: {status}, semantic_validation: {status: semantic},
    artifacts: output ? [{role: 'output', download: {available: true}}] : [],
  }}};
}

test('actual captured pending-publication window followed by authorized output passes', () => {
  const proof = verifyAutomaticPublication([query('running', 'not-run'), query('succeeded', 'not-run'),
    query('succeeded', 'passed', true)], [navigation], id);
  assert.equal(proof.pending_publication_observed, true);
  assert.equal(proof.navigation_count, 1);
  assert.equal(proof.query_count, 3);
});

test('a race window not sampled remains explicitly unobserved', () => {
  const proof = verifyAutomaticPublication([query('running', 'not-run'), query('succeeded', 'passed', true)], [navigation], id);
  assert.equal(proof.pending_publication_observed, false);
  assert.match(proof.caveat, /No delayed-publication interval/);
});

test('manual-navigation recovery, already-finished reads, and unpublished output cannot pass', () => {
  const ready = query('succeeded', 'passed', true);
  assert.throws(() => verifyAutomaticPublication([query('running', 'not-run'), ready], [navigation, navigation], id));
  assert.throws(() => verifyAutomaticPublication([ready, ready], [navigation], id));
  assert.throws(() => verifyAutomaticPublication([query('running', 'not-run'), query('succeeded', 'not-run')], [navigation], id));
  assert.throws(() => verifyAutomaticPublication([query('running', 'not-run'), query('succeeded', 'passed')], [navigation], id));
});
