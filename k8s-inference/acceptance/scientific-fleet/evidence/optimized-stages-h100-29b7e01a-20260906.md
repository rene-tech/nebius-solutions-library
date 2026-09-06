# H100 optimized-stage acceptance — 2026-09-06

All six real workflows passed, along with invalid BoltzGen input rejection:
**7/7 checks passed**, all 18 expected GPU shards and six CPU finalizers
succeeded, and every returned manifest plus a scientific output passed download
size/digest verification. Idempotent submission replay also passed. The
separate real admin-browser cancellation released both remaining GPU pods
within 2.5 seconds. No campaign pods, Jobs or labeled Kueue Workloads remain.

This validates intermediate runtime revision `29b7e01a`, not a later release,
final fleet requalification or GPU snapshot restore. Full measurements, image
and input identities, raw receipt hashes and container timestamps are in the
[machine-readable evidence](optimized-stages-h100-29b7e01a-20260906.json).

## Workload and results

Each request used fresh GPU pods and persisted runtime artifacts. Mosaic ran
two independent 44-residue binder shards with 30 optimization steps;
RFdiffusion ran four independent 96-residue designs with 50 diffusion steps.
Seeds vary by repetition. At most two requests ran concurrently, with six
overlapping GPU admissions. Both reserved nodes were verified as H100 80GB,
SM90, driver `580.159.04`; the RF runtime reported PyTorch `2.3.0`, CUDA `12.1`
and Triton `2.3.0`. GPU/resource limits were unchanged.

| Model | First run, CPU pool initially zero | Warm-node repeat 2 | Warm-node repeat 3 |
|---|---:|---:|---:|
| Mosaic, two shards | 412.959s | 120.004s | 124.448s |
| RFdiffusion, four designs | 417.833s | 108.160s | 108.162s |

These are external-client wall times, including upload, polling and verified
downloads—not raw GPU kernel timings. The first and warm-node samples are
different cache states and must not be combined into a claimed cold-start
percentile. Three samples do not establish p95/p99 reliability or biological
quality; the existing model-specific semantic checks passed.

The lifecycle ledger reconciled for every run without missing data:

| Run | Scheduler-occupied GPU-seconds | Application-active GPU-seconds | Occupied-idle GPU-seconds |
|---|---:|---:|---:|
| Mosaic 1 / 2 / 3 | 285 / 187 / 194 | 159 / 163 / 165 | 126 / 24 / 29 |
| RFdiffusion 1 / 2 / 3 | 348 / 279 / 286 | 301 / 237 / 243 | 47 / 42 / 43 |

“Application-active” is a closed application-observed lifecycle interval, not a
measurement of continuously busy GPU kernels. Concurrent-shard phase totals
can exceed request wall time. Dedicated weight-loading, compile/warmup and
snapshot-restore timings remain unavailable in this campaign; no restore
occurred. The slow CPU finalizers did **not** retain their upstream GPU pods.

## Finalizers: image fix works; startup transitions still matter

Every Mosaic `aggregate` and RFdiffusion `collect` pod used the lightweight
control-plane image digest
`sha256:443ecb4344ff74957a49729d42717cc034d06bc4351200b5b4888bdad8d9ddb4`
for its executable and collector, with zero GPU requests. The cold node pulled
59,326,950 bytes in **3.656 seconds**; there was no multi-GB GPU-runtime pull
for these CPU stages.

The first finalizer triggered the existing CPU pool from zero to one node at
22:02:09Z and was scheduled at 22:03:51Z: **102 seconds of capacity acquisition**.
Its actual artifact init processes ran for only 1–3 seconds, but transitions
between them included 62-second and 85-second gaps. For example:

- RF `materialize-1`: 22:04:14–22:04:16Z; only 7,859 bytes.
- RF `materialize-2`: 22:05:18–22:05:20Z.
- RF `materialize-3`: 22:06:45–22:06:46Z.

Later repetitions captured all frozen artifact sizes: RF's four inputs total
about 31.5 KB, and Mosaic's two total about 8.8 KB. Those delays are therefore
not weight transfer or NVMe bandwidth. On the warm node, each materializer ran
for 1–2 seconds and transitions were 0–1 seconds. Finalizer main containers
reported 0–1 seconds at one-second timestamp resolution; this is not a
sub-second microbenchmark.

The bounded follow-up is to consolidate ordered materializations into one
init process and keep the configured CPU floor available. That change was
**not deployed or measured by this report**. Parent-owned follow-up release
must prove it with live multi-input workflows, preserving the same checks and
resource envelopes.

## Invalid input and real browser cancellation

BoltzGen's two 20-candidate shards exceed the existing 24-candidate total bound.
The API returned **422** before creating an operation or any BoltzGen pod.

For cancellation, the parent used the actual admin page to cancel a separate
four-design, 192-residue RF request. At the cancellation request, two shards
had finished and two GPU-assigned stage pods remained. Denoising was observed
earlier in the run; device utilization at the exact click was not sampled.

- Browser click: 22:02:36.413Z; backend accepted request: 22:02:36.536337Z.
- HTTP 200 response: 22:02:36.626Z.
- Both remaining scientific-stage and collector process groups exited 143 at
  22:02:37Z; both pods were deleted by the watch timestamp 22:02:38Z.
- With one-second watch precision: under 1.5 seconds to process stop and under
  2.5 seconds to pod removal. The 90-second grace setting was unchanged.
- Public terminal state was `cancelled`; all four attempts reported resources
  released, and no downstream collection attempt was created.

The ordinary success-expecting runner retained `operation_terminal_failure`
for this deliberately externally cancelled run. Its terminal evidence was
checked independently as the expected cancellation; it was not rewritten into
a successful scientific result.

## Reproduce

From the exact deployed solution checkout, with existing token environment
variables set privately:

```bash
python3 acceptance/scientific-fleet/run_scenario_acceptance.py \
  --endpoint https://inference.example \
  --repository-root /read-only/deployed/k8s-inference \
  --scenarios acceptance/scientific-fleet/scenarios/optimized-stages.json \
  --receipt-root /secure/fs2-acceptance \
  --run-id optimized-stages-new-run --max-parallel 2 --admin-metrics
```

The endpoint is the origin, without the Terraform output's trailing `/v1`.
Retain API receipts and a pod/event watch to separate capacity wait, image
pulls, init execution and transition gaps. An initial local invocation with
`/v1` was refused before any HTTP call; the corrected run used a new receipt
directory. Read-only environment probes succeeded on both GPU nodes during
repetition three; earlier probe attempts raced ordinary pod cleanup.

The existing acceptance runner unit suite also passed: 38 tests. Final
qualification remains a separate full-fleet campaign after implementation and
its offline promotion metadata are stable.
