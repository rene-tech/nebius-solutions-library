# Historical metrics must not starve admission

Task: `fs2-metrics-ledger-scrape-isolation-r20261005`, child of the Lynx
long-run/restart acceptance. This is a bounded reporting repair, not another
observability system or a platform-wide readiness claim.

## Preserved diagnosis — 2026-10-05 17:54–17:56 UTC

Read-only inspection of `fs2-control-db-1` in `fs2-data`, context
`nebius-mk8s-k8s-inference-h100-e00j5z9te7x5dd9g6a`, found:

- API generation379 has three replicas of image
  `sha256:64c5c77d4eb6cac4b4ecf58d16766b2aa0423f836c1b9adee5a2034abd3e3644`.
- One Prometheus has three API targets, each at5s interval and5s timeout.
  Observed scrape durations were1.690512s,5.044910s (deadline exceeded),
  and1.732695s. The failed scrape remains a failure.
- `stages/workloads/control_plane.tf` selects that cadence for KEDA; the Helm
  chart's general default is30s. Slowing all metrics would slow cold admission.
- `api.py:collect_metrics` executed terminal totals, live queue counts/age,
  request semantics, customer outcomes, GPU phases and lifecycle totals on every
  scrape. `InFlightMetricsRead` coalesced overlapping HTTP calls in one Pod,
  but every subsequent scrape and every other Pod scanned again.
- `PostgresRequestTelemetryStore.semantic_metric_rows` has a full-history
  five-field `GROUP BY`. `pg_stat_user_tables` estimated1,793,787 live rows.
  Heap size was320,323,584bytes; total including indexes was663,175,168bytes.
  Stats are estimates, not an exact row-count pass.
- Existing request-ledger indexes cover primary key, model/time, owner/time,
  operation/time and semantic outcome/admission/time. None eliminates this
  full-history aggregation. Estimated plan: parallel sequential scan,
  partial hash aggregate, Gather Merge/final aggregate;2 planned workers,
  total planner cost271,227.65 (planner units, not milliseconds).
- `PostgresLifecycleRepository.metric_rows` aggregates
  `fs2_reporting_gpu_phase_usage`: latest `DISTINCT ON` rollup per subject,
  terminal filter, subject join and JSON phase expansion. Rollups estimated
  34,312live rows and48,463,872bytes total. The exact latest-rollup index exists,
  but this plan still chose a parallel full scan/sort;2 planned workers,
  total estimated cost82,822.11.
- One activity sample contained one semantic-query client backend and two
  parallel workers with the same leader PID. Six matching activity rows do
  **not** prove six independent client scans. Parent separately owns the
  concurrently observed scientific claim-scan defect.

Inspection used `BEGIN READ ONLY`,3s statement timeout, catalog statistics,
`pg_stat_activity` with fixed query-class projection and `EXPLAIN` without
`ANALYZE`. No request bodies, arbitrary SQL values, credentials or customer keys
were collected. No production mutation or inference was performed.

## Change

The existing5s live scrape remains. Queue counts, oldest queue age and pool/model
metadata are read fresh before response; a failed live read still fails the
scrape. KEDA never receives a cached queue or a zero invented for an error.

Only historical accounting runs as one background refresh per API process:

- at most one refresh per30s, including failed refreshes;
-3s total asynchronous collection budget, not3s per query;
- one complete sample published atomically after all historical reads succeed;
- sample age measured from before its first query, not after completion;
- no scrape waits for historical work;
- data less than30s old may be projected; expired/missing historical metric
  families are omitted rather than zeroed or silently reported as fresh;
- `fs2_serve_historical_accounting_available`, `_age_seconds`,
  `_sampled_timestamp_seconds` and `_refresh_in_progress` disclose the state.
  Age/timestamp are absent until any successful observation exists;
- failed refreshes log only exception class and retry at the next30s interval;
- shutdown cancels/drains the one task; cancelled scrapers cannot duplicate it.

Terminal, semantic, customer-outcome and lifecycle aggregate methods use read-only
transactions with `SET LOCAL max_parallel_workers_per_gather=0` and local3s
statement timeout. These settings do not escape into admission connections.
No index, migration, retention, key limit, resource size, scrape cadence,
Gateway, customer workload or public-model change is included.

Compared with the previous3×5s schedule, successful steady-state refresh
frequency is bounded to at most3×30s: six times fewer historical collection
starts at the current replica count. This is an arithmetic scheduling bound,
**not** a measured sixfold latency/CPU improvement or a cluster-wide singleton.
Scans still depend on retained ledger size. If they cannot complete within the
budget, accounting becomes explicitly unavailable; queues remain independent.
Incremental/shared database rollups would be separate future work, not claimed
implemented here.

This deliberately supersedes only the historical-accounting freshness contract
in `acceptance/reporting-contention-20260919/README.md`; its failure evidence and
in-flight sharing for live reads remain intact.

## Source verification

Focused non-PostgreSQL suite:87passed,1deselected. Includes real local ASGI/MCP
contracts, blocked background refresh with immediate fresh queue/age responses,
stale-family omission, failure backoff, cancellation, shutdown and accounting
identity. Ruff passes; mypy passes the helper and telemetry modules.

Actual PostgreSQL suite:9passed,50unselected tests. Uses the task-owned local
database `fs2_metrics_accounting_tests` in the existing local test container,
not the live database or siblings' test databases. Covers read-only local
settings, cancellation/pool recovery, full-history semantic parity including a
90-day-old observation and duplicate delivery, existing transport usage,
terminal/customer accounting and both lifecycle aggregate methods.

The first PostgreSQL run was5pass/1fail: an existing fixture inserted only
`mcp_is_error=true` while the already-existing usage query counts classified
`semantic_outcome=failed`. The fixture now emits the complete middleware
observation and explicitly asserts both failed and pre-admission totals.
Production usage SQL was not changed. This initial failure is not suppressed.

## Coordinated live qualification still required

The parent release owner must deploy the tested immutable successor. Verify
all exact-image API readers, queue/age series at the original cadence, a
successful historical sample within budget, expiry/failure behavior and
PostgreSQL aggregate leader/worker counts. Repeat the real20k-file acceptance
and preserve all history/readiness failures. Source tests alone do not close
the observed shared-production availability defect.

Rollback is the prior image, not an alteration to queues, limits or the ledger.
