# Stockholm usage accounting: projection and reconciliation

Status: implemented and locally tested; **not deployed or customer-qualified**.
This is a truthful accounting projection, not a pricing or billing system.
The reported historical discrepancy (22 admission GPU-hours versus approximately
0.1604 lifecycle occupied GPU-hours) has not been freshly read from Stockholm.
Do not treat that earlier task description as a new measured report.

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

Before readiness: renew authorized cloud access, read exact Stockholm historical
cohorts, retain the non-destructive report, deploy coordinated CP/admin images
from the integrated baseline, and run the two unchanged-release customer-equivalent
canary cohorts required by `CUSTOMER_RELEASE_POLICY.md`. Capture tenant/key scope,
endpoint, image/config identities, operation/attempt/lifecycle evidence and
recent speech/storage/model regression results. No such live pass is claimed here.
