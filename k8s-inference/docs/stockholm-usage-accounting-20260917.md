# Stockholm usage accounting: projection and reconciliation

Status: implemented, locally tested and initially deployed; **not customer-qualified**.
This is a truthful accounting projection, not a pricing or billing system.
The reported historical discrepancy (22 admission GPU-hours versus approximately
0.1604 lifecycle occupied GPU-hours) has not been independently reconstructed:
the requested September 9–11 window has no retained operations. Do not treat
that earlier task description as a new measured report.

## Contract and compatibility

| Public value | Meaning | Not equivalent to |
| --- | --- | --- |
| `gpu_seconds_budget` | Existing admission-policy limit; input name and enforcement unchanged | A purchased/measured GPU allocation |
| `admission_budget_consumed_gpu_seconds` | Lifetime conservative admission charges, historically stored as `gpu_seconds_used` | Actual occupied time, active compute, or money |
| `admission_budget_reserved_gpu_seconds` | Admission budget currently held for unfinished work | Kubernetes/device occupancy |
| `conservative_attempted_gpu_seconds` (admin key usage) | Existing terminal ledger's conservative attempted allocations | Measured GPU consumption |
| `scheduler_occupied_gpu_seconds` (owner usage) | Complete reconciled exclusive scientific-attempt occupancy, quality explicitly retained | Shared online-serving GPU utilization or a bill |
| `occupied_idle_gpu_seconds` (owner usage) | Classified resident-idle, workflow-wait and cooldown phases only | The historical occupied-minus-active residual |

The token and admin-key response fields formerly called `gpu_seconds_used` and
`gpu_seconds_reserved` now serialize with the admission-budget names above.
Admin-key `usage.estimated_gpu_seconds` serializes as
`usage.conservative_attempted_gpu_seconds`. Deploy CP and admin-console together;
external response consumers must update those field names. Token create/patch
inputs, quotas, admission enforcement, rotation inheritance and stored counters
are unchanged. Existing internal Python/persistence field names remain readable.
No migration rewrites historical rows.

Users expose `usage.lifecycle_accounting`: source, attribution, worst retained
quality, subject/operation coverage, occupancy completeness, phase completeness,
and occupied/active/classified-idle/startup/other/unknown/queue measurements.
Apps expose corresponding `scientific_gpu` values with quality and completeness.
Available occupied evidence remains visible when phase classification is partial;
known phase sums are marked incomplete/estimated, not complete active/idle totals.
Missing or unreconciled exclusive occupancy produces null, never fabricated zero.

Startup includes image pull, artifact load, restore, compile and warmup.
Other includes checkpoint/drain and teardown. Unknown includes explicitly
unclassified occupancy and residual or unrecognized historical phase time.
These categories partition recorded occupied time; the historical rollup's
`occupied_idle_gpu_seconds` remains untouched and is exported separately as
`legacy_nonactive_residual_gpu_seconds`, never relabeled as classified idle.
Queue is explicitly unavailable in seconds: no complete owner-level queue
interval projection exists, and waiting wall time is not GPU occupancy.

Online request intervals can overlap on the same worker. Their per-subject
evidence remains readable but is excluded from additive owner/app occupancy.
Existing exclusive scientific attempts are counted once per immutable subject,
including failed and retried attempts. No admission estimate is added to measured
occupancy. Accepted-in-window cohorts include full recorded attempt lifetimes;
these are not time-clipped fleet occupancy reports.

## Historical read-only export

Run from `components/control-plane` using the exact intended source/image and
an existing appropriately scoped read-only database connection. Supply its DSN
through `FS2_USAGE_DATABASE_URL` using the operator's protected environment;
never put it in command arguments, a report, or Git.

```bash
python -m fs2_serve.usage_reconciliation \
  --tenant STOCKHOLM_TENANT \
  --from 2026-09-15T00:00:00+00:00 \
  --to 2026-09-16T00:00:00+00:00 \
  --principal EXACT_OWNER --model EXACT_MODEL
```

Replace tenant, owner, model and timestamps with the authorized exact cohort;
the example is not evidence of a real Stockholm query. `--key` optionally limits
one opaque key ID. The tool prints a JSON report for protected operator capture.
It uses a repeatable-read **read-only** transaction, only SELECT statements,
and a 30-second command timeout. It never migrates, calls reconciliation writers,
updates counters, or downloads workload payloads. At most 10,000 operations,
attempts and key snapshots are accepted; exceeding a bound fails instead of
printing misleading partial totals. Narrow the cohort before retrying.

The report contains operation/attempt/tenant/owner/key/model identities, preserved
rollup IDs and event digests, outcomes (including failures), coverage gaps,
per-phase values and source quality. Operations with no lifecycle rows remain
visible. A latest immutable snapshot is used once per subject; conflicting
duplicate snapshots are refused. Original rollups/counters are not changed.

Admission snapshots are lifetime values taken at export time, even when the
lifecycle cohort has a narrower window or model filter. Key rotations can
inherit counters. **Do not sum these snapshots across keys or divide them by a
narrow-window lifecycle total as a billing ratio.** The export intentionally
does not invent such a ratio, currency amount, credit, or retrospective bill.

Terraform outputs continue to publish endpoints and admission policy settings,
not measured usage. Interpret any existing `gpu_seconds_budget` output as an
admission limit; use this read-only cohort report for lifecycle evidence.
No Terraform, quota, GPU capacity or cloud resource change is required by this
projection implementation.

## Verification and remaining live gates

Local tests cover admission-response aliases; retained historical counters;
success/failure/retry deduplication; shared-serving overlap exclusion; missing
subjects/rollups; partial phases; quality preservation; read-only export bounds;
and parity between PostgreSQL owner/app projections and the report. Fixtures
are synthetic and local SQL tests do not prove Stockholm usage or GPU behavior.

2026-09-17 verification in `agent/fs2-cosmos-stockholm-remediation-r20260917`:

- `pytest -q tests/test_usage_accounting.py tests/test_users.py tests/test_admin_access_api.py tests/test_lifecycle.py tests/test_apps.py`: **49 passed**.
- Isolated task-owned local PostgreSQL16 database `mindguard_usage`, `pytest -q tests/test_users_apps_postgres.py`: **10 passed**, including report/owner/app parity, retained failed retries and unchanged original rows/counters.
- Admin-console `npm run typecheck`: passed; `npm run test:run`: **223 passed across 35 files**.
- Ruff on accounting/access/user/app projection files and dedicated tests: passed. Focused mypy on the two new modules plus user repository/models: passed. `git diff --check`: passed.

The first API-test collection using the older canonical virtualenv lacked
`websockets`; it was rerun successfully using the existing September-16 gateway
virtualenv with this integration tree's `PYTHONPATH`. Test teardown also reported
pre-existing `/tmp/pytest-of-tux` ownership cleanup warnings; no broad cleanup or
permission changes were attempted. Neither warning is a live platform result.

Before customer readiness: run the two unchanged-release customer-equivalent canary cohorts required by
`CUSTOMER_RELEASE_POLICY.md`. Capture tenant/key scope, endpoint, image/config
identities, operation/attempt/lifecycle evidence and recent speech/storage/model
regression results. No such live customer pass is claimed here.

## September 17 read-only live verification

Authorized sandbox2 access was restored. The coordinated initial CP/admin
images from source `b33d73149` reached CP 3/3 and admin 2/2 Ready. The deployed
schema contains 32 migrations. Scheduled maintenance (not manually triggered)
completed on the candidate image, including the 13:44 UTC job. No customer
records, quotas or lifecycle/token counters were changed by this verification.

The SELECT-only exporter ran inside a candidate CP pod with its existing DSN
held only in process memory. Both exports use tenant `stockholm`, no principal
filter and the 10,000-record refusal bound; full outputs remain protected:

| Accepted window (UTC, end exclusive) | Operations | Lifecycle subjects | Result |
| --- | --- | --- | --- |
| September 9–11 | 0 | 0 | Requested event cohort absent from retained operations; no inferred historical total |
| September 11–13 | 14 inference | 14 | Separate retained-activity cohort: seven succeeded, seven failed |

The second cohort excludes one artifact-upload operation. All 14 subjects have
reconciled `application_observed` rollups and are shared online-serving work.
They are explicitly excluded from additive owner GPU totals. No exclusive
scientific occupancy exists for that cohort; occupied/active/idle/startup/unknown
owner measurements remain unavailable, not zero. Twenty lifetime admission-key
snapshots are retained for context and must not be summed across rotations or
compared as a billing ratio with this narrower time window.

Protected directory:
`/home/tux/secure-handoff/cosmos-stockholm-build-20260917-z7qpSC`.
Requested-window report SHA-256:
`6df0bd6509a89ffd881133d6d017a63f561e8b54d2e18d9ec2d30c02dac8b1fd`.
Retained-activity report SHA-256:
`5caa737f564178502ff6e9f249b7fc3a14bd0857fdfb96a49593c54f52e4287a`.
These reports contain opaque account/workload identities but no prompts, model
outputs or credentials; do not publish their full contents as public evidence.

The live `/metrics` check exposed HTTP 500: the new immutable terminal-fact
aggregation required five SELECT columns absent from the runtime-role grants.
Exact failure was reproduced on local PostgreSQL under the restricted runtime
role. Correction `d586613f1` adds only `tenant_id`, `model_id`, `protocol`, `status`
and `occurred_at` SELECT access and startup checks for the group and login role.
Full-table reads, principal/free-form outcome reads and ledger mutations remain
denied. Seven real-PG role/retention/readiness tests and 12 metrics/release tests
passed; lint and whitespace checks passed. Migration32 and its canonical contract
hash are unchanged. The migration owner must reapply the grant setup during
the normal release hook; every CP replica must then return `/metrics` HTTP 200.

That correction was verified live at **2026-09-17 13:54:23 UTC**. All three CP
replicas on `sha256:f96d890a19fb917322f5986d1ee81b1d5911643e35b62d76a46503c5f35654f8`
passed schema init, were Ready without restarts and returned `/metrics` HTTP200.
Each exposed 163 customer-operation series and 146 semantic-exchange series.
Read-only privilege checks confirmed all five required column grants and denied
full-table SELECT, ledger writes, principal IDs and free-form outcomes.
The ordinary 13:54 maintenance job completed successfully at 13:54:14 on the same
corrected image. No manual maintenance operation was run.

The retained terminal-fact day counts independently also had no September9–10
Stockholm rows. At this verification instant the all-history metrics showed
seven failed and eleven succeeded terminal operations, including three new
September 17 successes and artifact-upload facts; this is not the fourteen-row
historical inference cohort and is not a billing total. All CP replicas project
the same ledger, so Prometheus aggregation must deduplicate replicas, not sum them.
Protected reproducible check and receipt are `verify_live_readonly.py` and
`live-metrics-verification.json` in
`/home/tux/secure-handoff/cosmos-stockholm-metrics-build-20260917-YZMLav`.
This passes the metrics/maintenance deployment gate only, not customer/GPU acceptance.

At 13:56:16 UTC, the existing internal Prometheus API confirmed all three scrape
targets were `up` with empty scrape errors, and actual queries contained both
new metric families on every corrected replica (163 customer series and 146
semantic series each). The protected `prometheus-verification.json` receipt has
SHA-256 `01459b22e780cddcf9c39c8468d5760d44ed567a1faf24b1b40cadf02e984701`.

That same check found a separate rule-selection defect: the deployed
`PrometheusRule` had no `release` label, while Prometheus selected only
`release=fs2-r927c465c6d-monitoring`. The resource contained all 13 rules, but none
of its group was loaded. The chart now supports `prometheusRule.labels: {}`;
Terraform sets its `release` label from the existing `fs2-${var.run_id}-monitoring`
foundation identity. This does not widen the Prometheus selector or change alert
thresholds. Four targeted Helm/render/PromQL tests passed, including a full render
comparison showing that only rule metadata changes. Terraform formatting passed;
full validation was unavailable because this workloads tree lacks initialized
modules. No Terraform apply, alert firing or failure injection was performed by
this verification lane.

After the release owner applied that exact label, the 14:00:39 UTC check confirmed
`fs2-serve-control-plane.rules` loaded all 13 rules, with a 30-second evaluation
interval and last evaluation at 14:00:21. Every rule had `health=ok` and no
evaluation error. `Fs2ServePublicSemanticFailureSpike` and
`Fs2ServeCustomerOperationFailureRate` were both inactive. Existing lifecycle
reconciliation/unclassified-occupancy and three certificate alerts were pending;
successful rule loading is not an assertion that those conditions are healthy.
No alert firing or failure injection was performed. Protected
`prometheus-rules-after-label.json` SHA-256:
`4a38ca455b9e2a6d09c69b581400723c62971d9bcaee358a20ba63bcb8e66d1a`.
The earlier ignored-rule and metrics500 receipts remain retained; this result
does not erase them or replace the separate customer/GPU qualification gates.

## Final retained-contract deployment readback

At **2026-09-17 14:29:32 UTC**, the unchanged corrected CP image `f96d890a...`
was Ready on all three new replicas after the complete 20-model additive Cosmos
contract switch; controller and admin were Ready 2/2 each. Every CP mount had
20 model qualifications, 23 bundle revisions and all five voice models. Static
lean routes are not the controller-owned dynamic discovery projection; final
public sibling discovery is recorded separately by the Stockholm acceptance lane.

All three `/metrics` endpoints returned HTTP 200 with 166 customer-operation and
155 semantic-exchange series each. Actual Prometheus targets were up without
scrape errors, both families were present for every current pod, and all 13
rules evaluated with `health=ok` and no evaluation errors. Both new failure-rate
rules were inactive. Existing lifecycle reconciliation/unclassified occupancy
and certificate NotReady/ExpiresSoon alerts were **firing**; certificate renewal
overdue was pending. Rule evaluation health is not health of those conditions.
GPU-observer coverage was **13/15 Ready, 15/15 updated**, explicitly incomplete.
No alerts, workloads, customer records, keys or counters were changed by this
read-only verification.

The release owner's nine public read-only surfaces returned HTTP 200 at
14:28:09 UTC; this receipt was reused, not a duplicate browser/inference run.
Protected final verification:
`/home/tux/secure-handoff/cosmos-stockholm-rollout-20260917/final-deployment-verification-r142.json`,
SHA-256 `32fde8d007eeaba2982aa330e4eae5785d7347c6be3b6056d237f534aad29692`.
It references `surfaces-r142/surface-receipt.json`, SHA-256
`f5f65cc9d6ad40d3b0046f51a54bddd85d750a4d64e7b24424a61cb4d8a28625`.
The expected final Helm history is 142; release completion is owned separately.
This verifies deployment/telemetry availability, not complete GPU observation,
historical bill reconstruction, model inference quality or customer readiness.

## Known gap: serving attribution with multiple Ready replicas

The ordered release-142 cohort retained 13 successful serving operations with
empty runtime identities. This is consistent with, and was reproduced through,
the existing `KubernetesRuntimeMetadataProvider` ambiguity behavior: it resolves
by model ID only, discards `operation_id`, and returns no observation unless
exactly one non-deleting Ready model pod exists. `RuntimeClient` then retains
an empty `RuntimeIdentity`; `gpu_count: 0` in that object is an **unavailable
identity sentinel, not measured zero GPU consumption**. Do not backfill a pod
or GPU from an arbitrary replica or sum these shared-serving intervals into
exclusive-owner GPU totals.

Evidence from the bounded read-only diagnosis:

- Earlier OpenFold operation `919f933f-b69b-4f19-8bc2-515f068fc708`, completed
  14:29:44 UTC, retained the fixed hot pod/node/GPU identity. Operation
  `faaeda75-5471-4bbc-b527-d30fa893fd89`, completed 14:35:35, retained no identity.
- The OpenFold burst container started at 14:33:16. The 14:39:38 cohort snapshot
  records two Ready OpenFold replicas and two Ready Boltz replicas, with all
  four associated GPU observers Ready. That snapshot does not by itself prove
  exact candidate membership or serving endpoint for every earlier operation.
- At 14:54:56, an exact live provider probe on the unchanged `f96d890a...` CP
  confirmed enabled Kubernetes attribution, the intended `fs2-models` namespace
  and a two-second adapter timeout. Kubernetes reads succeeded. With the burst
  gone, the single OpenFold pod resolved to the same original identity; four
  Ready Boltz candidates produced no identity immediately after a successful
  31 ms pod-list request, without consulting nodes or observers.
- This provider behavior predates the remediation (`df377a560`) and already has
  a regression for ambiguous Ready replicas. The Cosmos binary-response fix
  does not change it. No new attribution-code regression or dropped persisted
  identity was demonstrated. Exact historical per-request endpoint attribution
  remains unproven, and no historical row was rewritten.

Protected diagnosis receipt:
`/home/tux/secure-handoff/cosmos-stockholm-rollout-20260917/runtime-attribution-diagnosis-20260917.json`,
SHA-256 `78e2e5b917e65fa15634c531ba59dfdfb08257930da04b1b2e9200695ac076df`.
The cohort snapshot is
`/home/tux/secure-handoff/stockholm-live-acceptance-20260917/cohorts-release142-ordered/runtime-serving-first-cohort.json`,
SHA-256 `86397c9ba9dc8c3cd0ede17bfd83702adbd970424e64b6faff5a921a82529df6`.
Closing this accounting gap requires trusted request-to-serving-endpoint
correlation; successful model output or fleet observer readiness alone cannot
supply it. No attribution code, scaling policy or live resource was changed.

## Release-143 operational verification

At **2026-09-17 14:58:18 UTC**, all three CP replicas and both controller replicas
were Ready on the Cosmos binary-response correction image
`sha256:624763c6141a990125c37b71fccc0a8d7ba5371a4efd5be325336cbc58d54020`.
The unchanged admin image `6428b350...` remained Ready 2/2. Each CP mounted the
same complete contract: 20 model qualifications, 23 bundle revisions and all
five voice qualifications. This local contract check does not substitute for
controller-owned public voice discovery or a speech inference test.

All three `/metrics` requests returned HTTP 200, with 168 customer-operation
and 161 semantic-exchange series each. Prometheus was actually scraping all
three current pods successfully, with no scrape errors; both metric families
were present for every pod. All 13 rules had `health=ok` and no evaluation
errors. Both new failure-rate rules remained inactive. The two existing
lifecycle alerts and all three existing certificate alerts were **firing**,
including certificate renewal overdue, previously pending. Successful rule
evaluation does not resolve those conditions.

GPU-observer coverage remained **14/16 Ready and Available**, with two
unavailable. `updatedNumberScheduled` was absent from the returned status; no
updated-count or fleet-wide telemetry completeness claim is made. The known
multi-replica serving-attribution gap above is unchanged by this release.

The release owner's nine public read-only surfaces all returned HTTP 200 at
14:57:48 UTC (34 Apps); this check reused that receipt without another API or
browser run. Protected operational receipt:
`/home/tux/secure-handoff/cosmos-stockholm-rollout-20260917/final-deployment-verification-r143.json`,
SHA-256 `d7569ced946e40915d320c617eaf5cd8deecf660ca06cc7ff8c03bfd54420bd0`.
Its referenced `surfaces-r143/surface-receipt.json` has SHA-256
`90828c5b07b26d76c1bb49cb7f155ad8a50f1fd7b6c9e814df3527668f302196`.

This bounded read-only pass verifies the running image, retained contract and
telemetry/API availability. Helm history completion, actual Cosmos inference,
full GPU accounting and customer qualification remain separate evidence gates.
No inference, key creation, customer-record update, alert removal or live
configuration change was performed in this verification lane.
