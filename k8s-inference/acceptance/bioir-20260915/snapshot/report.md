# BIR snapshot qualification — Boltz2 and OpenFold2

These measurements separate successful state restoration from faster startup. Three fresh-pod CUDA+CRIU restores are compared with three ordinary process loads on the same physical GPU, with valid same-shape and changed-shape requests. Other candidate snapshots are in [Protenix](protenix) and [OpenFold3](openfold3).

| Model / variant | Restore / normal trials | Median container → observed ready, restore / normal | Normal÷restore | Valid requests, all schedules |
|---|---:|---:|---:|---:|
| boltz2 / asyncio, H100 | 3 / 3 | 93.60 / 40.04 s | 0.428× | 21 |
| openfold2 / mixed precision, L40S | 3 / 3 | 98.23 / 30.89 s | 0.314× | 18 |

## What was actually tested

- Boltz default uvloop: CUDA checkpoint succeeded but CRIU rejected an io_uring mapping. CUDA recovery/unlock followed by a valid changed-shape request succeeded. The asyncio event-loop variant was then captured and restored; it is a distinct qualified configuration, not an implicit pass for the default runtime.
- An initial fresh restore failed because our initializer pre-created its scratch cache directory. That harness error is retained; corrected fresh-pod restore trials are counted separately.
- Donors were deleted before restore. Each restore received a new pod, empty scratch and read-only bundle. Device-plugin assignments and exact GPU/driver identity were preserved. No host PID namespace, driver reset or customer process was used.
- Boltz retained actual CUDA graph verification and valid graph recapture for95/199residue inputs. One restored worker also returned both requested samples at native batch2. No persistent graph cache for every shape is inferred from a single tracker.
- OpenFold2 used the exact mixed-precision BIR candidate and current upstream input/output adapter. This public BIR version has no enabled OF2 CUDA graphs. The restored standard-library HTTP server produced valid46/129residue structures.
- Three paired normal workers have the same image, application, snapshot supervisor, resource limits and node as their restored comparators. Their first request warms the same shape before measured repeats/new shape. Cold initial image downloads are not mixed into warm-image lifecycle medians.
- The fallback test intentionally mismatches model revision, then requests normal-load fallback. Its events, errors, resulting health and validated requests are retained. A missing bundle is not equivalent: the outer filesystem setup can fail before compatibility fallback.

## Quality, failure and resource accounting

Full responses, structures, input payloads, validator results, confidence values, graph states, capture/restore logs and failed attempts are retained. Structured results include exact structure-text comparisons between paired normal and restored outputs, full per-trial times, dispersion and allocated GPU-seconds. Allocation includes initialization, pull, idle/operator collection, capture failures and cleanup; it is experimental resource accounting, not a production price forecast. No p99, broad biochemical efficacy or untested API feature claim is made.

Readiness measurements include roughly3-second polling plus API latency. The health field `startup_seconds` survives capture and describes original donor loading; it must never be used as restore latency. Capture includes explicit durable fsync, recorded separately from CUDA/CRIU execution. Peak PyTorch allocator counters may reset internally during graph preparation.

Known candidate limitations still apply: Boltz chain-ID normalization can rename A,C to A,B, and neither candidate advertises a cancellation/idempotency API. Snapshot success does not repair these differences. Failed uvloop capture, failed scratch-cache restore and any HTTP validation behavior remain in the evidence.

## Reproduce and boundaries

`control.py` / `openfold_control.py` create frozen task-owned candidates; `operate.py` records readiness, requests, capture, collect, restore and exact-name cleanup. `matrix.py` executes3restore,3normal and incompatible-identity fallback cycles. `analyze.py` rebuilds this report and [result.json](result.json). Snapshot tools and application/source digests are frozen in [manifests](manifests) and capture runtime identities.

The eight coverage models have no equivalent implemented BIR full-model path. Their existing snapshot support is not requalified here and cannot be advertised as a BIR benefit. Protenix/OpenFold3 have separate candidate-specific evidence; no result is transferred between models or driver revisions. No production routing or model was changed.

## Measured decision

For these exact storage/driver/runtime configurations, use ordinary model loading: snapshot restoration is compatible but slower. Boltz asyncio restore is 2.34 times the matched normal startup duration; OpenFold2 restore is 3.18 times normal. All paired normal/restored structure texts are byte-identical in the measured cases. This is not a claim of universal numerical determinism or broad quality equivalence. Both incompatible-identity tests recovered through explicit normal-load fallback; Boltz rejected malformed/missing payloads with HTTP 422, OpenFold2 with HTTP 400, and both returned 404 for the wrong route and remained healthy.

The snapshot experiment holds graph settings constant. No separate graph-off capture/restore cohort was run; eager-versus-graph inference comparisons are recorded by the model lanes. Cancellation, autoscaler orchestration, cross-node/device migration, multi-GPU snapshots and untested product variants remain unqualified.

## Cleanup residual

All owned Boltz/OpenFold2 GPU and CPU collector pods, four snapshot ConfigMaps and three temporary PVC objects were deleted. Both snapshot PVs were reclaimed. The explicitly approved public Boltz cache PV `pvc-cf75a701-6ede-4d91-935e-af6116917554` remained Released with Delete policy after its `fs2-bioir-boltz2/evaluation-cache` claim was removed: `mounted-fs-path.csi.nebius.ai` reported `VolumeFailedDelete: rpc error: code = DeadlineExceeded desc = context deadline exceeded`. Its exact state/events are in [storage-reclamation-exception.json](lifecycle/storage-reclamation-exception.json). This is a separate storage-controller follow-up; no finalizer bypass, controller change or force-delete was attempted. Namespaces are retained for peer snapshot work. Raw benchmark evidence, public source/digests, bundle inventories and reproducible manifests are retained.

### Final closure supersedes the historical residual

The manager's [23:31 UTC cluster receipt](../report/final-cluster-state.json) confirms that this cache PV is now absent and all evaluation pods, PVCs and ConfigMaps are removed. Reclamation completed through the normal controller path; no storage follow-up remains open for this PV. The historical timeout and its original pending status remain above to preserve the incident chronology.
