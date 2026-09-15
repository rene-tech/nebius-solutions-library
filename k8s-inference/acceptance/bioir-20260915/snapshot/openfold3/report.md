# OpenFold3 Preview2 BIR fresh-pod snapshot qualification

Status: fixed matrix complete, negative application qualification. HOLD
graph serving and snapshots: all three ordinary-load controls also fail
on repeated graph keys after successful warmup. This is not a failure
isolated to process restoration. Task-owned GPU/CPU pods, ConfigMaps,
temporary PVC and its backing PV have been removed after fingerprinting.

## Frozen candidate and controls

The model lane's exact H100 FP32, native-global-RNG, diffusion-graph,
GPU-resident candidate is cloned from its frozen manifest. Public BIR
0.1.0, current Preview2 checkpoint SHA256
`af09eac4f29cef856633af07558cb143226fe95ebbef2c20921769d4a5f4bee4`, native
Preview2 0.4.2 preprocessing/scoring/CIF/HTTP boundary, three recycles,
200 diffusion steps and one sample are retained. Graph capture alone is
not a process snapshot.

Assigned physical H100 `computeinstance-e00j20a9hkb508cn4a` is rechecked
for scheduler allocations and GPU processes after model-lane cleanup.
Namespace `fs2-bioir-snapshot`, uniquely named `fs2-bioir-of3-*` objects,
one normal scheduler GPU request/limit; no production services or labels.

The existing snapshot helpers are reused read-only. Donor supervisor PID1
owns a child worker; capture targets only that worker and its process tree.
No host PID namespace, host network, GPU reset, driver or host security
policy change is used. Container-only helper capabilities and unconfined
profiles are identical in the normal-start controls. Each fresh restore
gets writable empty scratch and a read-only donor bundle. Image, source,
checkpoint, runtime, kernel, GPU UUID and driver bindings are retained.

## Matrix and clocks

Warm donor on 46-aa 1CRN, capture, collect and delete it before any fresh
restore. Three restore pods each receive real 46-aa, new129-aa and full-MSA
95-aa requests. Three normal-start controls use the same image, init work,
supervisor, capabilities and physical GPU, warm the same first shape and
receive the same measured requests. All requests preserve native seed42;
the HTTP boundary does not offer seed overrides. An incompatible-model
identity test explicitly requests normal-load fallback and real outputs.

Container-start to observed readiness, pod creation/init, first and later
HTTP-through-validated-artifact times, donor-delete to fresh-ready and
whole allocated GPU time are distinct. Readiness polling adds roughly3s
plus Kubernetes API latency. Captured `load_seconds` describes donor
loading, never restore time. Repeated images are cached; no caches are
flushed. Dependency/asset init remains included in pod-level cold time.
The existing harness keeps donor/normal scratch on its PVC; restored
workers copy cache/tmp into fresh emptyDir scratch while mounting checkpoint
pages read-only. Those storage-path costs belong to this measured recipe,
not a universal CUDA restore claim.

## Evidence and limits

`control.py`, `operate.py`, `request.py`, `matrix.py` and `analyze.py`
reproduce the experiment. Frozen app/helper hashes and rendered manifests,
raw request/response archives, CUDA/CRIU logs, actual graph states,
structural comparisons, failures, fallback and cleanup receipts are local.
The temporary bundle is fingerprinted before exact-name deletion; raw
scientific evidence remains. A snapshot must be recaptured after cleanup.

CA-only lDDT/RMSD and exact sequences assess these small repeated shapes;
they do not establish broad biochemical accuracy. Same-seed timing repeats
are not independent scientific replicates. Only same-node/same-driver
compatibility is tested, with no device remapping, multi-GPU restore,
autoscaler integration, cancellation or production promotion claim.
Model-level current-versus-BIR performance is in the separate model lane.
No new native Preview2 snapshot cohort was run. Older native snapshot
success is not reused as a matched contemporary comparator; this result
does not isolate the responsible BIR, graph, framework or driver component.

## Observed results

Low-level CUDA+CRIU restores reaching readiness: 3; application-serving passes: 0; matched normal controls: 3. Artifact-valid requests across all schedules: 7. Readiness is not accepted as successful inference.

| Pod | Cohort | Container → ready (s) | Container → first valid artifact (s, includes orchestration gap) | Valid requests | CUDA+CRIU restore |
| --- | --- | ---: | ---: | ---: | --- |
| fs2-bioir-of3-donor | donor | 72.120 | 80.478 | 1 | False |
| fs2-bioir-of3-fallback | fallback | 18.098 | 24.130 | 3 | False |
| fs2-bioir-of3-normal-1 | normal | 65.349 | 73.477 | 1 | False |
| fs2-bioir-of3-normal-2 | normal | 66.442 | 74.526 | 1 | False |
| fs2-bioir-of3-normal-3 | normal | 63.797 | 72.236 | 1 | False |
| fs2-bioir-of3-restore-1 | restore | 292.578 | unmeasured | 0 | True |
| fs2-bioir-of3-restore-2 | restore | 295.951 | unmeasured | 0 | True |
| fs2-bioir-of3-restore-3 | restore | 292.779 | unmeasured | 0 | True |

Median container-to-ready: restore 292.779s, normal 65.349s; normal/restore ratio 0.223x. Individual trials, ranges, init states, pull events and restore subphase receipts are in result.json.

Startup benefit is not established: no restored request produced a valid artifact, and even readiness was slower. A valid-result startup speedup is undefined.

No restored valid structures are available for paired output equivalence. CUDA illegal-memory-access errors on the first restored inference are distinguished from subsequent poisoned-context errors; three failed shapes in one process are not three independent CUDA fault replicates. Low-level restore success does not qualify this application.

Critical normal-control result: normal warmup succeeds, then the same query key fails with CUDA illegal memory access and subsequent requests see a poisoned context. This reproduces without any process restoration. The core graph benchmark varied query IDs and therefore recaptured graphs; its speed gains do not qualify cached-graph reuse. The originating kernel and exact lifetime bug remain unresolved because CUDA errors are asynchronous. No graph invalidation/reset or new runtime variant was tested.

Incompatible-identity normal-load fallback observed: True; valid requests 3/3. These are three distinct query keys without prior warmup, not validation of cached-key reuse. Negative contract-probe results are retained separately in the fallback raw evidence.

All inference schedules: 25 attempts, 7 valid artifacts, 18 HTTP500 failures. The three restored first-request faults and three ordinary-load repeated-key faults are six independent process-level events; their twelve follow-on failures are poisoned-context consequences. The seven deliberately invalid feature probes are separate from inference counts.

Capture fs2-bioir-of3-donor: status passed, complete capture wall 297.776s, durable flush 290.634s. CUDA/CRIU command times remain separate in the receipt.

Whole-trial GPU allocation: 2353.040s, including initialization, capture, all failures, collection and operator/orchestration gaps. This is not steady-state per-request production cost.

Decision: HOLD graph serving and snapshots for this exact integration. Ordinary model loading also fails when the warmed query key is reused, so failures cannot be attributed to restoration alone. Distinct-key speed gains are performance potential, not deployment qualification. Graph cache lifetime/invalidation remedies are unimplemented and unmeasured.

Cross-node/device portability is untested. No result is transferred to another GPU UUID, driver, checkpoint, precision or model variant.

Recorded bundle/PVC file inventory: 9173344679 bytes across 2356 files. Individual sizes and SHA256 fingerprints are retained before cleanup.

Resource disposition: Reproducible temporary CUDA/CRIU bundles removed after fingerprinting. Raw outputs, manifests, identities, logs and hashes retained; snapshot pages require recapture.

Final read-only GPU audit: NVIDIA H100 80GB HBM3, 0 MiB used, 0% utilization. Scheduler allocations and compute processes are empty.
