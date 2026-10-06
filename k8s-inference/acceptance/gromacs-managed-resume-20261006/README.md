# Managed GROMACS resume — 6 October 2026

Scope: finish the automatic plain-resume tuning and empty-segment analysis gap,
qualify the deployed release with internal credentials, then perform the owner's
explicitly authorized continuation of Lynx's actual job using its existing key.
No new bucket, credentials, scientific protocol, GPU pool or global GPU default.

## Release

- Source: `c1101e34214b50237a30f759fa4680bd187c1f5f`.
- Control-plane image/index: `sha256:4f9f12882ea9ffaac7b3034b8f1a45c476d910dece291a7c23062a3988ececcc`.
- Linux/amd64 manifest: `sha256:9fc85b1c6a1d4ceebb0da36863fa5078dd9cd18dec712f7380d28ea7a8758d67`.
- Worker unchanged: `sha256:ca863f44c7d17c8096267ec43939cda8b9546f0d3b11440e62bcf9149bc94a1e`.
- Previous control-plane digest/rollback: `sha256:1c22336993e588408c069b5a8f93e550ea60829f167099f55acdc1f3918fd0d9`.
- Target: `mk8scluster-e00j5z9te7x5dd9g6a`, `project-e00rene`, eu-north1.
- Private receipts: `/home/tux/secure-handoff/fs2-gromacs-managed-resume-20261006/`.

The image-only rollout updates the three API replicas, schema init, two model
controllers and maintenance image. Scheduling, execution map, worker image,
existing Pods, website, admin UI and workshop specifications are preserved.
The authoritative private H100 tfvars image pin is updated; no broad apply.

## Behavior

The server restores the latest owner-authorized native checkpoint itself. Its
versioned profile registry matches immutable TPR, worker digest and CPU shape.
For the qualified input it adds the measured GPU/list-200 settings; any explicit
customer performance argument preserves the whole original tuning. Unknown
inputs/runtimes, MPI and PLUMED are not silently tuned. `performance_mode:
"preserve"` opts out. This changes no TPR, force field, integration timestep,
temperature/pressure target, simulation horizon or output cadence.

Omitted `nonempty` on remaining trjcat/eneconv input patterns becomes true;
explicit true/false and literal filenames are preserved. All original empty and
nonempty files remain retained. The response and `_fs2-continuation.json` record
every adjustment. REST and MCP share this implementation.

## Acceptance and current state

108 focused API/MCP/checkpoint/resume tests and Ruff pass. Initial tests caught
an outdated installed local GROMACS package; rerunning against the exact current
source resolves that test-environment mismatch. Actual packaged image loading
proves the registry JSON and fourteen-day contract are shipped.

All three final API readers passed identity/readiness/public discovery with nine
MPI shapes. The first barrier check was deliberately rejected while an old Pod
was still terminating; the subsequent check passed after rollout completed.
Live MCP discovery exposes auto/preserve; MCP replay returns the same internal
operation as REST, not a duplicate simulation.

Two internal system/qa cohorts use the retained authorized six-hour source copy,
unaltered scientific inputs except the separately validated 2-ns test horizon,
and deliberately omitted tuning/empty-file flags. Each 120-second bootstrap
ends with the expected `WORKFLOW_TIME_LIMIT_EXCEEDED` fixture-setup condition.
Both plain resumes receive the new defaults and replay idempotently.

| Cohort | Bootstrap | Resumed operation |
| --- | --- | --- |
| a | `2cc68d38-56c1-4627-9b4e-23cd2b7be72b` | `db281d2a-945e-4977-a7b1-8603bf433d12` |
| b | `ee615c8c-d102-46ce-8e16-0cd018cfd85c` | `76dcd047-e7ea-49d5-9e8e-80e768328796` |

Terminal output verification and the real customer continuation are **pending**
at this documentation checkpoint. Do not treat accepted/running as complete.

The provider confirms the existing `fs2-lynx-c327dcc386444425` bucket's limit is
100,000,000,000 bytes, previously 5 GB. No data or S3 credential identity changed.
The new operation/resume budget is fourteen days. The running legacy operation
still has its frozen seven-day budget until explicitly continued.

## Customer commands

Use the existing API key; no new key or S3 upload is needed:

```bash
export SCIENTIFIC_AI_URL='https://89.169.99.188'
export SCIENTIFIC_AI_API_KEY='<your existing API key>'
export JOB_ID='<operation ID>'

# Current status (also works while the job is queued or running).
curl --fail-with-body -sS \
  -H "Authorization: Bearer $SCIENTIFIC_AI_API_KEY" \
  "$SCIENTIFIC_AI_URL/v1/operations/$JOB_ID" |
  jq '{id: .operation.id, status: .batch.status, error: .batch.failure_code}'

# Resume a FAILED or CANCELLED job from its latest committed checkpoint.
# Reuse this idempotency key for retries of this same resume action.
curl --fail-with-body -sS -X POST \
  -H "Authorization: Bearer $SCIENTIFIC_AI_API_KEY" \
  -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: my-job-continuation-01' \
  -d '{}' "$SCIENTIFIC_AI_URL/v1/operations/$JOB_ID:resume" |
  jq '{new_job_id: .operation.id, status: .batch.status, continuation: .continuation}'
```

Then replace `JOB_ID` with `new_job_id` and use the same status command. Do not
resume a running job, submit a duplicate workflow or use a new idempotency key
just because a queued run takes time. Retrieve its committed progress via
`GET /v1/operations/{id}/checkpoints`, and final output with
`GET /v1/operations/{id}/result` after completion. Native checkpoint continuation
is not GPU-process snapshotting, and the configured fourteen-day budget is not
a claim of a fourteen-day soak.
