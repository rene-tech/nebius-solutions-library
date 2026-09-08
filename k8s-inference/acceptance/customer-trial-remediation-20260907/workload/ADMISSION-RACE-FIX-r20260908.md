# Scientific admission race fix — implementation evidence

Status at 2026-09-08 07:19 UTC: **ready for root integration**, not yet live-qualified. The failed r02 Proteina submission and all original evidence remain unchanged. Root owns commit, deployment, old RF recovery and the next coordinated customer cohorts.

## Change

`components/control-plane/src/fs2_serve/scientific_batch/service.py` now handles concurrent materialization after durable acceptance. If the API creator receives `BatchRepositoryConflictError` while an outbox recovery worker has already created the batch, it reopens the tenant-scoped row and verifies all frozen admission fields against the original durable outbox: operation/batch/workload/tenant/model identities, variant, input artifact, plan, scheduling, execution binding, artifact access, input manifest, runtime localization and state schema. Only an exact match is reused. Current lifecycle status, attempts, revision and result publication may legitimately have advanced.

Missing or different admissions still raise the original conflict. This is not a blanket HTTP 409 handler: authentication, request-HMAC/idempotency checks and input validation remain before the recovery branch. The outbox is completed only after successful creation or verified matching reuse. If the other worker advanced the operation's status, submission refreshes that public status while preserving whether this was the original request or an idempotent replay.

No repository schema, database migration, model/profile/fixture, resource request, GPU policy, quota or snapshot recipe changed.

## Deterministic regression

`tests/test_scientific_admission_race.py` gates the original HTTP request at repository creation with two asyncio events. While that original request remains in flight, a second task invokes the real `recover_pending_admissions()` path using the durable outbox—not a client resubmission. The recovery worker materializes the batch and advances it before releasing the original creator.

The PostgreSQL case uses the real store, migrations, batch repository and ASGI route against an isolated PostgreSQL 16 database. A controlled cancellation in this **local synthetic database only** makes recovery finish before the original creator; the old repository path then rejects a new admission to the now-terminal operation. This proves a real post-commit materialization race and its HTTP consequence. It does **not** claim the historical r02 conflict had this precise terminal interleaving: that request's error body was not retained, and its associated server operation was still running at the time of its 409.

Before changing service.py, the controlled original-submit tests failed with the exact public response:

```json
{"error":{"type":"conflict","message":"scientific batch admission conflicts with its durable Operation"}}
```

Baseline: **2 failed / 1 passed**. Both the controlled in-memory case and actual PostgreSQL/ASGI case returned 409 rather than 202; the genuine changed-runtime rejection passed.

After the fix: **6/6 new tests passed**. The original submit returns 202 and its original operation ID, `reused=false`, accurate terminal status and location; an exact replay returns that same operation with `reused=true`. The database contains exactly one operation, one batch and one accepted event. Four controls retain rejection for changed runtime, changed scheduling, changed input manifest and missing durable row. Changed request bytes under the same idempotency key remain HTTP 409; missing authentication remains HTTP 401.

## Verification and provenance

Starting repository: main `984cf72f36b3c676fe2c6faa1fc5c5722d4f983d`. Existing sibling changes were preserved; this lane edited only service.py, its new test file and this evidence/card.

| Check | Outcome |
| --- | --- |
| New admission regressions, including PostgreSQL/ASGI | 6 passed in 5.90 s |
| Existing scientific production, PostgreSQL state, request-error and startup tests | 98 passed in 16.31 s |
| Mypy on service.py with silent imported-module checking | Passed |
| Scoped Ruff | Passed |
| `scripts/refresh_scientific_recipes.py --check` | Current; no recipe rewrite |
| `git diff --check` | Passed |

Source file SHA256:

- service.py: `2b43ea4ed5e0777b245df20a3c92577a9504c9b27329ef053cb416ee4376f7a5`
- test_scientific_admission_race.py: `9d044aa9c29196349b969c9355710193596bf4812d95f39e7b02ee108b390a43`

Private local test receipts are retained in `/tmp/fs2-admission-race-pg.f994py/`: baseline.xml SHA256 `c6e3135c6dd5a551522f5b8c251e0a357678e3b75b170bd0e2f19ae79e386734`; fixed-v2.xml `3f6b0e1956f0c08dec0d8ba26936a97e5cb225b1559e7ba41c57fd4b2542b1a5`; broader.xml `9c5faacd4619cd1f8d80df11276f935645273069798652951a7ede45082da12d`. No production credentials are in this report.

## Cleanup and remaining gate

The isolated `postgres:16-alpine` container `fs2-admission-race-20260908-f994py` (`5b5e806f7192aca97b15337cbfef1e0c20e3e7eb71c6625996342799da75e1d5`) exposed only a loopback port and was labeled with this task. It was stopped and automatically removed at 07:19 UTC; a filtered container listing confirmed absence. Its disposable synthetic database was removed; test evidence and the isolated Python environment remain locally. No shared database, production API, live model, policy, node or resource limit was changed or contacted by this lane.

Root must deploy the combined fixes and qualify them with fresh unchanged 14-operation cohorts. No r03 requests have been submitted. Historical r02 remains a failed cohort even after any later recovery of its unfinished RF operation.
