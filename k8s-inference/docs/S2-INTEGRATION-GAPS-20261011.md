# GROMACS / Serverless 2 joint integration gate — 11 October 2026

Scope: the first lean Scientific AI GROMACS REST/MCP deployment on the shared
`s2pr2` test fleet. No customer cluster, key, bucket, workload or route was changed.
Scientific AI work remains on `scientific-ai-serverless2-migration`.

## Evening follow-up: storage identity and continuation delivery

The new signer verifies Kubernetes' signed projected pod JWT against fixed issuer,
audience, namespace, service account and current S2 attempt. Missing, forged and
stale identities have dedicated tests. The managed OIDC issuer rejects urllib's
default user agent; the application identifies itself explicitly without relaxing
TLS or signature checks. API image `pilot-20261011-r10` includes that correction
and a retryable response for verified workers while startup status converges;
GPU workers remain the digest-pinned r6 builds.

Live fallback exposed missing private input on a successor Job. S2 commit
`094db8f16` / API **0.11.20** moves binding delivery into the common Job creation
path before admission, covering fallback, resume and export retry. Cleanup now
removes only a finished operation's copies, not those of active successors. The
same regression tests fixed fresh-volume creation for portable non-fleet resume.
These changes are in the existing **unmerged** Serverless PR and deployed through
Terraform on the shared control cluster. Its CI and `make check` pass (240 tests,
13 optional tests skipped; four Terraform tests). No customer fleet was changed.

See the [follow-up evidence](../acceptance/serverless2-gromacs-20261011/FOLLOW-UP.md)
for exact live qualification, failures retained and remaining gaps. Automatic
cross-class recovery of an already-placed job is still not implemented; timed
unplaced fallback and explicit checkpoint resume are different mechanisms.

## Retest after Serverless integration (11 October, afternoon)

All three executable probes now **pass**, against Serverless commit
`56aaa9eb3c9b6fb1176584c73e463884473b2c17`. The shared control API at that retest was 0.11.11;
the jobs 0.1.14 uploader adds the live checkpoint file bridge and retains scratch
until acknowledged delivery. The original findings below are preserved as
incident history, not current blockers.

The application now implements the file bridge, idempotent resume, REST/MCP
compatibility routes and online request-scoped S3 object signing. Subsequent
[live application acceptance](../acceptance/serverless2-gromacs-20261011/LIVE-PILOT.md)
completed two REST/MCP cohorts and abrupt RTX-pod/scratch loss followed by L40S
resume. It preserves the exact failures, image digests, timing and remaining
gates. No customer cutover is authorized.

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

94 application/transport tests pass. A real, read-only call using the existing
`sciai-worker` key confirmed the live fleet's discovery and tenant configuration.
That identity is a normal internal test user, not a customer or operator key.
The GROMACS pilot API/MCP is now deployed at the shared API's `/scientific-ai`
path. Native single-GPU work, explicit cross-GPU resume, completed-result replay
and bounded authorization checks passed; this is not complete compatibility or
protected-data qualification.

## Remaining joint gates after real GROMACS execution

- **S2 automatic recovery:** class fallback handles unplaced work; a failed
  already-placed run does not automatically move to another class/region when
  its original capacity disappears. The live drill used explicit `:resume`.
- **Scientific AI storage authorization:** projected pod verification is now
  implemented. Qualify external provider IAM and token refresh/revocation; a
  signed pod JWT is not hardware attestation or proof of immediate Pod deletion.
  Retain the online S2 current-attempt check and 60-second signed-URL lifetime.
- **Joint live durability/security acceptance:** inject denied final-object
  storage and lost final acknowledgement. Completed-result replay and offline
  denied-transfer tests passed, but are not equivalent. External AWS trust is
  still a placeholder; provider IAM and protected scratch/log/process isolation
  remain unqualified.

These are retained integration work, not hidden passing checks. The bounded
Serverless fixes above were made after the original report; no external
tickets/messages were created from the shared fleet.

## Original blocking execution contracts (fixed upstream; retained evidence)

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
not Kubernetes credentials. Exit 0 is now expected; do not convert a regression
to a passing or skipped test. The receipt identifies the
Serverless source commit and reports the actual outcomes.

Last complete probe: S2 source `fdcbf7785dd0f46679f877eaef9c7b78d2f59ac8`;
all three checks passed. The original failing source was
`069cfe712d3c44e90c74c6fa3c63fa4b891e7f61`. Its checkout is shared and has active
development, so re-run against the current candidate rather than editing or
deploying an older copy. The evening follow-up above records the later S2 fixes.

Other lanes upgraded the shared API to 0.11.12 and then 0.11.13 during the live
application tests. The new three-probe pass and the exact final API image digest
are recorded in the live report; do not imply that the entire fleet was frozen.

## Original continuation plan (live status is recorded above)

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
