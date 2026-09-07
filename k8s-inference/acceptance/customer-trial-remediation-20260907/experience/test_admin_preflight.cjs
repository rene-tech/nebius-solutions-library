// Offline assertions only: importing the helper never launches a browser or reads credentials.
const test = require('node:test');
const assert = require('node:assert/strict');
const {parseChecks, verifyPhaseRegression, PHASE_OPERATION} = require('./admin_preflight.cjs');

const commit = 'a'.repeat(40);
function fixture() {
  return {
    body: {meta: {generated_at: '2026-09-07T20:00:00Z'}, data: {lifecycle_phases: [
      {phase: 'restore', duration: {value: 3.946846, unit: 'seconds', evidence: 'estimated',
        source: 'lifecycle-signal-boundaries'}},
    ]}},
    observed: {
      restoreCard: 'restore\n3.95s estimated\nlifecycle-signal-boundaries',
      contextLabel: 'Cluster context checked 9/7/2026, 8:00:00 PM',
      runLabel: 'Run data observed 9/7/2026, 8:00:00 PM. Active runs update every 5 seconds.',
      phaseCopy: 'Observed wall-time union per phase. These values are not additive request latency or GPU-seconds.',
    },
  };
}

test('phase mode requires an exact confirmed identity and fixes the original operation', () => {
  assert.throws(() => parseChecks(['--phase-regression']), /root-confirmed/);
  assert.throws(() => parseChecks(['--phase-regression', '--source-commit=abcdef']), /40-character/);
  const parsed = parseChecks(['--phase-regression', `--source-commit=${commit}`]);
  assert.deepEqual(parsed.cases, [['protenix-v2', PHASE_OPERATION]]);
  assert.equal(parsed.sourceCommit, commit);
  assert.throws(() => parseChecks(['--phase-regression', `--source-commit=${commit}`,
    `rfdiffusion=${PHASE_OPERATION}`]), /only the retained/);
});

test('historical default and explicit existing-run checks remain supported without a guessed release', () => {
  assert.equal(parseChecks([]).cases.length, 3);
  assert.equal(parseChecks([]).sourceCommit, null);
  assert.deepEqual(parseChecks([`protenix-v2=${PHASE_OPERATION}`]).cases, [['protenix-v2', PHASE_OPERATION]]);
  assert.throws(() => parseChecks(['--unknown']), /model=operation-id/);
});

test('exact signal seconds and rounded visible browser seconds pass', () => {
  const {body, observed} = fixture();
  const result = verifyPhaseRegression(body, observed);
  assert.equal(result.status, 'passed');
  assert.equal(result.expected_seconds, 3.946846);
  assert.equal(result.operation_id, PHASE_OPERATION);
});

test('the original zero-ingestion duration, wrong source, or overstated measured quality fails', () => {
  for (const patch of [{value: 0}, {value: null}, {source: 'scientific-events'}, {evidence: 'measured'},
    {unit: 'gpu-seconds'}]) {
    const {body, observed} = fixture();
    Object.assign(body.data.lifecycle_phases[0].duration, patch);
    assert.throws(() => verifyPhaseRegression(body, observed));
  }
});

test('correct API alone cannot pass a stale browser card or ambiguous timestamp wording', () => {
  for (const patch of [
    {restoreCard: 'restore\n0s measured\nscientific-events'},
    {contextLabel: 'Updated 9/7/2026, 8:00:00 PM'},
    {runLabel: 'Run data observed Not observed. Active runs update every 5 seconds.'},
    {phaseCopy: 'Durations are request latency.'},
  ]) {
    const {body, observed} = fixture();
    Object.assign(observed, patch);
    assert.throws(() => verifyPhaseRegression(body, observed));
  }
});
