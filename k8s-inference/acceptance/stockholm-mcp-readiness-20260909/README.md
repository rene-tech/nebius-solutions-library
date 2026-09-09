# Stockholm MCP readiness remediation — 2026-09-09

This receipt closes the eight server-side findings in the Stockholm participant
readiness report. The retained H100 cluster was updated through the Terraform
wrapper. No node-group bounds, model replica settings, or participant access
policies were changed by this release.

## Outcome

| Finding | Resolution | Live evidence |
| --- | --- | --- |
| Short MSA requests failed upstream | The PDB70 adapter now accepts the query plus the homologs actually returned instead of requiring 128 matches. | The original 20-residue request completed as operation `d4a1c6e0-e173-4e46-9bcd-1f87eb138bd6`, including an A3M alignment. |
| MCP failures lost their cause | Tool failures now return `isError=true` with a stable type/code, retryability, durable-admission state, request correlation, idempotency context, and an operation ID when one exists. | Both `admission_limit_reached` and `operation_has_no_result` were observed as structured tool results; the temporary admission error included a two-second retry hint. |
| Proven routes were marked unqualified | An active reconciled route is reported as active. Missing HTTP/MCP or elasticity evidence is `null` with a reason rather than the false assertion `false`. | All affected serving Apps reported `route_active=true`; unknown qualification dimensions carried explanations. |
| Serving artifact upload and real CXR input failed | Serving Apps use the same bounded object-storage upload service as scientific Apps, including rollback if admission fails. Image URL and finalized-artifact paths are both supported. | A 144,679-byte JPEG was uploaded and verified. Artifact-backed operation `76151644-a79d-4b30-a14d-2b801e8b21f1` and HTTPS-backed operation `0495d710-fd91-4206-bf8a-ffd238e799c2` both completed successfully. |
| Long-lived MCP discovery became stale | The server advertises `tools.listChanged=true`, emits `notifications/tools/list_changed`, and exposes a per-principal catalog revision for reconnect/poll fallback. | A fresh public session reported `tools.listChanged=true`; serving and scientific discovery returned the same tool-catalog revision. |
| A paused opaque Protenix clone was public | Paused clones remain available to operators as history but are excluded from participant discovery and invocation. | The paused opaque App is absent from scientific discovery; the named `protenix-v2` App remains present. |
| AltumAge and NV-Segment-CT lacked examples | AltumAge publishes a compact artifact-reference recipe; NV-Segment-CT publishes the qualified small NIfTI fixture flow. | Both examples validate against their published typed schemas. |
| Raw artifact bytes were agent-visible | Raw base64 upload/download tools are client-only. Agents receive compact manifest inspection tools with names, media types, sizes, and digests. | Public discovery returned 50 tools, no raw byte tools, and a prior Mosaic manifest was inspected without placing bytes in the model context. |

## Release checks

- Control plane: 3/3 ready on image digest
  `sha256:87c609807d02238cf0b1d94cb285b96de71af97a98237d78895f6b12d202f63a`
  for the runtime-equivalent remediation build.
- Model controller: 2/2 ready.
- Static PDB70 MSA runtime: ready on the patched CPU image; it retains its
  general-CPU placement and does not introduce a GPU fallback.
- Public `/readyz` and `/admin/`: HTTP 200 with successful default TLS
  validation.
- Authorized MCP discovery: 50 tools, 16 serving Apps, 10 scientific-batch
  Apps, no raw byte tools, matching catalog revisions.
- Focused MCP/configuration suite: 9 passed.
- Full control-plane suite: 1,955 passed and 98 skipped.

## Event capacity follow-up

Capacity Advisor reported fresh one-GPU H100 preemptible availability in each
eu-north1 fabric checked, with 4–16 nodes available per fabric at observation
time. The event configuration therefore keeps the two reserved 8×H100 nodes,
raises the existing one-GPU H100 preemptible pool from `0..2` to `4..16` nodes,
and lets the existing Kueue flavor expose that added quota. L40S capacity was
not mixed into H100-qualified CUDA checkpoint/snapshot bundles: it remains a
future runtime-qualification target instead of being advertised as usable
capacity prematurely.

Scientific Apps already admit multiple durable Jobs and are bounded by Kueue.
Serving Apps receive explicit live autoscaling ceilings through the admin API,
which causes the model controller to render KEDA burst targets without changing
their existing hot floors.

The cluster was deliberately left running for event preparation.
