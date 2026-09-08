const { test } = require("node:test");
const assert = require("node:assert/strict");
const {
  verifyAppIdentity,
  verifyMetrics,
  verifyRunPublication,
  verifySettingsRestoration,
  disabledCloneSpec,
} = require("./browser_checks.cjs");
const { sanitize, adminPath, rangeOption } = require("./browser_session.cjs");

test("owned clone cleanup clears its hot floor and warm windows without changing source settings", () => {
  const original = {
    lifecycle: { desiredState: "Enabled" },
    availability: {
      minReplicas: 1,
      maxReplicas: 2,
      warmWindows: [{ minReplicas: 1 }],
    },
    artifact: { revision: "unchanged" },
  };
  const before = structuredClone(original);
  assert.deepEqual(disabledCloneSpec(original), {
    ...before,
    lifecycle: { desiredState: "Disabled" },
    availability: { minReplicas: 0, maxReplicas: 2, warmWindows: [] },
  });
  assert.deepEqual(original, before);
});

test("time-range commands select the actual numeric-hour UI options", () => {
  assert.deepEqual(["1h", "6h", "24h", "7d"].map(rangeOption), [
    "1",
    "6",
    "24",
    "168",
  ]);
  assert.throws(() => rangeOption("unknown"));
});

test("same model is not enough to prove independent apps", () => {
  const source = {
    app_id: "a",
    model_ref: "m",
    public_model_id: "m",
    deployment_name: "m",
  };
  assert.throws(() => verifyAppIdentity(source, source));
  assert.equal(
    verifyAppIdentity(source, {
      ...source,
      app_id: "b",
      public_model_id: "b",
      deployment_name: "b",
    }).status,
    "passed",
  );
});
test("missing series cannot pass as measured zero or nonzero", () => {
  const metrics = {
    app_id: "a",
    from_at: "2026-09-08T00:00:00Z",
    to_at: "2026-09-08T01:00:00Z",
    step_seconds: 60,
    charts: [
      "concurrent_requests",
      "gpu_utilization",
      "gpu_memory",
      "cpu",
      "memory",
      "containers",
      "ready_containers",
    ].map((id) => ({
      id,
      state: "unavailable",
      source: "prometheus",
      unit: "count",
      reason: "No samples",
      series: [],
      summary: { average: null, maximum: null },
    })),
  };
  assert.throws(() => verifyMetrics(metrics, { requireNonzero: true }));
  assert.equal(verifyMetrics(metrics).charts[0].samples, 0);
  metrics.charts[0].summary.average = 0;
  assert.throws(() => verifyMetrics(metrics));
});
test("unsampled publication is qualified honestly, not fabricated", () => {
  const published = {
    status: 200,
    at: "2026-09-08T00:00:05Z",
    body: {
      data: {
        operation: { id: "op" },
        scientific: {
          run: { status: "succeeded" },
          semantic_validation: { status: "passed" },
        },
      },
    },
  };
  assert.equal(
    verifyRunPublication([published], "op").transition_sampled,
    false,
  );
  const pending = structuredClone(published);
  pending.at = "2026-09-08T00:00:00Z";
  pending.body.data.scientific.semantic_validation.status = "not-run";
  assert.equal(
    verifyRunPublication([pending, published], "op").transition_sampled,
    true,
  );
});
test("restoration includes omitted settings rather than only displayed equivalence", () => {
  const original = {
    app_id: "a",
    app_revision: 1,
    display_name: "A",
    academic_required: false,
    serving: { spec: { availability: { minReplicas: 0 } } },
  };
  assert.equal(
    verifySettingsRestoration(original, { ...original, app_revision: 3 })
      .exact_spec_restored,
    true,
  );
  const changed = structuredClone(original);
  changed.serving.spec.availability.startupTimeoutSeconds = 900;
  assert.throws(() => verifySettingsRestoration(original, changed));
});
test("browser evidence removes structural secrets and credentials reflected in text", () => {
  const secrets = new Set(["temporary-secret-12345"]);
  assert.deepEqual(
    sanitize(
      {
        data: { secret: "unknown-new-secret", key: { id: "safe-id" } },
        message: "value temporary-secret-12345",
      },
      secrets,
    ),
    {
      data: { secret: "[redacted]", key: { id: "safe-id" } },
      message: "value [redacted]",
    },
  );
});
test("browser navigation stays on the authorized console routes", () => {
  const id = "11111111-2222-3333-4444-555555555555";
  assert.equal(
    adminPath(`/admin/apps/${id}/metrics?window=7d`),
    `https://89.169.99.188/admin/apps/${id}/metrics?window=7d`,
  );
  assert.throws(() => adminPath("https://external.invalid/admin/apps"));
  assert.throws(() => adminPath("/v1/models"));
  assert.throws(() => adminPath("/admin/apps?token=credential"));
});
