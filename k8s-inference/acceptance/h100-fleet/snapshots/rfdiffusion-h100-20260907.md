# RFdiffusion optional CUDA snapshot qualification

Three matched normal/fresh-restored H100 pairs passed two original requests each. The donor was deleted before a separate cross-node restore, which also passed both requests. The [machine-readable report](rfdiffusion-h100-20260907.json) binds the immutable [production bundle](rfdiffusion-bundle.json), original image, source hashes, checkpoint, GPU/driver identity and full-output evidence.

| Clock, median (range), seconds | Normal, n=3 | Restore, n=3 |
|---|---:|---:|
| Container → reusable CUDA model ready | 8.507 (7.167–10.121) | 3.070 (3.030–3.433) |
| Container → first validated full output | 65.044 (64.244–67.220) | 50.360 (49.732–52.209) |
| Pod creation request → model ready | 13.142 (11.748–423.420) | 5.841 (5.795–5.848) |

These are fresh processes with existing image/shared-filesystem caches, not disk-cold, new-node or reserved-RAM measurements. The long third normal Pod waited behind another GPU capture. Its container clock excludes that queue delay. CRIU itself took0.662–0.688s, CUDA restore0.242–0.257s and unlock0.015–0.016s.

## Correctness and boundaries

The exact native image, precision, checkpoint and50diffusion steps are unchanged. The original76-residue/seed8100 and96-residue/seed8200 requests each produced byte-identical PDB coordinates across all six trials. They use the same raw constraint-file bytes but different meaningful length/seed parameters; this is not a claim of distinct raw constraint artifacts. Original GPU, PDB, trajectory and TRB validators ran. The cached model has no captured target, Sampler, contigs, request seed or output directory; those are rebuilt independently for each request.

The ready marker means the immutable model is on CUDA and the worker can accept a request. It does not include the request-specific Sampler/diffusion-table initialization. The earlier34.87s native initialization marker included that broader work and is not directly comparable. First validated output includes the remaining work and is the more useful end-to-end bound. Deterministic IGSO3 table construction is a separate future optimization candidate in the pinned [upstream diffusion implementation](https://github.com/RosettaCommons/RFdiffusion/blob/9273ef67335acaf91df0150473a274759229cdf6/rfdiffusion/diffusion.py); it is not GPU snapshot state.

## Integration

The bundle uses read-only shared claim `fs2-fleet-snapshots-rwx-r20260907`, prefix `rfdiffusion-current-r20260907-r3`, with per-Pod mutable scratch. Captured source ConfigMap `fs2-fleet-snapshot-rfdiffusion-v2` remains immutable. Separate ConfigMap `fs2-rfdiffusion-request-v1` contains the thin request entrypoint and the metadata-only outer CLI. Terraform assembles both from checksum-bound canonical source files; no model-specific manual code edit is needed when registering this bundle.

The ordinary loader remains the default. Compatibility is qualified only for this image, checkpoint, H100/driver580.159.04/kernel6.11.0-1016-nvidia combination. Actual failed restore/preparation falls back to the original loader. Result metadata records actual supervisor outcome, not the selected policy. The paired campaign used the frozen native CLI proxy; production-transform and public admission/collector acceptance are separate checks and must not be inferred from these pairs.

The separate [production-transform acceptance](rfdiffusion-production-transform-20260907.json) now passes both actual restoration and intentional unavailable-scratch fallback using the original76-residue command. Both outputs match the accepted coordinate hash. The original result metadata correctly reports `gpu_snapshot_used=true` only after completed CUDA restoration, and false on ordinary fallback; the production adapter validates both. Both disposable Pods and input ConfigMaps are confirmed absent. Public API/admission/collector testing remains the parent-owned release gate.

The subsequent [actual public production acceptance](production-options-expanded-h100-20260907.json) passes both original requests after selecting the installed option, including final collection, artifact download, physical structures and actual CUDA restore in Pod/Loki logs. The original normal model policy was restored. Public first-on-fresh-worker96 output exactly matches the retained original native96 output (`fb51ce83…`); the first comparison mistakenly used the paired96-after76 reused-worker reference (`42831fab…`). That initial mismatch and both references are preserved. The paired normal and restored workers agree for their shared request order, but these results do **not** establish request-order-independent byte determinism for a long-lived worker. Production runs one original invocation per fresh Pod; the matching first-request comparisons pass.

## Retained limitations and cleanup

Failed r1 capability restoration, r2 Hydra serialization and the r3 observer timing race are retained as failures, not silently promoted. The corrected source composes the same task-only Hydra settings as the native CLI; a per-request working-directory regression prevents accidental reuse of the first request's stage completion marker. All six measured GPU Pods and the cross-node proof Pod were deleted. The immutable bundle/source ConfigMaps remain for Terraform-managed production use. No quotas, pool limits, model resource limits, host drivers or global caches changed.
