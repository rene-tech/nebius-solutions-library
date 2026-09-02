# Read-only admin BFF implementation and rollout handoff

The initial read-only slice owns six typed, versioned `GET` projections under
`/admin/api/v1`: context, overview, model list, model detail, operation list,
and operation detail. The same API now also owns session, access, capacity,
observability, configuration, and feature-gated ModelDeployment routes. Their
compact frontend handoff is `contracts/admin-api-v1.json`; FastAPI remains the
schema authority at `/internal/openapi.json`.

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
  and returns typed replica/serving-health/capacity summaries. An implementation
  must use an allow-listed informer/cache; it must not return Kubernetes objects
  or credentials. `CachedKubernetesAdminAdapter` supplies a 1..60 second,
  model-set-keyed cache around that typed boundary.
- `PrometheusAdminAdapter` accepts only canonical model IDs and a bounded time
  range. `PrometheusQueryTemplates` demonstrates the fixed server-owned PromQL
  vocabulary. There is no free-form PromQL endpoint and no principal, tenant,
  token, prompt, or response label.
- The existing ModelDeployment mutation-capabilities response is also the only
  Add Model configuration authority. Its `configuration_options` are derived
  from the installed `InfrastructureEnvelope` and carry exact qualified
  artifact, runtime, template, placement, queue, priority, and tenant defaults.
  The server emits only defaults accepted by the live validator and includes
  the envelope revision. Incomplete or unplaceable tuples are omitted; the
  browser does not invent a fallback or accept manually copied digests.

The default Kubernetes and Prometheus adapters still fail closed when live
sources are disabled. When the read adapters are enabled, production runtime
composition injects `KubernetesModelStateAdminAdapter` behind the bounded cache
and `PrometheusModelMetricsAdminAdapter` beside the capacity and observability
adapters. The Kubernetes adapter joins only canonical model IDs across
Deployments, Services, Pods, and GPU Nodes. The Prometheus adapter executes six
global and six `model`-grouped server-owned queries regardless of catalog size.
It never constructs one query per model.

Kubernetes reconciliation treats a Deployment plus Service at zero desired
replicas as cold; a non-failing rollout below its desired replica count as
loading; and only a Ready Pod selected by the model Service as ready/hot.
Replica failure, progress-deadline failure, crash loops, image pull failures,
and nonzero container exits are unhealthy. Missing Deployment or Service
evidence remains unknown. Catalog compatibility is evaluated separately and
still takes precedence as unsupported. The contract-compatible
`semantic_healthy` field in this adapter is therefore Kubernetes serving
health, not proof that an application-level semantic probe ran. Request-level
semantic outcomes remain durable operation evidence.

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

1. Configure `FS2_ADMIN_CONTEXT_PROJECT`, `FS2_ADMIN_CONTEXT_CLUSTER`, and
   `FS2_ADMIN_CONTEXT_REGION` together. `FS2_ADMIN_CONTEXT_LABEL` is optional.
2. Enable the Kubernetes reader and Prometheus URL. The model-namespace Role
   must allow `list` on `apps/deployments` and core `services` and `pods`; the
   ClusterRole must allow `list` on core `nodes`. Set
   `FS2_ADMIN_KUBERNETES_CACHE_TTL_SECONDS` within 1..60 seconds (default 15).
   The exact Prometheus service remains private.
3. Verify the bootstrap credential is accepted only by the same-origin session
   exchange and that every admin route then enforces the server-side operator
   session and role. Never place the bootstrap token in browser storage.
4. Generate frontend types from the immutable image's
   `/internal/openapi.json`, compare the fully enabled route profile with
   `contracts/admin-api-v1.json`, and fail the build on drift.
5. Reconcile the Helm/Terraform image digest and settings, render/plan first,
   then run same-origin host/origin, auth, partial-source, redaction, cursor,
   fixed-window, and non-placeholder live-state acceptance.

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
