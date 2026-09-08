const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const assert = require('node:assert/strict');

function summarizeQueries(records) {
  const runs = new Map();
  for (const record of records) {
    const detail = record.body?.data;
    if (record.status !== 200 || !detail?.run?.id) continue;
    const run = detail.run;
    if (!runs.has(run.id)) runs.set(run.id, {operation_id: run.id, model_id: run.model.model_id,
      first_observed_at: record.at, query_count: 0, transitions: []});
    const result = runs.get(run.id);
    result.query_count++;
    result.last_observed_at = record.at;
    const state = {status: run.status, artifact_count: detail.artifacts.length,
      semantic_validation: detail.semantic_validation.status, shard_counts: run.queue.shard_counts,
      stages: detail.stages.map(stage => ({id: stage.id, status: stage.status,
        attempts: stage.attempts.map(attempt => ({id: attempt.id, number: attempt.number, status: attempt.status,
          phase: attempt.phase, phase_reason: attempt.phase_reason, resolved_pool_id: attempt.resolved_pool_id}))}))};
    const fingerprint = JSON.stringify(state);
    if (result.last_fingerprint !== fingerprint) {
      result.transitions.push({at: record.at, run_observed_at: record.body.meta.generated_at,
        ...state, restore: detail.lifecycle_phases.find(phase => phase.phase === 'restore')?.duration ?? null});
      result.last_fingerprint = fingerprint;
    }
    result.final_observed_accounting = run.gpu_accounting;
    result.final_observed_phases = detail.lifecycle_phases;
  }
  return [...runs.values()].map(({last_fingerprint, ...result}) => result);
}

function main() {
  const [privateDirectory, destination] = process.argv.slice(2);
  assert(privateDirectory && destination && !fs.existsSync(destination), 'private source and new public destination required');
  const read = name => JSON.parse(fs.readFileSync(path.join(privateDirectory, name), 'utf8'));
  const digest = name => crypto.createHash('sha256').update(fs.readFileSync(path.join(privateDirectory, name))).digest('hex');
  const session = read('session-report.json');
  assert(session.browser_closed_at, 'close the owned browser before final export');
  const queryFiles = fs.readdirSync(privateDirectory).filter(name => /^query-\d+\.json$/.test(name)).sort();
  const report = {schema: 'fs2-serve.nebius.ai/admin-cohort-observation/v1', source_commit: session.source_commit,
    scope: 'Existing bootstrap operator browser; no customer onboarding or policy/workload mutations.',
    started_at: session.started_at, signed_in_at: session.signed_in_at, browser_closed_at: session.browser_closed_at,
    observed_query_count: session.observed_queries, runs: summarizeQueries(queryFiles.map(read)),
    actions: session.actions, downloads: session.downloads, publication_checks: session.publication_checks ?? [],
    errors: session.errors, failed_requests: session.failed_requests,
    source_receipts: [{file: 'session-report.json', sha256: digest('session-report.json')},
      ...queryFiles.map(file => ({file, sha256: digest(file)})),
      ...session.actions.filter(action => action.status === 'passed').map(action => ({file: action.command.label + '.json',
        sha256: digest(action.command.label + '.json')}))],
    limitations: ['Automatic queries are produced by the UI itself; this helper does not poll the scientific API separately.',
      'Navigation and snapshots remain explicit in the action log; a manual navigation must not be counted as automatic recovery.',
      'Phase values are observed wall-time unions; GPU accounting is a separate ledger, not device utilization.',
      'This export preserves observations and does not automatically declare the customer cohort clean.']};
  fs.writeFileSync(destination, JSON.stringify(report, null, 2) + '\n');
  console.log(JSON.stringify({destination, runs: report.runs.length, queries: report.observed_query_count,
    sha256: crypto.createHash('sha256').update(fs.readFileSync(destination)).digest('hex')}));
}

module.exports = {summarizeQueries};
if (require.main === module) main();
