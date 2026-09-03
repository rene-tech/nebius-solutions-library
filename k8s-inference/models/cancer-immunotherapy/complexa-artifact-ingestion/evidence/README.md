# Live ingestion evidence

Recorded on 2026-09-03 against tenant `tenant-e00f3wdfzwfjgbcyfv`, project
`project-e00rene`, region `eu-north1`, cluster `k8s-inference-h100`.

| File | What it records |
| --- | --- |
| `staging-receipt-r20260903a.json` | Each file's download onto the ingress claim: source revision, bytes, SHA-256, attempts, resume offset, duration. |
| `promotion-receipt-r20260903b.json` | Publication onto the reference plane: generation digest, tree counts, marker digest, sub-path, and the successor functions used. |
| `reclaim-receipt-r20260903c.json` | Release of the ingress copy, gated on the published generation being present and intact. |
| `loader-verification-20260903.json` | `torch.load` of all seven checkpoints inside the published Proteina-Complexa runtime image. |
| `h100-consumer-visibility-20260903.json` | Full re-hash of all seven files from an 8xH100 inference node, read-only, through the same host root. |
| `binding-handoff.json` | What a consumer pins: exact mount sub-paths, marker digests, per-file identities, source revisions and licences. |
| `code-identity.json` | The exact code that produced these receipts: localization core file digests, the ConfigMap digest, the task tool digests, and what still needs re-binding. |
| `manifests/` | The exact Job and ConfigMap manifests as applied. |
| `raw/*.report.json` | The probes' own machine-readable reports, unedited. |
| `raw/*.stdout.txt` | The probes' raw stdout, unedited. |
| `raw/*.status.json` | Pod status: node, phase, exit code, and the **kubelet-resolved image digest** actually run. |

The promotion receipt is from run `r20260903b`, which re-measured the trees run
`r20260903a` had published and found them intact. That is why its generations are
marked `already_published`: the digests were confirmed a second time by an
independent pass rather than merely re-asserted.

Node identity appears only as `node_digest`, a truncated SHA-256 of the node
name. These receipts are committed to a public repository and the downward API
hands a pod the opaque Nebius instance ID, so the raw value stays out; the digest
still answers whether two receipts came from the same machine.

## Scope of these receipts

They are deployment-time evidence about bytes: identity, readability, and where
the bytes live. They are not serving-time evidence. No endpoint was created and
no request was issued, so there is no request receipt here and none is implied.

Both AlphaFold 3 and PyRosetta remain tenant-private on the academic claim and
are outside this task. No receipt here covers either of them; the PyRosetta
binding receipt is part of the pending re-binding recorded in
`code-identity.json`.

The promotion and reclaim receipts were produced by a localization core that was
uncommitted when it ran. `code-identity.json` pins its exact digests and records
that the identity-critical functions are byte-identical to the successor now
under review, which is a source comparison and not a live result.
