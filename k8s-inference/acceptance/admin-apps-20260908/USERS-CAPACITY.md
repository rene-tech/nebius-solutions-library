# Users and Capacity implementation

Status: implementation, real PostgreSQL checks and corrected live acceptance
complete. The root release report records the integrated image history.
No production resources, permissions, limits or policies were changed by this
lane. The owner directory is not a console-role directory.

## Users

- Canonical owner is `(tenant_id, principal_id)`, with stable UUID URLs. Existing
  owners are discovered from durable keys and operations without writes on GET.
  Their type and academic eligibility remain unclassified until explicitly set.
- Migration `0027_inference_users.sql` stores display name, human/service type,
  team, enabled state, optional academic eligibility and optional app UUID list.
- Null eligibility/app policy preserves existing key policy. Explicit settings
  intersect each key's permitted **public app route**, never its model-family
  reference, so one app permission cannot grant a second independent deployment.
- `TokenService.principal_policy` is an optional, fresh-read hook. User disable
  removes new inference/MCP invocation scopes across all keys; previously
  accepted operations and results remain readable. Re-enable is reversible.
- Existing key issue/edit/rotate/revoke machinery is reused. The UI discloses a
  new credential once, uses the selected owner/tenant, and retains older keys.
- Counts and 60-bucket charts use unique operations accepted in `[from,to)` across
  all owner keys, including scientific runs. Polling and idempotent replay do not
  create new counted rows; key issuer is not the inference owner.
- Scientific history `attribution.user_id` now uses the principal owner rather
  than the API-key creator. No historical operations are rewritten.
- GPU usage sums latest reconciled exclusive scientific-attempt lifecycle
  rollups only. Missing/incomplete coverage is unavailable. Overlapping online
  worker intervals and shared idle overhead are not charged repeatedly to users.
  Key admission-budget estimates remain separate, per-key controls.

Endpoints: `GET/POST /admin/api/v1/users`,
`GET/PATCH /admin/api/v1/users/{uuid}`,
`GET/POST /admin/api/v1/users/{uuid}/keys`; existing key mutation endpoints remain.

## Capacity

`GET /admin/api/v1/capacity/summary` powers the primary Capacity page. Existing
`/capacity` diagnostics remain under `/admin/advanced/capacity`.

- Pool/resource rows distinguish GPU classes and MIG resource names, Ready and
  cordoned nodes, requested GPU allocation, Ready/starting workers and configured
  min/max/expansion headroom. Headroom does not promise available provider supply.
- Free requests are computed per Ready, noncordoned node, not by subtracting
  allocation from an aggregate containing unready nodes. GPU reservations in
  terminating Pods remain allocated until those Pods disappear.
- Pod inventory covers every configured workload/queue namespace, including
  academic workloads. The CLI now forwards the existing extra-namespace setting
  into the capacity adapter. Partial namespace reads cannot manufacture free GPUs.
- GPU utilization is current DCGM telemetry, deduplicated by device UUID. It is
  not the allocated fraction. Loaded idle is a conservative observed reservation:
  Ready reusable whole-GPU serving Pods with no pending/running logical work for
  their route. Jobs, deleting Pods, active shared models and MIG slices are not
  included in that whole-GPU standby count.
- Logical pending runs count each operation once, including partially running
  scientific batches with a shard awaiting admission or a node. Oldest age is
  from run acceptance. Physical Kueue workloads are separately labelled; they
  include shards/activation and must not be read as customer-request counts.
- Capacity is a current snapshot. User/App usage charts obey the selected time
  range. Node CPU/RAM, taints, compatibility and queue constraints still determine
  whether a particular model actually fits a numerically free GPU.

## Local checks (2026-09-08)

- Backend Users/capacity/access/scientific attribution, mounted API and package
  checks: **75 passed**, **2 optional PostgreSQL checks skipped** in that command
  (21.81 s). Dedicated actual-SQL checks below are separate, not skipped.
- Six new backend source modules: strict mypy clean; Ruff clean.
- Users and simplified/advanced Capacity UI: **8 tests passed**; TypeScript clean.
- Combined actual PostgreSQL Users/Apps/history and transport suite: **10 cases**;
  with the existing scientific-attribution tests, **18 passed** (5.70 s).
  Covers discovery, owner persistence, time windows, independent app
  identity, create-only initialization, optimistic concurrent updates, missing
  rollups and wholly missing scientific subjects, exact Pod UID/GPU interval
  attribution, independent scientific app pause/concurrency/resume, HTTP polling
  and replay without logical duplication, payload bytes and null timeline gaps.
- The final serving-clone PostgreSQL regression exercises actual admission,
  same-ID replay, encrypted canonical runtime payload, independent public service
  routing, stale revision rejection and disabled-app rejection. It validates the
  sibling's public-app-ID fence correction rather than just catalog visibility.
- A custom non-owner runtime role performed real Users, Apps and request-telemetry
  writes and reads after applying the production 28-migration grant contract.
  The first DB attempt correctly refused an incomplete immutable manifest; no
  validation bypass was used. Two later fixture failures were fixed in the test
  setup (complete telemetry cleanup and an explicitly sized test-key limit).
- Users/Capacity files were formatted with cached Prettier without dependencies
  or lockfile changes. Source and test Ruff checks and strict mypy are clean.
- Existing unrelated old pytest temporary-directory cleanup permissions emitted
  warnings; those directories were not altered. The focused suite retains one
  existing Starlette deprecation warning.

Disposable test PostgreSQL only: `fs2-users-capacity-pg-20260908`, PostgreSQL 17,
loopback `127.0.0.1:33370`, database `fs2_test`. After root's final **73 passing**
PostgreSQL checks, the exact owned container
`c007efbd3239bba1f60d37703e4b95b701325eb487fa541f0238c5c12399f2e1`
was stopped and removed at `2026-09-08T12:27:21Z`. Only disposable test data was
removed; test code and results remain. No other container or production database
was changed. Container absence was verified after cleanup.

## First live gate and repaired regression

Source `d21439d025c806f6a3bcb4167c57b95d84ff923f` was sampled from
12:31:35.157637–12:31:38.725430 UTC. Apps, Users and the other read endpoints
returned 200, but Capacity summary returned **500**. The gateway traceback proved
that `_queue` resolved `attempt` to the operation's integer column rather than
the lateral JSON value (`operator does not exist: integer ->> unknown`). Explicit
`stage_entry(value)` / `attempt_entry(value)` aliases repair the actual query.
Transient database exceptions now yield unavailable data, never invented zeros.

The same historical interval exposed scientific bookkeeping counted as requests:
28 real scientific operations plus 56 artifact-upload operations appeared as 84.
Users logical usage, request series, pending counts and loaded-idle detection now
exclude `scientific-artifact-upload-v1`; raw operation history is preserved.
Sibling Apps history filters run before pagination and preserve the same boundary.
Qwen's 137 logical operations were already correct.

The combined actual PostgreSQL, Users/API and Capacity regression run passed
**27 tests in 8.95 s**, with Ruff, two-module mypy and diff checks clean. It covers
partially waiting real batches, the integer/JSON column collision, uploaded-input
exclusion, Apps last-used and cursor pagination, and unchanged raw history. Root's
broader actual PostgreSQL run subsequently passed **74 tests in 38.03 s**.
The recreated, exclusively owned test container
`6ca11bc506c063ceae2fd2b12bed90b693ec9d6a1916692748a53509d2cbfcdc`
on loopback port 33370 was then stopped and automatically removed; exact-name
absence was verified. Only disposable test data was removed; evidence remains.

Private baseline: `releases/admin-apps-20260908/observer/baseline-r01.json` under
the existing H100 acceptance state directory. It retains failed and successful
receipts without exposing credentials. This failed first gate is not relabelled
as a pass; the corrected exact release was captured separately below.

## Corrected live results

Root's read-only capture at 12:55:33.503084–12:55:36.916745 UTC on
`2d170292037386f339fdc96fcf115a07adcf6932` returned 200 for all inspected
Apps, Users, Capacity, usage and observability endpoints. Over the retained
08:59–10:00 UTC interval, Apps and owner usage agree on **137 Qwen requests and
28 scientific runs**, excluding 56 artifact-upload bookkeeping operations.
Raw global history remains intact; unobserved historical transport bytes are
unknown, not zero. Private receipt: `observer/baseline-r02.json`.

The adjacent Capacity snapshot reports two Ready reserved H100 nodes, 16 GPUs,
14 requested, 2 estimated free and no queued customer runs. Current DCGM
utilization was 0%, distinct from reserved capacity. The unchanged preemptible
pool has zero current nodes and a configured ceiling of two. These are timestamped
observations, not permanent capacity promises.

Real browser follow-up verifies the loaded Capacity view, a newly created user's
single successful App request, exact-scope key creation and revoke-to-401. Both
test keys are revoked, that user disabled and the owned serving clone reduced
to zero actual containers. Source App settings are unchanged. See
`BROWSER-R02.md` for exact identities, timestamps and retained harness failures.

## Post-release read-only gate

Root must provide the exact release-ready signal. This lane performs no model
calls or settings/key writes; the browser and scientific lanes own those actions.

1. Capture the new Apps list and verify every pre-release serving/scientific
   route remains present. Compare the retained r04/r05 interval
   `2026-09-08T08:59:00Z`–`2026-09-08T10:00:00Z`: 137 Qwen logical operations and
   28 scientific operations were previously confirmed. Do not equate HTTP polls
   with those operation counts or call historically unrecorded bytes zero.
2. Query Users over that same interval, map `(tenant,principal)` to durable UUID
   and verify all matching key rotations share the owner's usage. Compare the
   per-owner and per-app totals against retained operations, not key issuer names.
   Inspect existing user settings only; preserve their effective policies.
3. Capture `/capacity/summary`, ordinary capacity diagnostics, Nodes and Pods
   from all configured workload namespaces at adjacent timestamps. Compare pool
   node counts, GPU resource types, Ready/noncordoned free requests and retained
   deleting-Pod reservations. Compare utilization to the same DCGM expression,
   and distinguish logical pending operations from Kueue shard workload counts.
4. Observe root-authorized clone activity in the new Apps/Users/Capacity views;
   retain independent route/owner attribution and post-test cleanup. Report
   sampling races and missing metrics explicitly; do not modify policies to make
   a numerical comparison pass. Publish only redacted receipts and result counts.
