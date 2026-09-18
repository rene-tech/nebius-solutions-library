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

## Final result

Pending corrected deployment and public dataset acceptance. Exact action-array
preservation must not be described as proof of physically action-aligned video.

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
