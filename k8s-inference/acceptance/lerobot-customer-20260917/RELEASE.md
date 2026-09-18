# LeRobot public workflow release — 18 September 2026

Status: live acceptance in progress. This document does not establish customer
readiness until the final result and exact release identity below are recorded.

## Deployment and first integration run

- Cluster: `mk8scluster-e00j5z9te7x5dd9g6a`, project `project-e00rene`.
- Public gateway: `https://89.169.99.188`, MCP path `/mcp`.
- App: `cosmos3-lerobot-augmentation`, with delegated `cosmos3-nano` generation.
- Helm revision 147: source `182be10e0b4ee7afe596142e9fadb00101ef1989`;
  control-plane index
  `sha256:58a6ccc0c7a9882e6155c1aefda1798d8f8777aa86faebf7d583bf007e339390`;
  dataset worker
  `sha256:71047b62303a21d2feec6127a4531b2ad4333f157bdb52cf67d2756dd7ead8c1`.
- All three control-plane replicas, both controllers, and both unchanged admin
  replicas became ready. Nine operator/public surface checks returned HTTP 200;
  the Apps API listed the new App alongside the previous 34 Apps.
- A deployment verifier was initially invoked before the rolling update had
  settled and rejected the transient replica count. It passed after Kubernetes
  reported the rollout complete; no inference was admitted before that pass.
- The real browser displayed the new App and its logical run, including its
  actual failed state and the associated public MCP/HTTP exchanges. This is an
  admin UI observation, not hosted LibreChat acceptance.

First ordinary-key run: `842b4692-6587-4dfc-8c1b-0d7f77047962`, accepted
`2026-09-18T00:19:39.492Z`, failed at `00:20:45.747Z` (approximately 66.3 s).
The key matched the intended robotics grants with concurrency one, not operator
privileges. Upload and in-flight idempotent replay passed. The CPU worker ran on
the general CPU node; its upload child succeeded. Generation child
`1de73efe-3784-4338-84f8-3f748813f3ce` was admitted but had not started inference
when the worker failed. Parent failure cancelled that child and released the
stage resources. No output dataset was produced and this run is **not a pass**.

Integration diagnosis: the worker treated every `operation` field as a nested
operation object. Generation admission returns a bare operation view, whose
`operation` field is the string `generate-media` and whose `id` is the UUID.
The upload response legitimately uses a nested object. The corrected client
must handle both actual contracts without admitting a duplicate generation.

Private payloads, keys, raw logs, and browser sessions are not in Git. Retained
operator evidence is under
`/home/tux/secure-handoff/lerobot-release-20260917/`; the customer run journal is
`debug-lighting-mcp/run.json`. The failed cohort remains preserved there.

## Release149 acceptance — multi-variant failure, not a final pass

Customer cohorts ran on frozen Helm revision **149**, deployed
from source `8fe42b85fc6e6d7f3cfca7128d65888bca7e4951`:

- Control-plane index:
  `sha256:da50219255ae4af4975bba74c0221890cce1eaad4b28ac114d89ceb087e624e4`.
- Dataset worker:
  `sha256:df364675b50cd267cb80dab38ad29fa3ec2f5d704e82e30e29ec104cea511042`.
- Native Cosmos model, serving image, r7 GPU snapshot and scaling configuration
  are unchanged. GPU snapshotting applies to the delegated Cosmos runtime,
  not the CPU-only dataset coordinator.
- All three control-plane, two controller and two admin replicas were ready;
  nine public/operator read checks returned HTTP 200 before admission.
- The first final lighting parent `6068f623-0263-4e34-9e18-68989b78411d`
  passed the complete client workflow. Its delegated generation measured
  42.92455 s cold activation on reused preemptible H100 capacity. This is not
  a newly provisioned node benchmark.

Four single-variant parents passed full download, reader comparison and both
replays: lighting and all-episode/all-camera environment generation, each once
through HTTP and once through named MCP. The fifth parent,
`dc708f87-d0a2-4021-b645-7540cf0f4837`, failed its **second blur variant** with
public `PLATFORM_UPSTREAM_ERROR`. Its first generation child
`6b963566-dbf0-4423-9e35-67dc628a19a4` succeeded. Live worker logs show
`fs2 artifact content upload returned HTTP 409` when the second variant tried to
upload the same already-finalized reference. No second generation was admitted.
The CPU resources were released and the runner stopped new admissions; the
negative/concurrency/cancellation probes were not run. This release is **not a
complete acceptance pass**. Its private `final-cohorts-r149` directory name does
not change that failed outcome.

Read-only cleanup subsequently confirmed desired Cosmos replicas zero, no native
Cosmos Pods, and no Jobs/Pods for any of the five exact dataset attempts. The
canary remains intentionally valid for the corrected acceptance run. The second
environment parent's four child rows still lacked Pod/node/GPU identities on a
delayed recheck; these are unavailable observations, not measured zero usage.
Its operation IDs, timings, successful outputs and parent attribution remain
recorded. This known GPU-observer coverage gap was not hidden or filled with
guessed identities.

The worker fix must reuse a verified finalized reference without overwriting
it or creating duplicate GPU work. Pending: the corrected multi-variant run and
negative probes, a fresh unchanged-release full acceptance set, real-customer
grants and disposable-key cleanup.

Exact action-array preservation must not be described as proof of physically
action-aligned video. The `strength` value is prompt guidance, not a calibrated
native denoising control. Visual review of both environment runs found that the
requested laboratory/pale-blue-panel appearance was not visibly achieved and
robot motion diverged. The exact prompts survive the exercised worker/adapter
mapping; this does not prove native conditioning behaviour. Lighting produced a
warm appearance but changed object/robot details. These are model-output
limitations, separate from passing dataset integrity.

### Admin and accounting observations

The final149 browser displayed the dataset App, successful parent, actual
HTTP/MCP exchanges, CPU attempt, phase timings and downloadable artifacts. The
first final parent displayed 0.12 s queueing and 83.01 s operation duration;
the client additionally spends time preparing uploads and validating downloads.
CPU-parent startup is unavailable, not a fabricated zero. GPU cold-start and
GPU usage belong to the delegated `cosmos3-nano` child records. Join by parent
and tenant/principal when analysing the complete workflow; do not interpret the
CPU coordinator's zero GPU use as free generation.

Both Apps' Runs, Usage, Settings, Containers, Metrics and Logs APIs returned
HTTP 200 during the final cohorts. App-wide usage includes the explicitly
retained earlier failed147 and successful148 integration runs; it is not an
isolated final-cohort counter.

The generic Admin "Last qualification" badge remains unpopulated because its
separate customer-readiness verdict file is not mounted. The scientific recipe
qualification and these acceptance receipts are distinct evidence sources.
Wiring that generic badge would require another rollout; it is not silently
claimed to have been updated. One browser App-detail HTTP503 occurred on147 at
00:27:31 before the corrected releases and is retained in the private browser
log. No recurrence has been observed in final149 checks so far.

## Corrected integration run (not the final release cohorts)

Helm148 deployed source `0834206d06375c6ea4320dbdaf23da80e1e1509c`, control-plane
index `sha256:96e197fd0e6d8c48b91c409bb2042e35f6a09d4b94e8c891534b587b30e9617b`
and worker `sha256:df364675b50cd267cb80dab38ad29fa3ec2f5d704e82e30e29ec104cea511042`.
The parser handles both real response shapes. Worker failures now carry bounded
static codes/details through the public operation API. No quota, native model,
snapshot, customer-key, or scaling setting changed.

The repeated typed MCP lighting run `e50a8f50-c388-4eb7-a53d-bd943d82070c`
passed: source upload, one delegated generation, output publication/download,
full LeRobot 0.6.1 reload, and in-flight/terminal idempotent replay. The client
ran from `00:37:14.595Z` to `00:39:50.589Z` (156.0 s including local preparation
and output validation). Generation child `200f9cd2-34a5-4402-9cc8-ed6ffcc9f340`
measured **38.000323 s GPU cold activation**, on a cached preemptible H100 using
the unchanged r7 snapshot. This is not a new-node/image-pull benchmark.

Validation covered two 32-frame episodes and two cameras (128 decoded video
frames), 2,304 exact non-video values, and preserved episode/task/FPS structure.
All 32 selected frames changed. Untouched streams were re-encoded; their
maximum per-frame mean absolute pixel error was 2.108/255 (limit 6/255), not
byte identity. Exact arrays do not establish physical action alignment.

The Admin APIs show both parent and child terminal Runs, usage, logs and metrics.
Loki retained 95 App log entries at the observation time, including the first
run's explicit `PLATFORM_RESPONSE_INVALID: operation response is not an object`.
Thus the initially source-reproduced diagnosis now also has direct runtime-log
confirmation, despite the original Kubernetes Pod already being removed.
Raw logs remain private in `observe148-debug/`.

The H100 Terraform variables now pin the deployed control-plane digest and add
the LeRobot scheduling entry. Formatting and Terraform validation pass. No
broad Terraform apply was performed against the older unrelated infrastructure
state.

## Corrected upload replay release150

Worker source `4dc1ef4850c1d794a5f4cd2a941f12d11b3f7718` fixes the second-variant
failure through the actual durable upload contract. After begin/replay it reads
the upload operation: queued uploads receive content, succeeded uploads skip
PUT, and idempotent finalization returns the immutable reference. The returned
digest, size and media type must still match. Failed/cancelled/expired or invalid
states fail explicitly; arbitrary HTTP409 is not swallowed and invocation keys
are not changed to create duplicate work. Fresh-client replay is covered.

The worker suite passed **78 tests**, Ruff and strict mypy. An initial test
invocation selected an incompatible older8fps fixture; it was corrected to the
retained10fps fixture, without relaxing timestamp validation. Offline nonroot,
read-only, network-disabled image smoke passed. A pre-publication packaging
smoke caught archive directory permissions inherited from private umask077;
the generated build context was corrected to readable/traversable source
permissions and rebuilt. No failing image was deployed. Publication details:
`models/general-media/lerobot-augmentation/activation/registry-publication-upload-replay-20260918.json`.

Helm150 deployed source `7aa37722bd4989c983aa8c5aaedab5310da90049`, control-plane
index `sha256:e240fb81a208df7517df8ee7ae3f0fa47d4544b7399f68f02db933d16fed9b6c`
and worker `sha256:1eeb26e239243c3c088b1ce9cb184f6cde9639100a6583d0d39b45d787cd85a1`.
The new worker is initially active with null qualification receipts; historical
148 proof is retained, not borrowed by a new worker. The explicit replacement
renderer permits only the LeRobot image/identity change and preserves ten other
scientific rows, four snapshot bundles and exact scheduler bytes. Its affected
suite passed23 tests.

An early identity check while Helm was still running rejected the still-old
deployment; no inference followed that rejection. After Helm reported deployed,
all three gateways, two controllers, two unchanged admin replicas and15 GPU
observers were ready on their expected images. The settled check at01:26:33
passed all three Prometheus targets/13 rules and the separate nine public/admin
reads returned HTTP200. The targeted two-variant and negative probes are now
running before a new final qualification/acceptance promotion.
