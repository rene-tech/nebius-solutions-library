# Read-only admin BFF implementation and rollout handoff

The control plane now owns six typed, versioned `GET` projections under
`/admin/api/v1`: context, overview, model list, model detail, operation list,
and operation detail. Their compact frontend handoff is
`contracts/admin-api-v1.json`; FastAPI remains the schema authority at
`/internal/openapi.json`.

Every successful response is `{meta,data}`. `meta` carries the selected
server-authorized project/cluster/region/time context, generation time,
per-source freshness, and bounded partial-data warnings. Every numeric product
field is `{value,unit,state,source,reason}`. Missing TTFT, tokens, DCGM measured
GPU time, cache residency, snapshot timing, or adapter data is `null` with a
reason; it is never emitted as numeric zero.

## Data boundaries

- The canonical catalog supplies model identity, immutable runtime identity,
  compatibility, protocols, relative public endpoints, and MCP exposure.
- PostgreSQL supplies payload-free operation records, exact terminal-window
  totals, current durable queue counts, and activation phase. All SQL is static,
  positional, time-bounded, and cursor-bounded. Selected columns exclude
  encrypted content, HMACs, idempotency keys, trace context, backend error
  detail, Pod/node/GPU UUIDs, and raw credentials.
- `KubernetesAdminAdapter` accepts only a bounded tuple of canonical model IDs
  and returns typed replica/semantic-health/capacity summaries. An implementation
  must use an allow-listed informer/cache; it must not return Kubernetes objects
  or credentials. `CachedKubernetesAdminAdapter` supplies a 1..60 second,
  model-set-keyed cache around that typed boundary.
- `PrometheusAdminAdapter` accepts only canonical model IDs and a bounded time
  range. `PrometheusQueryTemplates` demonstrates the fixed server-owned PromQL
  vocabulary. There is no free-form PromQL endpoint and no principal, tenant,
  token, prompt, or response label.

The default Kubernetes and Prometheus adapters fail closed as unavailable.
That default is intentional: installing this code alone cannot invent live
state or acquire cluster/metrics credentials. The capacity/observability
workstream owns the cached live implementations and their service-account and
network-policy reconciliation.

## Status and failure behavior

The model projector applies the sealed precedence:
`unsupported > unknown > unhealthy > loading > hot > queued > cold`.
Canonical `qualified` and retained-route `lean-live-verified` are the only
accepted compatibility states; `enabled` alone is not compatibility evidence.
Hotness requires fresh catalog, PostgreSQL, and Kubernetes evidence, an
explicit passing semantic-health result, and a ready replica. A missing health
result is `unknown`; ready replicas alone are insufficient.

Independent adapter failure or staleness keeps the response usable, marks the
source, emits a bounded warning, and makes affected model state `unknown`.
Operation reporting has no alternate durable authority, so its unavailable
path returns sanitized RFC 9457-style `application/problem+json`. Backend
exception text is never reflected. Every BFF problem carries a new
server-generated UUID in both `request_id` and `x-request-id` for safe support
correlation.

## Reconciled rollout handoff

Before deploying the admin UI, the retained release owner must:

1. Construct `AdminReadService` in the control-plane runtime with one
   `AdminContextConfig` derived from runtime configuration, not hard-coded
   region or GPU values.
2. Inject the reviewed Kubernetes cache and bounded Prometheus adapter from the
   capacity/observability workstream. The service account must remain GET/LIST/
   WATCH-only for its exact allow-list.
3. Replace the temporary bootstrap-admin dependency with the operator-session
   and RBAC dependency owned by the access workstream. Never place the bootstrap
   token in browser JavaScript or storage.
4. Generate frontend types from the immutable image's
   `/internal/openapi.json`, compare the six routes with
   `contracts/admin-api-v1.json`, and fail the build on drift.
5. Reconcile the Helm/Terraform image digest and settings, render/plan first,
   then run same-origin host/origin, auth, partial-source, redaction, cursor,
   and fixed-window acceptance. No shared-cluster rollout is part of this
   implementation slice.

## Verification

From `k8s-inference/components/control-plane`:

```bash
PYTHONPATH="../catalog" uv run pytest -q
uv run ruff check src tests/test_admin_read.py
uv run ruff format --check src tests/test_admin_read.py
uv run mypy src/fs2_serve
```

The PostgreSQL integration case is
`test_admin_reporting_queries_are_bounded_paginated_and_payload_free`; set
`FS2_TEST_DATABASE_URL` to run it against an isolated database.
