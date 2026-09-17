# Protenix BIR fresh-pod snapshot qualification

Status: partial / infrastructure-blocked. No production promotion or scientific-parity approval.

Independent fresh xjaw donor/GPU/checkpoint cohort; original fkt receipts are retained separately in the parent directory. No UUID remapping. Replacement node also became NotReady during normal-control3 BEFORE observed readiness/inference. Three restores and two controls completed; fallback unrun. Stop boundary reached; no additional node retry. Timing ratio is a descriptive3v2 observation, not planned acceptance completion.

## Outcome

Three same-node fresh restores and three ordinary-load controls are required. Current completion: 3 restores, 2 controls. The matched readiness ratio (restore/ordinary load) is 1.452×; values above1 mean slower restoration, not acceleration.

Matched-history restored outputs: 0/6 byte-identical to normal control1; paired CA-lDDT range 0.660629–0.898911. This is a small technical cohort, not scientific equivalence. The underlying model-lane experimental-reference quality regression remains disqualifying even if snapshot/normal outputs agree.

Numerical dispersion is retained separately; no causal attribution is made:

- restore, ubiquitin-76: within-cohort paired CA-lDDT 0.684103–0.786345 (3 pair comparisons).
- restore, lysozyme-129: within-cohort paired CA-lDDT 0.765538–0.875699 (3 pair comparisons).
- normal, ubiquitin-76: within-cohort paired CA-lDDT 0.850281–0.850281 (1 pair comparisons).
- normal, lysozyme-129: within-cohort paired CA-lDDT 0.688902–0.688902 (1 pair comparisons).

## Exact binding and control

- Runtime: `cr.eu-north1.nebius.cloud/e00akg9ndpx77eaexh/fs2-evaluation/bioir-protenix@sha256:0c391bdc2ab0c5260a222ec7df5e5adc5ea9bfdbc2e158b386a97aac5dc72e94`; public `bionemo-ir==0.1.0`, PyTorch2.12.0+cu130, Python3.12.
- Tools: `cr.eu-north1.nebius.cloud/e00akg9ndpx77eaexh/fs2-snapshot/scientific-tools@sha256:17cc3536dd847355b8457b2e92bd7d0fdf292bdd8e6acc457e25f14e28284ba4`. Exact frozen application/snapshot helper hashes: [source hashes](inventory/source-hashes.json).
- Source/checkpoint binding: source2475421477ab414b571149ad4a875c390ff8a35d, checkpoint8f931f9774a396b67033d0e58628e1834f4a1448165e04254b40a780b0c0d599; manifesta093d28ecfc8374f143cc32ff713b0e6ad1124c095dbbca5af6e51b4f7dcc6b7.
- Same node `computeinstance-e00xjaw5jqexvvnpat`, GPU `GPU-ca98bcbc-cf06-a4c2-e3bc-c27a0cba95bc`, driver `580.159.04`, kernel `6.11.0-1016-nvidia`; no remapping.
- Exact global-RNG/token-transformer-graph prototype from model lane. Serving remains Python HTTPServer, without a uvloop compatibility change. Read-only host-local public model artifacts; own128Gi RWO snapshot PVC. GPU allocated normally by Kubernetes, one device per pod.
- Each prediction retains seed101,10 cycles,200 steps,one sample,no MSA/templates/RNA, native input/provenance/confidence/cardinality validation. Structures additionally require exact amino-acid sequence, finite coordinates and actual `GRAPH_VERIFIED` capture state.
- Capture follows donor76-aa ubiquitin warmup and synchronous `/prepare-snapshot` CUDA drain. Actual CUDA owner PID174 is dumped, not its supervisor. Donor deletion and free-GPU checks precede every fresh restore. New129-aa lysozyme is then evaluated before replaying76-aa input.
- Normal controls use load→76→129→76, matched against donor76→capture→restore→129→76. Readiness ends before normal76 warmup. First-output shapes differ, so their raw clocks are retained but not converted into a misleading first-output speed ratio.

## Timings

| Cohort | n | Container→ready median,s | Pod-create→ready median,s | First validated request median,s |
| --- | ---: | ---: | ---: | ---: |
| donor | 1 | 62.052 | 98.052 | 14.251 |
| restore | 3 | 87.741 | 122.741 | 6.549 |
| normal | 2 | 60.411 | 90.411 | 14.572 |

Capture: 0.966s CUDA checkpoint, 2.821s CRIU dump, plus 85.435s durable flush. Captured pages≈5.395GB; generated executable cache≈16.8MB. Image pull/init/mount, CRIU restore, request and graph phases are separate in [analysis](analysis.json), lifecycle files and supervisor logs. The original fkt donor pulled its image in6.01s; replacement xjaw reuses its already-cached image. Neither is silently treated as a cold-start advantage. Restored health `model_load_seconds` is inherited donor metadata, never a restore stopwatch.

Ordinary-load controls have an empty task executable cache. Restore and guarded normal-load fallback reuse the donor's captured executable cache; this is part of the measured deployment path. Fallback is reported separately and is not pooled into the three normal controls. Its single cached-start observation does not qualify a cache-only optimization or a matched request-history startup comparison.

Allocated GPU-slot proxy bounds across closed, release-verified pods only: 1046.711–1059.571s, including diagnostic/operator gaps and durable flushing. This is not saturated production throughput or GPU compute time.

Per-request clocks end after the native wrapper and finite-coordinate/cardinality validation. Extra sequence/graph audit and evidence collection occur outside that request clock; they remain mandatory success gates and are included in the allocated-pod windows. Readiness polling and controller overhead are retained, not subtracted.

## Failures and limitations

In the original fkt cohort, the first two warm-client attempts failed before inference: CPU `prep` was initially routed into the prediction-only bridge; an unbridged retry found the GPU candidate lacks `zstd` for packaging CPU handoffs. All12 failed CPU-preparation stage records and logs remain in the original cohort, not counted as replacement failures. Final comparisons reuse byte-hashed validated CPU handoffs from the exact current-model lane, as its benchmark does. No model, confidence or artifact validator was bypassed.

The donor first76 output differs from its post-restore76 result after129. That observation alone is confounded by graph/request history; it is not described as tiny or automatically attributed to checkpointing. Matched-history normal controls and their own dispersion inform comparisons but do not establish a cause. CA-lDDT against experimental references remains descriptive and does not establish all-atom quality, interface quality or non-inferiority.

Observed compatibility is limited to this GPU UUID, image, source, kernel and driver. No cross-GPU remapping, version upgrades, arbitrary batch envelope, concurrency/cancellation, production route or customer workload was exercised here. Model-lane broader native feature evidence remains separate.

## Reproduction and disposition

Review capacity and ownership first. `control.py prepare`, `control.py donor`, `operate.py ready/request/capture/release` freeze and capture the task-owned worker. `matrix.py` performs three restores, three controls and a revision-mismatch fallback; `summarize.py` and `write_report.py --complete` regenerate results from raw records. Select `BIOIR_SNAPSHOT_COHORT=xjaw` for replacement artifacts; `run_fresh.py` orchestrates that complete fresh cohort. Original manual restore1/2 and local-controller interruptions are retained separately.

No host configuration, driver, production model or routing changes. GPU/Task Deck skills guided node preflight, scoped resource ownership and retained phase/quality evidence; the Kubernetes diagnostic workflow stopped the interrupted cohorts at node unavailability without unsafe host intervention. Cleanup disposition is recorded separately in [lifecycle](lifecycle/); no checkpoint binary needs production promotion.

The interrupted pod lifetime and final GPU release are unverified and excluded from closed-pod cost bounds. Normal cleanup acknowledgements do not prove disk reclamation.

Final resource closure supersedes earlier pending-cleanup descriptions: this cohort's task pods, PVC, PV and immutable ConfigMaps are absent through normal reclamation. Historical incident/cleanup receipts remain unchanged.
The final saved manager receipt shows this node Ready=True again. Earlier provider STOPPED state is historical; GPU memory was not re-probed after interruption.
