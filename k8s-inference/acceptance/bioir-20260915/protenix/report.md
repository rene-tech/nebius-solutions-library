# Protenix v2: measured BioIR prototype

Recommendation: **do not promote this prototype**. It runs faster and preserves
the native result envelope, but the tested structural-quality distribution is
worse for ubiquitin. A valid CIF and a lower billable runtime are not enough.
Fresh-process snapshot results are reported separately in
[snapshot/protenix](../snapshot/protenix/); they cannot waive this quality issue.

## What was actually compared

All primary timings use the same physical H100 80GB, UUID
`GPU-ca98bcbc-cf06-a4c2-e3bc-c27a0cba95bc`, driver `580.159.04`, on the assigned
otherwise-unused Scientific AI node. The initial two-request bring-up prototype
used a different H100/driver and is excluded from speedup calculations.

The baseline is the exact deployed Protenix v2 image
`sha256:ac8f7c2c35d2bc911281f9d4a8aa9779e2cb955cdb1c2c2d37eb31d89669980e`,
source `2475421477ab414b571149ad4a875c390ff8a35d`, Protenix 2.0.0,
PyTorch 2.7.1/CUDA 12.6. Checkpoint SHA256:
`8f931f9774a396b67033d0e58628e1834f4a1448165e04254b40a780b0c0d599`.
The exact current wrapper only accepts SM90 H100; we did not remove this check
to invent an L40S baseline.

Three distinct comparisons:

1. Current conventional prediction CLI, which constructs and loads the model
   for each invocation.
2. Current model retained in the repository's existing localhost worker bridge.
   Native request configuration, featurization, seeds, validation and outputs
   remain unchanged. This isolates residency benefits.
3. Public BioIR 0.1.0 model module underneath the same native pipeline. There is
   no public Protenix `build_processor` pipeline. The custom adapter strictly
   converts the same checkpoint, preserves ten cycles/200 steps, and enables
   only the permitted token-transformer CUDA graph.

Candidate image is
`sha256:0c391bdc2ab0c5260a222ec7df5e5adc5ea9bfdbc2e158b386a97aac5dc72e94`,
PyTorch 2.12.0/CUDA 13.0 and Python 3.12. This is a hybrid evaluation environment,
not an officially qualified joint Protenix/BioIR environment. The upstream
featurizer's dependencies conflict with some BioIR and Protenix metadata pins;
the exact pip freeze is retained in `raw/prototype-environment.stdout`.
The comparison measures the whole proposed stack, not a single isolated kernel.

## Matched timings

Seconds from validated prepared-input prediction invocation through the native
confidence/CIF result and finite-coordinate check. Conventional medians have
three independent loads per case. Resident and BioIR warm medians use three
subsequent requests after the first request for each shape. Offline reference
structure scoring is outside this timer.

| Public input | Current conventional | Current resident | BioIR resident + graph | Current resident / BioIR |
|---|---:|---:|---:|---:|
| Ubiquitin, 76 residues | 86.870 | 9.221 | 5.570 | 1.66× |
| Lysozyme, 129 residues | 88.393 | 10.050 | 5.731 | 1.75× |
| Artificial lysozyme homodimer, 258 residues | 88.583 | 10.199 | 6.095 | 1.67× |

The roughly 14.5–15.6× conventional-to-candidate ratio is **not** a pure BioIR
gain. Most of it is avoiding model construction/loading on every request.
Keeping the current implementation resident is the lower-risk first option.

The initial privately reseeded eager BioIR comparison took about
9.73/9.83/10.22 s; its graph-enabled counterpart about 5.57/5.60/6.12 s. These
exploratory cohorts are retained in full, but the final candidate uses the
caller-seeded global stream after native preprocessing. The primary logs prove
actual `GRAPH_VERIFIED`/captured state across multiple shapes, not just an
enabled environment flag.

Native multi-seed/multi-sample requests were also exercised: seeds 101/202/303,
two samples per seed, three requests per implementation, six complete structures
per response. Median request latency was 25.531 s current resident versus 13.367 s
BioIR, about 1.91×; all 18 structures per variant were present and finite. The
first native batch request was 41.980 s, subsequent 25.531/25.110 s; BioIR's full
individual timings are in [analysis.json](analysis.json). This is native sample
batching, not a public queue/autoscaler load test.

## Quality is the blocking finding

All 65 successful prediction invocations across baseline, candidates and
bring-up produced finite coordinates and the expected sequences. One request
sent by the harness before readiness failed and is retained separately.
This does **not** establish scientific equivalence.

Sequence-aligned per-chain CA-lDDT against public 1UBQ/1LYZ references:

| Input | Current resident median | Primary BioIR median |
|---|---:|---:|
| Ubiquitin, one sample | 0.605 | 0.491 |
| Lysozyme | 0.260 | 0.294 |
| Artificial homodimer, per-chain fold only | 0.440 | 0.365 |
| Ubiquitin, 18 multi-seed/sample structures | 0.633 | 0.492 |

These are CA-only local-distance scores, not all-atom OpenStructure lDDT or
DockQ. The homodimer has no verified biological interface reference. Both
implementations use the current no-MSA/no-template profile; some native results
are low-confidence too. Nevertheless the measured ubiquitin regression remains
after correcting RNG-stream handling and testing multiple seeds/samples. It
must not be hidden by a timing average or dismissed as successful inference.

The next bounded accuracy investigation should feed **identical serialized
feature tensors** to native and BioIR implementations and separate dependency/
featurization, precision and module-conversion effects. A broader MSA/complex
quality set is required before customer promotion. This evaluation does not
claim the public library is universally inaccurate: this specific adapter,
dependency stack and corpus failed the quality gate.

## Startup, snapshots and cost interpretation

Latest same-node resident model initialization took 67.707 s natively and 54.763 s
for BioIR, excluding image pull, queue and request-specific first compilation.
The first primary BioIR lysozyme request took 14.950 s versus 5.731 s warm. These are
separate clocks; do not describe 54.763 s as a complete external cold-start SLO.
CPU preparation takes approximately 3.6–3.8 s on the public fixtures and is also
outside the prepared-input prediction table.

The pinned public Protenix source explicitly forbids graph capture of its trunk
and confidence pairformers because replay can yield NaNs. Only its supported
token-transformer graph was enabled. GPU process snapshots require fresh
capture for this image/checkpoint/driver/GPU binding; old native snapshots are
not reusable merely because the model name matches.

`analysis.json` reports request-window GPU-seconds per valid request, including
retained failures in that cohort. Lifecycle receipts and sampled GPU utilization
are retained under `raw/`. Request-window cost is **not** total allocated cost:
model loading, image waits, benchmark operator gaps and cooldown still occupy
capacity. No monetary price or production throughput/SLO is inferred from this
small evaluation. In particular, cost per scientifically acceptable result is
not established for the faster, quality-regressing candidate.

[Allocation bounds](allocation.json), regenerated by [allocation.py](allocation.py),
cover all six benchmark pods and all 66 prediction attempts (65 artifact/sequence-valid).
Conservative timestamp-padded totals are 2,796.00–3,323.83 GPU-seconds,
or 43.02–51.14 GPU-seconds per valid request. Of that,
1,426.70–1,954.53 GPU-seconds lie outside recorded prediction intervals:
startup, CPU preparation, operator collection/debugging and termination all
count. This is **occupied allocation, not measured hardware-idle time**.
The prototype's release time is bounded, not known exactly. Asynchronous
deletion acknowledgement is not mistaken for GPU release; UID-matched Killing
events, next same-GPU pod scheduling and final confirmed deletion bound it.

## Resource disposition and reproduction

Root-owned benchmark pods and ConfigMaps were deleted after evidence capture.
The assigned xjaw H100 was verified at 0 MiB, 0% utilization and no compute processes;
see `raw/root-protenix-final-*`. No customer-serving deployment, route, API key,
driver, node group or quota was changed. The separate snapshot worker owns its
own explicitly assigned node/resources and cleanup record.

Scripts: [run_baseline.py](run_baseline.py), [run_prepared.py](run_prepared.py),
[render_candidate.py](render_candidate.py), [bir_server.py](bir_server.py),
[analyze.py](analyze.py), [build_result.py](build_result.py). Raw commands,
source ConfigMaps, pod identities, every result and failure are under [raw](raw/).
Run `analyze.py` in the pinned analysis container (Biopython/numpy), then
`allocation.py` and `build_result.py` to regenerate the machine-readable result.
