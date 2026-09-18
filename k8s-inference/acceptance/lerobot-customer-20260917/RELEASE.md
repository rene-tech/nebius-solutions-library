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
