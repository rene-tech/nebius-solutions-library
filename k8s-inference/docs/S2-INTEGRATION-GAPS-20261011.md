# GROMACS / Serverless 2 joint integration gate — 11 October 2026

Scope: the first lean Scientific AI GROMACS REST/MCP deployment on the shared
`s2pr2` test fleet. No customer cluster, key, bucket, workload or route was changed.
Scientific AI work remains on `scientific-ai-serverless2-migration`.

## Implemented and checked here

The lean application's Serverless client now speaks the delivered API rather
than the earlier provisional discovery contract:

- `GET /v1/fleet`: application I/O, tenant execution profiles and identities;
- `PUT` / `DELETE /v1/bindings/{ref}`: bounded private files and explicit withdrawal;
- invoke/status/result/cancel and the actual portable-checkpoint resume request;
- safe upstream errors, stable submission keys, no implicit retry/new operation;
- no AWS role required when an authorized confidential binding is selected;
- fail closed if Serverless says an application run succeeded but provides no
  durable result reference.

76 application/transport tests pass. A real, read-only call using the existing
`sciai-worker` key confirmed the live fleet's discovery and tenant configuration.
That identity is a normal internal test user, not a customer or operator key.
No GROMACS API/MCP endpoint has been deployed or qualified by this slice.

## Blocking execution contracts

These are functional/data-durability requirements agreed for the pilot, not new
compliance requirements. Do not bypass them with a second Scientific AI scheduler
or a permissive lifecycle implementation.

| Contract | Observed | Required fix and test |
| --- | --- | --- |
| Stable request identity on resume | Two identical `POST :resume` calls with the same `Idempotency-Key` create `-r1` and `-r2`. | Serverless must persist/derive one continuation per tenant, source operation and intent; identical replay returns it, conflicting input returns 409, concurrent replay must not duplicate work. Preserve the existing Scientific AI idempotent continuation contract. |
| Stable polling handle during capacity fallback | `switch_class` deletes the original Job. Polling the original operation then returns 404. | Keep a durable original handle and successor lineage; status/result/cancel must resolve the current attempt after fallback and after Job TTL. Do not require clients to enumerate the fleet to find their run. |
| Success requires durable delivery | `status.normalise` returns `SUCCEEDED` for a Complete application-I/O Job with no attempt/finalization record. | Missing records mean unknown/finalizing/export-failed, never delivered success. Qualify missing record, failed POST and stale-epoch/cancel cases. |
| Scratch retention until delivery acknowledgement | The deployed `upload.sh` uses `EXPORT=n/a` in record mode, ignores a failed attempt-record POST, and deletes the PVC on main exit 0 whenever export is not literally `failed`. | Keep recoverable scratch unless the result pointer and fenced API acknowledgement are durable. Retry record delivery without recomputing. Missing `RESULT.json`, API timeout/5xx/409 and restart must retain recovery data. |
| Checkpoint pointers while compute is still running | The uploader records once at start and at termination. No live checkpoint publication/acknowledgement path exists; an abrupt node loss can lose the pointer to already-uploaded native state. | Provide an opaque checkpoint-reference/generation handoff through the existing uploader, durably recorded and fenced by the current epoch, with an acknowledgement readable by the application. GROMACS owns parsing/upload; Serverless owns attempt identity and pointer acceptance. Kill the actual attempt without graceful shutdown and recover on L40S using a fresh volume. |

The first three findings are executable offline reproductions against the
Serverless PR's own fake-cluster fixtures and real API functions. The latter two
are source inspection of `services/jobs/bin/upload.sh` and `services/api/attempts.py`
at the delivered API 0.11.8 / jobs 0.1.13 boundary; they still need failure-injection
tests after repair. We have not described those source checks as live drills.

## Reproduce without cloud writes

From this Scientific AI branch:

```sh
/home/tux/nebius-solutions-library-pr/k8s-inference/.venv-api/bin/python \
  k8s-inference/acceptance/serverless2-gromacs-20261011/probe_upstream_contracts.py \
  --source /home/tux/nebius-solutions-library-pr/k8s-inference
```

This uses the Serverless project's installed test dependencies and fake cluster,
not Kubernetes credentials. Exit 1 is the current expected failing integration
gate; do not convert it to a passing or skipped test. The receipt identifies the
Serverless source commit and reports the actual outcomes.

Last complete probe: S2 source `069cfe712d3c44e90c74c6fa3c63fa4b891e7f61`;
all three checks failed. Its checkout is shared and has active development, so
re-run against the next integrated candidate rather than editing or deploying an
older copy of it. No Serverless source or shared-fleet configuration was changed
by this Scientific AI task. The owner has been asked which team should own these
fixes.

## Continue after the upstream fixes

1. Rerun the probes and finish the native-worker lifecycle/file bridge against
   the delivered acknowledgement contract; keep the model container free of
   Kubernetes credentials.
2. Build/pin the optimized GROMACS 2026.4 RTX and L40S runtime images, validate
   native checkpoint interchange, and register the internal pilot App.
3. Deploy the lean REST/MCP compatibility surface on the shared test fleet only.
   Preserve the current scientific request/result/artifact envelopes and typed
   `submit_gromacs_workflow`, `get_scientific_status`, checkpoint, result, cancel
   and resume contracts. Reuse the native scientific worker, not the old scheduler.
4. Complete fresh synthetic scientific workflows through both interfaces, two
   unchanged-release cohorts, concurrent internal users, idempotent replay,
   storage denial and export-only recovery, abrupt RTX loss -> L40S recovery,
   permission/isolation negatives, and actual artifact/trajectory validation.
5. Record exact digests, operation/attempt identities, steps, timings and ns/day.
   AWS cross-account federation needs a real approved role/trust setup; the
   test fleet's placeholder ARN is not a working grant. Use an approved internal
   scoped storage profile for the first test, and leave AWS and PHI unqualified
   until their own checks pass.

Do not merge this migration to `main`, route customers to it, or call GROMACS
customer-ready merely because configuration discovery or component tests pass.
