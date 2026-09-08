# Public aging Apps r04: three predictions pass; one MCP admission gap

Runtime/Terraform source `55c8744f2d0e08cd2ed49e444f216ec9b572d730`, CP image
`sha256:5328ce31e7fc2781832163dde2a8d8a274113d0ef7f528e6b22eb04cb246a8df`.
UI source `d00976744bd7cefb93896cac3cac8585ed8d6b0b`, image
`sha256:d474e818258ca0e78a3ec6765d3a035f7ded616a5f94d89ad4252aa23aa4988a`.
Campaign discovery started on 2026-09-08 at 14:46:34.477602 UTC.

Both canonical Apps were observed Cold with zero containers twice, and each
normal-tenant scoped key discovered exactly its model over HTTP and MCP.
Existing min0/max1, pools and 300-second idle/cooldown settings were retained.

| Request | Operation | Accepted → terminal | Raw API startup field | Full client verification |
| --- | --- | ---: | ---: | ---: |
| PhenoAge cold HTTP | `126ad621-03d4-488c-8f47-8fdbd3e1bbd0` | 8.370505 s | 7.990402 s | 14.133420 s |
| AltumAge cold HTTP | `272db2c3-65a3-45a8-b6b7-cc14580ea2b5` | 260.630723 s | 260.120673 s | 269.604695 s |
| AltumAge warm MCP | `49f26031-3d2a-42ee-b5cf-054ac524380e` | 0.883802 s | 0.481633 s | 6.252356 s |

All three returned the expected original synthetic predictions. Their exact
replays reused their existing operation IDs, and HTTP and MCP result retrieval
agreed. The client clock includes polling, replay, download and semantic checks;
it is not a model inference-only benchmark. The warm request reused the exact
same AltumAge Pod and GPU. Its API `cold_start_seconds` field measures admission
to readiness confirmation, **not** another model load.

The second PhenoAge request was rejected before an operation was returned:
MCP `invoke_model` reported `route_unavailable` at 14:46:57.186418 UTC,
request ID `4b9d888d-5770-45cb-9826-f5b20d46addd`. The root traced this to a
normal same-revision Desired/Progressing scaler handoff withdrawing the service
route even though the first request had already succeeded. This remains a real
product failure; there was no retry or silent replacement. The planned fourth
prediction did not execute, so the full cohort is not qualified.

## Actual cold-start components

AltumAge caused the existing preemptible node group to scale from 0 to 1; no
manual node/limit change was made. Its runtime witness records:

- Request accepted 14:46:43.440710; Pod created/admitted 14:46:47.
- Autoscaler trigger 14:47:18; node created 14:48:38 and Ready 14:48:57.
- Pod scheduled and image pull started 14:49:09.
- Image pull took 103.936 seconds, finishing 14:50:53. Reported image size was
  4,167,595,352 bytes, not a measurement of bytes transferred over the network.
- Runtime container start 14:50:56; Pod Ready 14:51:03; API readiness confirmation
  14:51:03.561383 and completed inference 14:51:04.071433.

The witness binds Pod `cc8e04b0-de1e-4703-92bd-1f90c36a7ba2`, node UID
`8016b2ab-991f-4f47-84f2-78fb280b1644`, H100 80GB, driver 580.159.04 and the
exact `ca6352f7…` model image. This is a **new-node plus image-cold** observation,
not an existing-node/cache-hit claim. PhenoAge used the existing CPU node and
Pod `93a1036a-c230-4eb4-9773-68df115e052e`; its runtime witness is separate.

Original receipts are in `releases/aging-20260908/public-apps-r04`; the observer
owns `public-apps-r04-runtime-witness/{phenoage,altumage}.json`. Prior r01/r02/r03
failures remain separate. No GPU snapshot qualification is inferred.

## Cleanup and final outcome

AltumAge returned naturally to observed Cold with zero containers in two
consecutive samples, confirmed at 14:56:43.470463 UTC. Its Runs and Usage showed
exactly two logical operations, not extra replay runs; final min0/max1 and all
other saved settings matched. Both task keys were revoked and returned 401 on
the explicit denial check. No successful operation was cancelled by cleanup.

The campaign finished at 14:56:44.932184 UTC and exited 1, correctly reflecting
the PhenoAge MCP admission failure. No owned client process remains. AltumAge's
individual lane passed its complete 0→1→0 and HTTP/MCP checks, but combined
qualification waits for a fresh cohort after the publication correction.
