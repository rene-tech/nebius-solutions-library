# Scientific AI: BioNeMo Inference Runtime evaluation

Date: 2026-09-15. Status: **evaluation complete — not production qualification**.
No candidate has been promoted to customer serving.

## Decision

BioNeMo Inference Runtime (BioIR/BIR, previously discussed as TFT) is useful for
part of this catalog, not an acceleration switch for every BioNeMo model.
Boltz2 is the strongest measured candidate. OpenFold2 mixed precision is
promising but changes numerical behavior. Protenix runs faster but currently
fails our structural-quality comparison. OpenFold3's FP32 graph-enabled path
has a meaningful measured gain with close paired H100 outputs, but later
repeated-key tests exposed a serving failure, so that prototype is also on hold. Most of
the catalog has no equivalent full-model BioIR path.

These are measurements against **our exact deployed implementations**, including
portable adapters, not a claim about performance against every official NVIDIA
NIM. Published NVIDIA benchmark figures are not substituted for measurements.
The [official support matrix](https://docs.nvidia.com/bionemo/inference-runtime/references/support-matrix/)
distinguishes model modules, complete pipelines, input coverage and eligible
CUDA graphs; those are separate integration questions.

The largest headline gains can be misleading: both Boltz2 and Protenix currently
have reload-per-request paths. Keeping the existing model resident produces
substantial savings independently of BioIR. The tables therefore include a
resident baseline, and Boltz2 also includes an optional-kernel/toolchain control.

## Complete catalog coverage

The eleven BioNeMo-branded models in the frozen live catalog are all included.
Protenix v2 is an explicit additional module candidate. Related products such
as OpenBind, BoltzGen, BindCraft, mosaic, ESMFold and AlphaFold3 have separate
[applicability notes](../coverage/applicability.md); they are not silently
rebranded or substituted by OpenFold/Boltz.

| Hosted model | Actual evaluation | Current recommendation |
|---|---|---|
| Boltz2 | Matched H100 and L40S, four implementations, full MSAs, multiple shapes/samples, output checks | Prioritize a controlled integration after chain-ID and broader quality/feature gates |
| OpenFold2 | Current versus FP32/mixed BioIR, environment control, H100/L40S cases | Mixed precision is conditional; FP32 can be slower |
| OpenFold3 Preview2 | Matched FP32 graph/lifecycle comparison, mixed/RNG controls and repeated-key tests | Hold graph prototype: repeated cached-key inference fails; mixed precision also regresses quality |
| Protenix v2 | Same-H100 conventional/resident/BioIR, graph/eager, multiple shapes and native seed/sample batch | Hold: measured quality regression despite lower latency |
| DiffDock | Fresh current baseline, docking outputs, repeated/mixed requests | No direct BioIR mapping; retain current runtime |
| Evo2-40B | Exact two-H100 current model, three context/output lengths and mixed requests | No BioIR Hyena/sequence-generation backend |
| GenMol | Fresh current baseline and generation/contract checks | No equivalent BioIR molecular-token model |
| MolMIM | Fresh current baseline, generation/cardinality checks | Fix observed fallback/cardinality behavior first; no direct BioIR fit |
| MSA Search PDB70 | Real CPU search misses and cached responses separately | No BioIR database-search replacement; preserve MSA caching |
| ProteinMPNN | Fresh current baseline, sequence outputs, batch and mixed requests | No equivalent BioIR message-passing/sequence-sampling model |
| RFdiffusion | Actual 50-step current runs, three lengths and native batch | No drop-in; narrowly scoped outer-product operation reuse could be investigated |
| Proteina-Complexa | Full generate/filter/evaluate/analyze workflows, two targets and native batch | No drop-in; individual pair/triangle operators are only a future custom-integration lead |

Unsupported speedups are **N/A**, not zero, 1× or a reused speedup from a
different model. Detailed source-level reasoning is in the coverage report.

## Measured request speed

All ratios below compare the same physical GPU and checkpoint with matched
shape/sampling settings. They are small-corpus medians, not p99 or throughput
SLOs. H100 and L40S are not mixed in a ratio.
Exact images, dependency stacks and CPU/memory resource budgets are disclosed
in the manifests; some prototype budgets differ from current serving. These
are complete implementation comparisons on the same GPU, not isolated-kernel
causal measurements or equal-priced infrastructure forecasts.

### Boltz2: real incremental gain beyond residency

In-pod HTTP response latency, three measured requests per shape. Full MSAs,
three recycles and 200 diffusion steps remain unchanged.

| GPU / residues | Current | Resident upstream | Resident + optional kernels | BioIR | Stronger kernel control / BioIR |
|---|---:|---:|---:|---:|---:|
| H100 / 95 | 29.891 s | 6.534 s | 6.367 s | 2.123 s | 3.00× |
| H100 / 199 | 30.189 s | 6.920 s | 6.824 s | 2.271 s | 3.00× |
| H100 / 464 | 36.148 s | 12.625 s | 8.815 s | 3.138 s | 2.81× |
| L40S / 95 | 36.412 s | 8.253 s | 8.318 s | 2.481 s | 3.35× |
| L40S / 199 | 36.723 s | 8.711 s | 9.046 s | 2.595 s | 3.49× |
| L40S / 464 | 50.765 s | 22.196 s | 14.419 s | 5.429 s | 2.66× |

The current-to-BioIR HTTP ratio is about 9.35–14.68×, but includes residency.
Separate validated-wall timings, including local structure checks, give smaller
ratios and are retained rather than hidden. Sequence checks passed for 218 CIFs;
ten candidate heteromer outputs changed requested chain identifiers. The small
reference-quality distribution is encouraging, not a scientific noninferiority
study. [Full Boltz2 report](../boltz2/report.md).

### OpenFold2 and Protenix

OpenFold2 L40S warm HTTP medians for 46/129/214 residues:

| Implementation | 46 residues | 129 residues | 214 residues |
|---|---:|---:|---:|
| Current | 1.707 s | 6.176 s | 13.185 s |
| Unchanged upstream in new environment | 1.718 s | 6.171 s | 13.210 s |
| BioIR FP32 | 4.844 s | 15.496 s | 29.392 s |
| BioIR mixed precision | 1.188 s | 1.493 s | 2.408 s |

Mixed precision gives about 1.44–5.47× on these cases, with paired CA-lDDT
0.968–0.984 and CA RMSD 0.35–0.75 Å. That is explicitly a precision-changing
candidate, not a same-FP32 speedup. [OpenFold report](../openfold/report.md).
On H100 the same three cases improve from 0.984/2.275/4.292 s to
0.927/1.035/1.213 s with mixed precision (about 1.06–3.54×); FP32 remains slower.

Protenix H100 prepared-input prediction latency falls from 86.9–88.6 s
conventional to 9.2–10.2 s resident, then 5.6–6.1 s with BioIR graphs. The
incremental BioIR ratio is only about 1.66–1.75×. Six-structure native batches
took 25.531 versus 13.367 s, approximately 1.91×. However, median ubiquitin
reference CA-lDDT across 18 seeded/sample outputs fell from 0.633 to 0.492.
**Do not trade this regression for a cheaper request.**
[Protenix report](../protenix/report.md).

### OpenFold3: graphs plus a fair resident control

The first graph-disabled FP32 prototype had little warm-request improvement.
That is **not** the conclusion for the graph-enabled implementation. The latter
keeps the inner model on the GPU across native Lightning teardown. Applying the
same residency change to upstream gives the control below, so saved CPU/GPU
transfers are not misattributed to BioIR. All native preprocessing, confidence
scoring and CIF writing remain inside the measured HTTP boundary.

| H100 variant | 46 aa | 129 aa | 214 aa | Complex287 | Full-MSA95 |
|---|---:|---:|---:|---:|---:|
| Current | 8.820 s | 9.448 s | 9.958 s | 11.140 s | 9.411 s |
| Resident upstream control | 7.658 s | 8.535 s | 9.315 s | 10.356 s | 8.648 s |
| BioIR FP32 graphs + residency | 2.291 s | 2.983 s | 3.972 s | 5.674 s | 2.838 s |
| Control / BioIR | 3.34× | 2.86× | 2.35× | 1.83× | 3.05× |

Each control/candidate sweep completed 23 requests. Paired H100 warm-case
CA-lDDT was 1.000 throughout, with CA RMSD 0.020–0.138 Å. This establishes
potential speed/quality on those cases, not a deployable implementation. L40S
also sped up, but its complex pose differed; those exact results remain in the
lane report. The mixed-precision OF3 variant has a substantial quality regression
and is not recommended. Joint dependency pins still need production integration.

The first H100 graph-candidate request took 6.070 s versus 419.030 s for the
resident-native compiler-cold control. That is a first-request/compiler result,
not the complete image/init/model-load cold-start clock and not a saving on
every warm request. Graph metadata includes query identity, causing recapture;
this overhead remains in the measurements.

**Later correctness gate: hold this graph-enabled prototype.** The snapshot
study's ordinary-load control warmed one query successfully, then failed with
CUDA illegal memory access when the same graph key was reused. The main speed
sweep used distinct query IDs and therefore recaptured graphs. That successful
sweep does not qualify cached-graph reuse across requests. A graph/buffer
lifetime or lifecycle incompatibility is a hypothesis, not an isolated root
cause. Fix it and repeat both same-key and different-key requests before any
customer integration; do not present the timings above as ready-to-deploy gains.

### Models without an equivalent BioIR path

These are fresh baseline medians across the actual tested case sets. They are
not directly comparable across different tasks, GPUs or batch sizes.

| Model | GPU | Measured current request range | Important interpretation |
|---|---|---:|---|
| DiffDock | 1 H100 | 1.785–3.070 s | Repeated single-pose public fixtures |
| Evo2-40B | 2 H100 | 1.343–5.921 s | 64/512/2048 input tokens and 32/64/128 output tokens |
| GenMol | 1 H100 | 0.228–0.379 s | Native valid-generation fixtures |
| MolMIM | 1 H100 | 0.191–2.975 s | **Fallback responses, not newly decoded molecules** |
| MSA Search PDB70 | CPU | 2.187–2.202 s miss; 1.11–1.15 ms hit | Actual search and cached response are separate cohorts |
| ProteinMPNN | 1 H100 | 0.220–0.425 s | Already resident; native batch also tested |
| RFdiffusion | 1 L40S | 40.796–63.683 s | Includes a new native process; 50 steps, lengths 80/160/256, batch1/2 |
| Proteina-Complexa | 1 H100 | 155.032–195.000 s | Full four-stage workflow, 25 generation steps, two targets and batch1/2 |

Complexa's workflow completed, but its tested public fixtures did not meet the
native scientific-success criteria. Its native metadata also says 400 steps
where retained execution arguments show 25; that discrepancy is recorded.
Neither successful execution nor finite coordinates proves biological efficacy.
[Coverage results and failures](../coverage/report.md).

## GPU snapshots and other platform features

Fresh CUDA+CRIU qualification runs in separate workers. Donors are deleted
before restore; different input shapes, repeated cycles and fallback are
exercised. CUDA Graph replay alone never counts as process snapshot success.

Completed trials show a material distinction: restoring correctly does not
necessarily make startup faster. These medians use the same physical GPU,
persistent checkpoint volume and container-to-observed-ready clock, with three
fresh restores and three normal loads each.

| Candidate | GPU | Restore → health | Ordinary load → health | Application result |
|---|---|---:|---:|---|
| Boltz2, asyncio | H100 | 93.60 s | 40.04 s | Correct, but 2.34× slower |
| OpenFold2, mixed | L40S | 98.23 s | 30.89 s | Correct, but 3.18× slower |
| OpenFold3, FP32 graphs | H100 | 292.78 s | 65.35 s | **Failed reused-key inference in both modes; no valid-result startup ratio** |
| Protenix, final non-preemptible cohort | H100 | 88.91 s | 57.82 s | Three restores served; 1.54× slower; scientific quality remains unqualified |

For Boltz2 and OpenFold2, paired normal/restored structure texts were identical
for the tested inputs; new shapes and incompatible-identity normal-load
fallback passed. This does not apply to the failing OpenFold3 prototype. Boltz's
default uvloop fails CRIU capture on `io_uring`, recovers, and needs the explicit
asyncio change to qualify. The earlier approximately 114 s Boltz observation used
a broader initialization boundary and is not this runtime-container clock.
Do not enable these slower restores by default merely because capture succeeded.

The tested checkpoint volumes are RWO `compute-csi-default-sc` block-storage
PVCs: 128 GiB for Boltz/Protenix and 64 GiB for OpenFold2/OpenFold3. They are
reused across fresh pods, **not** a shared RWX filesystem, local NVMe or RAM
cache. These results qualify this storage recipe, not the best achievable
restore speed or every cache level. [Storage provenance](snapshot-storage.md)
records the actual provider-backed disks and documented limits separately.
The live OF3 and replacement-Protenix disks are standard Network SSDs with no
explicit performance override. Their documented size-based throughput limits
are respectively 30 and 60 MiB/s. These are advertised ceilings, not a measured
bandwidth-saturation result; they do not isolate every restore bottleneck.
[Nebius disk-performance specification](https://docs.nebius.com/compute/storage/types#disk-performance).
Increasing storage performance or using a RAM-local cache is a future matched
experiment, not an acceleration already demonstrated here. Existing production
snapshot configurations are not disabled or declared slower by this study.

All four snapshot-model studies and their cleanup are complete, including
negative results. [Snapshot report](../snapshot/report.md),
[Protenix snapshot](../snapshot/protenix/),
[OpenFold3 snapshot](../snapshot/openfold3/).

OpenFold3 failed application-level qualification in all three planned fresh
restores: CUDA/CRIU and health checks passed, but the first inference raised
CUDA illegal memory access and the remaining requests hit a poisoned context.
These are three independent first-request failures, not nine independent kernel
failures. The node remained healthy; the original CUDA fault is not localized
by its asynchronously reported Python stack. Median container-to-health time
was 292.78 seconds, but there was **no valid inference result**, so this is not
a successful startup latency or a useful-result speed ratio. All three ordinary
controls reproduced the error on cached-key reuse after successful warmups,
so the failure is **not isolated to snapshot restoration**. Final planned
controls are complete: six independent first-reuse faults and twelve poisoned
follow-ons, with 7 valid artifacts across 25 attempts (donor, normal warmups and
fallback outputs). The identity-mismatch fallback loaded normally and served
three distinct keys; it does not qualify graph reuse. Hold both the graph-serving prototype and its snapshot
configuration. Metadata-mismatch fallback is not proof of recovery from a
process that fails real inference.

The first Protenix snapshot cohort was interrupted by loss of its dedicated
preemptible node during restore 3, after two successful restores and before
normal controls. Its ratio is therefore unmeasured. The provider subsequently
reported the VM stopped; heartbeat loss preceded the recorded stop operation,
so neither preemption nor a CUDA/runtime failure is established as the cause.
All original evidence is retained. A new, independently captured cohort on
another verified-idle H100 is recorded separately, not pooled across GPU
identities. That second preemptible node also lost contact, this time during
ordinary-load control 3 after three successful restores and two normal controls.
The final independent cohort uses an existing isolated, idle, non-preemptible
H100 with a different driver; its three fresh restores and three ordinary-load
controls all served valid artifacts. The median container-to-health restore is
88.91 s versus 57.82 s for ordinary loading, so this recipe is not faster.
Artifact/sequence validity does not establish numerical or scientific parity,
and cannot waive the underlying BioIR quality regression. The restore schedule
first serves a new 129-residue shape, while each normal control first warms the
76-residue donor shape to match the subsequent request history; do not derive
a matched first-useful-output speedup from those different first requests.
No new capacity was provisioned and no results are pooled across conditions.
See the [first incident](capacity-incident-fkt.md)
and [second incident](capacity-incident-xjaw.md).

Protenix's identity-mismatch fallback genuinely launched a new worker and served
two predictions, reaching health in 13.42 s. This is a **separate, single-run
cache-warm observation**, not the three-control normal-load comparator: the
fallback restored approximately 16.8 MB of donor executable caches before
ordinary loading. The new PID and 2.55 s model-load receipt distinguish it from
the donor's inherited log entry; no CUDA/CRIU restore command ran. Cache reuse
is a known difference, not an isolated causal attribution for the whole gain.
Preserving compiler/kernel caches during normal loading deserves a matched,
repeated follow-up before investing further in the slower process-restore path.

The library does not replace our MCP/HTTP gateway, access control, queue,
autoscaler, model catalog or usage accounting. Existing contracts must survive
an engine swap. The candidate-only tests retain relevant native adapters, but
they do not certify a new public endpoint rollout or multi-tenant load SLO.
Changed images/environments require new snapshot artifacts keyed to the exact
runtime, checkpoint, driver and GPU compatibility; old snapshots cannot be
relabelled as BioIR snapshots. Image and weight caches still matter even when
GPU state can be restored.

These trials establish fresh-pod behavior on the same physical GPU/driver,
**not portability to a replacement preemptible node**. Cross-node/GPU-identity
restoration and the autoscaler path remain a separate acceptance requirement
before advertising snapshot-backed scale-out. Failure to restore must preserve
ordinary model-loading fallback; a missing artifact is not necessarily handled
by the same compatibility-mismatch fallback tested here.

## Economics for per-request pricing

Use **allocated GPU-seconds per acceptable result**, not just kernel duration.
The relevant allocation includes model loading, first-shape compilation,
processing, waiting/cooldown and failed/retried work. The reports retain both
request-window times and whole evaluation allocation windows. Operator gaps and
benchmark debugging are counted in the latter, so those totals are not a
production saturation-price forecast.

For an actual pricing calculation, measure a realistic arrival/batch trace and
use its entire GPU allocation duration divided by accepted results. Multiply
by the applicable GPU-hour price only after choosing that explicit rate. No
free-beta or assumed cloud price is used here. Unsupported candidates and
MolMIM's zero decoded results do not receive a fabricated cost per successful
generation. Snapshot restore can improve elasticity or make costs worse;
the measured startup path, not its cache-level label, decides.

## Recommended implementation order

1. Qualify persistent upstream workers for Boltz2 and Protenix first. This
   removes repeated loading without making the BioIR model/dependency change.
   It still needs normal production contract/load testing; this study did not
   deploy those changes.
2. Prioritize Boltz2 BioIR after fixing chain-ID preservation, locking its
   dependency environment and expanding MSA/complex quality coverage. For
   OpenFold3, first fix and re-test cached-graph reuse across requests. Its
   measured unique-ID gains are potential, not a functioning general serving
   implementation; do not promote it in its present form.
3. Offer OpenFold2 mixed precision only after a workload-specific numerical
   acceptance decision. Its unchanged-FP32 BioIR path is not an upgrade here.
4. Keep Protenix BioIR on hold. Investigate native versus BioIR with identical
   serialized feature tensors and broader references; faster but worse
   structures do not reduce cost per acceptable result.
5. Keep the other eight current runtimes. No unsupported full-model BioIR
   speedup is promised. RFdiffusion/Complexa operator reuse is a separate
   development hypothesis, not an implemented acceleration.
6. Fix MolMIM's measured decode/cardinality behavior before interpreting its
   response rate as generation throughput. Preserve Complexa's scientific
   success metrics alongside workflow completion, including actual step count.

Snapshot enablement is a separate decision, based on the completed tables
above and model-specific reports, not on the presence of CUDA graphs. None of
these recommendations authorizes an automatic production rollout.

## Reproducibility, scope and disposition

One isolated branch/worktree, not a branch per worker. Exact images/checkpoints,
public fixture hashes, command manifests, all completed attempts, startup and
harness failures, reference-quality checks and resource dispositions live with
the lane reports. The public wheel is `bionemo-ir==0.1.0`, SHA256
`ebbfe2808a9a56ad9838a81f887000b89a7554d674bb7a9eac130c7cf2c4e8ae`.
The inspected public source reference is
`401c6fcc4a43925bcf1342b6c0979b060130b396`; we do not assert that it is the
wheel's build commit. The private launch bundle was not redistributed or used.

Only existing, otherwise-unused H100/L40S capacity was used. The exact Evo2
baseline briefly used two unallocated slots on an existing eight-H100 node;
the six original customer GPU pods remained ready and unchanged. No new GPUs,
node groups, quotas, drivers, production model routes or API keys were changed.
All task-owned benchmark pods, PVCs and ConfigMaps are absent in the final
read-only check at 23:31 UTC, and all 27 model deployments have their desired
replicas ready. The former Boltz evaluation-cache PV has also reclaimed
normally; its earlier CSI timeout remains in the
[historical cleanup record](../snapshot/lifecycle/storage-reclamation-exception.json).
The two interrupted Protenix cohorts' resources have reclaimed normally too.
No storage-controller changes or finalizer bypasses were made. Temporary GPU
checkpoint pages were deleted after retaining their fingerprints and results;
they must be recaptured to rerun the experiment. Pinned evaluation images and
local raw evidence remain available.

The final check found the previously released OpenFold3 node `j20` NotReady
after its healthy, GPU-idle cleanup receipt. The provider subsequently recorded
it stopped; the heartbeat loss preceded that Stop operation. This is a separate
post-study capacity incident with unknown initiating cause, not evidence of
what caused the six earlier normal/restored application faults. The original
Protenix nodes `fkt` and `xjaw` were Ready again in the final Kubernetes receipt;
their earlier provider-stopped states must not be presented as current status.
See the [j20 incident](capacity-incident-j20.md) and
[final cluster-state receipt](final-cluster-state.json).

[Machine-readable comparison](comparison.json) is generated by
[aggregate.py](aggregate.py). Scope completeness is not scientific equivalence
or deployment approval. The [single final consistency review](final-review.md)
checked all twelve models and four snapshot-model studies; its two publication
consistency findings were resolved without changing measurements or running
more GPU experiments. [Manager acceptance](review.json) binds that decision to
the final source hashes. Eight offline reporting/archive tests pass, the owned
Python scripts compile, and structured final summaries pass strict JSON parsing.

Raw predictions and logs are preserved byte-for-byte in seven archives with
3,329 per-file checksums. Follow the [extraction instructions](../README.md)
before opening raw-file links or re-running offline analyses. The
[Task Deck handover](../management/README.md) records all nine work items and
the original experiment contract. No implementation recommendation in this
report is an authorization to promote a candidate or start another study.
