const assert = require('node:assert/strict');

/** Verify captured UI queries, never create a replacement API polling loop. */
function verifyAutomaticPublication(queries, actions, operationId) {
  const pagePath = '/admin/scientific-runs/' + operationId;
  const rows = queries.filter(row => row.path === '/admin/api/v1/scientific-runs/' + operationId && row.status === 200);
  const navigations = actions.filter(row => row.status === 'passed' && row.command.action === 'navigate' && row.command.path === pagePath);
  assert.equal(navigations.length, 1, 'automatic publication requires one initial navigation, no manual recovery');
  assert(rows.length >= 2, 'capture an initial state and a later UI-generated query');
  const first = rows[0];
  const last = rows.at(-1);
  assert(first.body.data.run.status !== 'succeeded' || first.body.data.semantic_validation.status === 'not-run',
    'opening an already-published run does not prove automatic publication');
  assert.equal(last.body.data.run.status, 'succeeded');
  assert.equal(last.body.data.semantic_validation.status, 'passed');
  assert(last.body.data.artifacts.some(item => item.role === 'output' && item.download?.available),
    'a real authorized output must be published');
  const publicationWindow = rows.filter(row => row.body.data.run.status === 'succeeded'
    && row.body.data.semantic_validation.status === 'not-run');
  return {operation_id: operationId, status: 'passed', query_count: rows.length,
    first_observed_at: first.at, published_observed_at: last.at,
    pending_publication_observed: publicationWindow.length > 0,
    pending_publication_query_times: publicationWindow.map(row => row.at),
    navigation_count: navigations.length, artifact_count: last.body.data.artifacts.length,
    caveat: publicationWindow.length ? null : 'No delayed-publication interval was sampled; automatic completion was observed.'};
}

module.exports = {verifyAutomaticPublication};
