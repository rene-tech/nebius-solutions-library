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

Both internal cohorts completed successfully on the unchanged release:

| Metric | Cohort a | Cohort b |
| --- | ---: | ---: |
| Newly resumed simulation | 1.876 ns | 1.87472 ns |
| Accepted to durable completion | 789.850 s | 807.923 s |
| Delivered performance | 205.212 ns/day | 200.484 ns/day |
| Native useful performance | 220.039 ns/day | 215.429 ns/day |
| Clean native segments | 3/3 | 3/3 |
| Final platform files SHA/size-verified | 335 | 335 |
| Original source files retained | 305 | 305 |
| Independently verified bucket objects | 291 | 291 |

Both result documents passed semantic validation, complete zero-tolerance TPR
comparison allowed only the finite test horizon, and native restart began at the
exact new bootstrap checkpoint. All six native segments and the remaining
analysis commands completed; the same single-L40S node hosted the overlapping
cohorts while the old customer run continued. Both test Pods were released before
the customer switch. This is a new bounded functional/throughput gate, not a
new six-hour or fourteen-day soak. Prior seven-hour sustained evidence remains
separately identified in the previous handover.

The owner-authorized customer switch has been performed. Source
`a42479f9-5ee0-4ed4-869b-0a094357403f` was cancelled only after committed
generation 189, native step 52,023,920 / 104.04784 ns, and all old resources
were released. Plain resume returned new operation
`5a6cba05-2de1-43e5-8dc7-a29d5a222d76` at 06:23:05 UTC. Replay returned the
same operation. The admission response records profile `single-gpu-list200-v1`,
the fourteen-day budget and the nonempty trajectory selector.

The continuation is running on one L40S in existing pool `l40s-4x`, node
`computeinstance-e00xwjv9khjp8fhp3v`, with worker `ca863f44` and unchanged
collector `1c223369`. Its full 500,000,000-step / 1 µs target is unchanged.
The live customer observation gate **passed** at 06:39 UTC:

| Customer measurement | Observed value |
| --- | ---: |
| Native restart step, independently checked in its log | 52,023,920 |
| Latest committed step / simulated time | 53,145,800 / 106.29160 ns |
| New simulation completed during observation | 2.24376 ns |
| Acceptance to third durable checkpoint | 948.585962 s |
| Delivered performance, including startup and exports | **204.368 ns/day** |
| Native segment performance | 219.512, 217.780, 214.799 ns/day |
| Previous run's last 20 native segments, mean | 116.891 ns/day |
| Native segments completing without errors | 3/3 |
| Original scientific files retained unchanged | 778 |
| Current checkpoint workspace files | 814 |
| Worker / collector restarts | 0 / 0 |

This is roughly 1.86× the previous **native** throughput; native and delivered
rates are not conflated. The new delivered measurement spans 15.81 minutes,
not the full 1 µs simulation or a new multi-hour soak. The earlier sustained
qualification and the two new terminal internal cohorts are separately reported
above. The customer's real continuation is deliberately **left running**.

Actual native commands contain the qualified GPU/list-200 settings. The first
native log starts at the exact saved step. Every original `simulation.tpr` and
`md.part*` file has an unchanged SHA-256 and size in the new checkpoint. The
receipt is `customer/verification.json` under the private receipts directory;
raw customer inputs and outputs are not published to Git. The observation
processes have exited; no additional internal GPU workload remains.

Customer Job: `fs2-workflow-mas1-20e-a1-ec69e0520fc9`, Pod
`fs2-workflow-mas1-20e-a1-ec69e0520fc9-fjr6j`, Pod UID
`51b9d39a-8e8b-45da-b198-1fb730ff89e5`. Both containers were Ready and the
operation remained Running at the final 06:39 UTC check.

The local observation helper initially reused an immutable checkpoint download
destination between generations and correctly refused mismatching bytes.
No customer mutation occurred during those failed observations. It now retains
each downloaded checkpoint by artifact ID, separately from the latest summary;
the subsequent boundary stop succeeded.

The provider confirms the existing `fs2-lynx-c327dcc386444425` bucket's limit is
100,000,000,000 bytes, previously 5 GB. No data or S3 credential identity changed.
The new operation's confirmed budget is fourteen days (1,209,600 seconds).
The cancelled legacy source retains its original frozen seven-day budget.
The actual successor Kubernetes Job has `activeDeadlineSeconds: 1211400`
(the execution budget plus 1,800 seconds of export grace), rather than an old
six-hour or seven-day deadline. Its scientific container requests one GPU,
eight CPUs and 16 GiB RAM. No new capacity was provisioned for this continuation.

## Customer commands

Use the existing API key; no new key or S3 upload is needed:

```bash
export SCIENTIFIC_AI_URL='https://89.169.99.188'
export SCIENTIFIC_AI_API_KEY='<your existing API key>'
export JOB_ID='5a6cba05-2de1-43e5-8dc7-a29d5a222d76'

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
  -H "Idempotency-Key: resume-$JOB_ID-01" \
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

## Handover delivery

The copyable customer draft was sent **only to Rene**, not directly to Lynx,
at 06:40 UTC: [Slack DM](https://nebius.slack.com/archives/D07UH2N735X/p1791268846098699).
It explains the platform-side integration gap, controlled cancellation of the
old operation, new live ID, measured native/delivered performance, 100 GB bucket,
fourteen-day budget, unchanged access, and the status/resume commands above.
No actual API key or customer molecular data is included in the message.
