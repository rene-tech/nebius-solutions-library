# Stockholm readiness and retention remediation — local handoff

Status: source implemented, local regressions passed; **not deployed**.
Worktree: `/home/tux/worktrees/fs2-cosmos-stockholm-remediation-20260917`.
Branch: `agent/fs2-cosmos-stockholm-remediation-r20260917`, based on
`bad3f9cba9cac2762ddbe0b62f8c6ab3a780a6d7`. The root integration owner records the
final source/image identity after all sibling changes are frozen.

## Behavior

`/readyz` bounds database ping, required local activation-controller readiness,
and federation health individually. `FS2_READINESS_DEPENDENCY_TIMEOUT_SECONDS`
defaults to 0.75 seconds (allowed range 0.01–10); the default combined I/O budget
is 2.25 seconds, below the chart's three-second readiness probe timeout. Raising
this setting requires keeping the probe timeout above the combined budget.
Timeouts or dependency exceptions return a sanitized 503 identifying the failing
dependency. Mandatory dependency failures do not become readiness success.
Existing zero-route, route-evidence, admission-worker, and scientific-worker
checks retain their behavior. Timed-out test coroutines are cancelled, and
readiness recovers once the dependency recovers.

Ordinary operation retention preserves any terminal operation still referenced
by scientific stage attempts, stage commits, run results, artifact events,
scientific batch state, or the scientific admission outbox. Artifact/upload
ownership is retained through stage attempts. A parent with a queued, activating,
or running delegated child is also retained. Scientific retention deadlines do
not authorize this ordinary janitor to remove scientific history, even if that
deadline has already elapsed; the separate scientific lifecycle owns it.

Candidate deletion uses a serializable transaction with the existing
serialization retry and `FOR UPDATE SKIP LOCKED`, keeping concurrent maintenance
and reference creation safe. Operation and token deletion remain separate
transactions to preserve token/operation lock ordering. The maintenance role
receives only the additional operation-reference column reads needed for these
checks; it receives no scientific document reads or scientific delete grants.

## Verification

From `k8s-inference/components/control-plane` with its task-local `.venv`:

```sh
.venv/bin/python -m pytest tests/test_readiness_timeouts.py tests/test_api_mcp.py -k 'readiness or readyz' -q
FS2_TEST_DATABASE_URL=postgres://postgres@127.0.0.1:33474/stockholm_retention \
  .venv/bin/python -m pytest tests/test_postgres_retention_references.py \
  tests/test_postgres_integration.py::test_terminal_usage_facts_are_exactly_once_survive_operation_retention_and_back_safe_views \
  tests/test_postgres_integration.py::test_audit_retention_is_bounded_independently \
  tests/test_postgres_integration.py::test_distinct_configured_roles_run_activation_and_retention_with_closed_privileges -q
```

- Readiness: 9 passed, 29 deselected, in 6.67 seconds.
- PostgreSQL retention and existing role/accounting regressions: 6 passed in
  6.80 seconds. The historical `fs2_scientific_stage_attempts_operation_id_fkey`
  violation is deliberately reproduced inside a rolled-back test transaction.
- Tests cover independent references, actual input-artifact preservation,
  preservation of otherwise cascading batch history, active child-parent
  correlation, three concurrent maintenance calls, three subsequent unchanged
  passes, and reference insertion holding a concurrent parent lock.
- The restricted maintenance role can read reference IDs but cannot read result
  documents or delete stage attempts. Existing usage/audit retention and custom
  role tests pass with the new query and grants.
- Ruff passed for the changed source and both new test modules. Existing
  Starlette deprecation warnings remain. Initial integrated mypy found a sibling
  `Store.cipher` wiring annotation in `api.py`; that belongs to the root's
  in-progress LeRobot wiring, not the readiness/retention patch.

The PostgreSQL tests are destructive only to their own synthetic fixture DB.
Do not point `FS2_TEST_DATABASE_URL` at a live or historical database.

## Local resources and remaining live work

Created local Docker container `fs2-stockholm-retention-pg-20260917`, image
`postgres:16-alpine`, container ID
`c89822aafa0cacae52321f5ac69858f2102a66554561ed995ca1306e08f5021a`.
It is bound only to `127.0.0.1:33474`, with local test trust authentication and no
cloud resources. Separate databases are `stockholm_retention`,
`cosmos_delegation`, `mindguard_usage`, and `semantic_outcomes`, assigned to the
parallel integration workers. The container and task temp root
`/tmp/fs2-stockholm-mcp-tests-jNcGXq` remain retained for the root's final suite.
No pre-existing local containers or historical data were cleaned up.

Cluster credentials were reported revoked; the manager has requested refreshed
access. This worker performed no cloud writes, deployment, live deletion, or
Cosmos runtime/cache/snapshot change. After exact-image deployment, the release
owner still must observe at least three successful maintenance intervals and
verify public readiness, the admin UI, MCP, a non-Cosmos Stockholm terminal
canary, and recently deployed speech/storage/workshop features. Local fixture
passes do not establish those deployment or customer-readiness claims.
