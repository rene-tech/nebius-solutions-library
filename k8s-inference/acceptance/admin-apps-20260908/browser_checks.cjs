// Assertions for real captured admin responses. No requests, retries or mutations.
const assert = require("node:assert/strict");

function verifyAppIdentity(source, clone) {
  assert.notEqual(clone.app_id, source.app_id, "independent app ID required");
  assert.equal(clone.model_ref, source.model_ref, "same source model required");
  assert.notEqual(
    clone.public_model_id,
    source.public_model_id,
    "independent public route required",
  );
  assert.notEqual(
    clone.deployment_name,
    source.deployment_name,
    "independent serving deployment required",
  );
  return {
    source_app_id: source.app_id,
    clone_app_id: clone.app_id,
    model_ref: source.model_ref,
    source_route: source.public_model_id,
    clone_route: clone.public_model_id,
    status: "passed",
  };
}

function verifyMetrics(metrics, { requireNonzero = false } = {}) {
  assert(
    metrics.app_id &&
      metrics.from_at &&
      metrics.to_at &&
      metrics.step_seconds > 0,
  );
  const ids = new Set(metrics.charts.map((chart) => chart.id));
  for (const id of [
    "concurrent_requests",
    "gpu_utilization",
    "gpu_memory",
    "cpu",
    "memory",
    "containers",
    "ready_containers",
  ]) {
    assert(ids.has(id), `missing required chart ${id}`);
  }
  const records = metrics.charts.map((chart) => {
    const points = chart.series.flatMap((series) => series.points);
    for (const point of points)
      assert(
        point.value === null || Number.isFinite(point.value),
        "only numeric or null observations",
      );
    if (chart.state === "unavailable")
      assert(
        chart.summary.average === null && chart.summary.maximum === null,
        "unavailable summary must remain unknown",
      );
    return {
      id: chart.id,
      state: chart.state,
      source: chart.source,
      unit: chart.unit,
      samples: points.filter((point) => point.value !== null).length,
      missing: points.filter((point) => point.value === null).length,
      nonzero: points.filter((point) => point.value !== null && point.value > 0)
        .length,
      average: chart.summary.average,
      peak: chart.summary.maximum,
      reason: chart.reason,
    };
  });
  if (requireNonzero)
    assert(
      records.some((row) => row.nonzero > 0),
      "no actual nonzero metric observations",
    );
  return {
    app_id: metrics.app_id,
    from_at: metrics.from_at,
    to_at: metrics.to_at,
    step_seconds: metrics.step_seconds,
    charts: records,
  };
}

function verifyRunPublication(responses, operationId) {
  const matching = responses.filter(
    (row) => row.status === 200 && row.body?.data.operation?.id === operationId,
  );
  const pending = matching.find(
    (row) =>
      row.body.data.scientific?.run.status === "succeeded" &&
      row.body.data.scientific.semantic_validation.status === "not-run",
  );
  const published = matching.find(
    (row) => row.body.data.scientific?.semantic_validation.status === "passed",
  );
  assert(published, "validated publication must be actually observed");
  return {
    operation_id: operationId,
    terminal_unpublished_observed_at: pending?.at ?? null,
    published_observed_at: published.at,
    transition_sampled: Boolean(pending),
    caveat: pending
      ? null
      : "Terminal-unpublished interval was not sampled; do not claim this exact transition was observed.",
  };
}

function verifySettingsRestoration(before, after) {
  assert.deepEqual(
    after.serving?.spec ?? null,
    before.serving?.spec ?? null,
    "restore complete desired spec, including omitted optional fields",
  );
  assert.deepEqual(
    after.scientific?.desired.startup_policies ?? null,
    before.scientific?.desired.startup_policies ?? null,
  );
  assert.equal(after.display_name, before.display_name);
  assert.equal(after.academic_required, before.academic_required);
  return {
    app_id: before.app_id,
    previous_revision: before.app_revision,
    restored_revision: after.app_revision,
    exact_spec_restored: true,
  };
}

module.exports = {
  verifyAppIdentity,
  verifyMetrics,
  verifyRunPublication,
  verifySettingsRestoration,
};
