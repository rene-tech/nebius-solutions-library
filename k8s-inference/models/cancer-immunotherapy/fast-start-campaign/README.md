# Fast-start live campaign for qualified scientific models

Repeated live H100 fast-start trials for the cancer-immunotherapy runtimes that
are already semantically qualified. Fifteen task-owned Kubernetes Jobs ran on
the shared `k8s-inference-h100` cluster on 2026-09-03. All fifteen completed and
all fifteen passed a semantic gate that reads the structure the run produced.

This replaces inventory of old one-off receipts with a measured cohort: three
prepared-node trials per model per variant, each with a phase-resolved timeline
reconstructed from objects the cluster itself recorded.

## Result

Model start is measured the way `FAST_START_LEVELS.md` defines it: from the
capacity-available point to the first semantic result. For a batch stage the
capacity-available point is the instant the Pod is scheduled onto a Ready
accelerator node, so image setup, volume setup, artifact load, runtime
initialisation and compilation all count inside model start. Queue admission
and placement are reported separately as capacity wait and are excluded.

| Model | Variant | Trials | p95 model start | Level |
| --- | --- | ---: | ---: | --- |
| Mosaic | baseline | 3/3 | 516.5 s | `Off` |
| Mosaic | fast-volume | 3/3 | 367.4 s (steady state 228.1 s) | `Off` (steady state `L1`) |
| BoltzGen | baseline | 3/3 | 345.3 s | `Off` |
| BoltzGen | fast-volume | 3/3 | 45.9 s | `L3` |
| AlphaFold 3 | baseline | 3/3 | 60.8 s | `L2` |

Every number above is a p95 over three trials, which at that cohort size is the
worst observation. `CAMPAIGN_SUMMARY.json` carries min, mean, max and p95 for
every phase, and `receipts/` carries one receipt per trial.

## What actually dominates startup

The largest single contributor was not the image, the weights or compilation. It
was the kubelet's recursive `fsGroup` ownership pass over the shared artifact
claim, which the default `fsGroupChangePolicy: Always` performs on **every** Pod
start. On a 128 GiB `ReadWriteMany` claim holding roughly 45,000 molecule files
that cost 153–305 s before the container even began.

Setting `fsGroupChangePolicy: OnRootMismatch` cut it to 1–2 s. That single
change moved BoltzGen from `Off` to `L3` and Mosaic's steady state from `Off` to
`L1`, without touching either runtime image, its argv or its artifacts.

The penalty is also contagious across Pods, which is why the `fast-volume`
Mosaic cohort still contains one slow trial. `mosaic-fast-volume-t01` emitted no
ownership events of its own, yet waited 154 s, because the kubelet serialises
volume setup per node and claim and two concurrent `baseline` BoltzGen Pods were
performing their `Always` walk over the same claim on the same node at that
moment. `summarize.py` attributes this by overlapping each trial's mount window
against every other trial's ownership walk on the same node and claim, so the
exclusion is evidence-based rather than chosen by hand. Both the all-trials and
the steady-state cohort are published; neither replaces the other.

AlphaFold 3 never paid the penalty. Its binding deliberately sets no `fsGroup`
and reaches the licensed parameter tree through `supplementalGroups: [65532]`
instead, and its measured volume setup was 1–2 s throughout. That is an
independent confirmation of the diagnosis rather than a separate result.

### Where the remaining time goes

| Model | Volume/sandbox | Runtime init and artifact load | Compute to first result |
| --- | ---: | ---: | ---: |
| Mosaic | 2.0 s (optimized) | 141–153 s | 69–73 s |
| BoltzGen | 1–2 s (optimized) | 2.9–3.7 s | 36.8–41.2 s |
| AlphaFold 3 | 1–2 s | 13.3–13.6 s | 43.7–45.4 s |

Mosaic's remaining floor is its own 141 s runtime-init phase: it verifies a
2.29 GB Boltz-2 checkpoint by SHA-256 on every start, then pays full XLA
compilation because the JAX persistent cache lives in the per-Pod `/tmp`. The
`warm-jit` variant in `campaign_matrix.json` is defined to isolate that
compilation cost and has not been run; the ownership finding was the larger and
cheaper win and was measured first.

## Separated mechanism evidence

The parent task asks for image, artifacts, weights, snapshot, host-RAM and hot
paths to be measured separately rather than inferred from Pod readiness.

- **Image.** Measured on its own with a deliberate prewarm Pod per accelerator
  node that runs the image's no-GPU smoke command, so only image setup happens.
  Cold pulls: Mosaic 4,204,439,320 bytes in 105.5 s and 96.9 s; AlphaFold 3
  4,460,819,393 bytes in 79.4 s. Every timed trial then ran against a node-cached
  image, confirmed per trial by the kubelet's own `Pulled` event text rather than
  assumed.
- **Artifacts and weights.** Delivered from the regional shared filesystem, never
  baked into an image. Mosaic verifies 2,293,242,770 bytes by digest inside the
  run; BoltzGen reads a 1.93 GB checkpoint, a 2.29 GB folding checkpoint and a
  45,227-file molecule tree; AlphaFold 3 reads a 1,020,545,840-byte compressed
  parameter object. Digests are recorded per trial in each receipt.
- **Local NVMe.** Unavailable, proven by the node label
  `local-nvme.fs2.nebius/eligible=false` on both H100 nodes, not by inference.
- **GPU process snapshot.** Unsupported. Node label
  `snapshot.fs2.nebius/eligible=false`. **No snapshot was captured and none was
  restored, so no level anywhere in this campaign is attributed to a snapshot.**
  `run_checks.sh` fails if any receipt claims otherwise.
- **Host-RAM standby and hot replicas.** Not exercised. These are batch stages
  that terminate at the end of each trial, so no standby process and no Ready
  Service endpoint exists to measure. Recorded as not-exercised rather than as a
  result.

## Semantic gates

A zero exit code is not evidence that science happened, so each trial is gated
on the structure it produced.

- **Mosaic** reuses the owning task's own `structural_report` from
  `../runtime-images/mosaic/qualification/validate_result.py` rather than
  restating it, then checks backend and source-revision identity, that the
  sequence length matches the residue count, and that iPTM and pLDDT are in unit
  range and above trivial. Observed across six trials: 40 residues, 200 atoms,
  15–16 distinct residue types, iPTM 0.678–0.859, mean pLDDT 0.903–0.928.
- **BoltzGen** reads the ranked design CIF back off the shared filesystem and
  checks atom and residue counts, and that the run really used fused kernels on
  a compute-capability 9.0 device. Observed: 1,231 atom records over 162
  residues, 2 CIF files.
- **AlphaFold 3** writes into an ephemeral scratch volume that is deliberately
  not copied onto the tenant-private licensed claim, so the run itself digests
  its top-ranked CIF and prints the result. Observed: 5 samples extracted, 6 CIF
  files, 128 atom records, self-reported inference 43.69–45.4 s and
  featurisation 6.05 s.

Determinism differs by runtime and is reported, not smoothed over. All six
BoltzGen trials produced a byte-identical ranked design, SHA-256
`97d07a4cf2c1…`, across both variants and both nodes. Mosaic is seeded
identically in every trial yet its iPTM spread across trials is 0.678–0.859,
which is expected for a GPU-nondeterministic optimiser and is why the gate
checks ranges rather than an exact digest. AlphaFold 3 produced a stable 128
atom records per trial but a different CIF digest each time.

## GPU telemetry

`gpu_metrics.py` attaches DCGM series from the cluster Prometheus to each
receipt. The exporter publishes no Pod label, so a series cannot be tied to one
Pod when several trials share a node. Every value is therefore recorded as
node-scoped and each receipt names the concurrent campaign trials that were
resident on the same node during its window. Mosaic trials show node peak GPU
utilisation of 91–99%.

## AlphaFold 3 lanes

The lane measured here is the stock upstream v3.0.4 academic image that carries
the retained H100 semantic evidence, run through the official
`--norun_data_pipeline` path. Because the data pipeline is skipped, the
still-unpublished `alphafold3-public-databases-v3.0` bundle is not on this path
and does not block the measurement.

The clean runtime-wrapper successor is **not** measured here. Its owning task
published `sha256:2c357d97…`, then reported that a reviewer had blocked that
digest, that it is being superseded, and that the owning task is running the
semantic trial itself. No Job was submitted against it from this campaign. The
lane is recorded in `campaign_matrix.json` as `pending-upstream` with the
replacement planned as one production-equivalent semantic inference against the
same official subpath parameter mount, then three trials once it passes, using
the academic lane's p95 of 60.8 s as the comparison baseline. That runtime
verifies parameter identity before load, so it is expected to add a one-off read
of the 1.02 GB parameter object that this lane did not perform.

## Reproducing

`campaign_matrix.json` is the single source of truth: image digests, argv,
mounts, security context, per-variant overrides, log markers and semantic gates.

```sh
export KUBECONFIG=~/.local/state/k8s-inference-dual-acceptance/h100/run/kubeconfig
kubectl config current-context   # must be k8s-inference-h100

python3 faststart.py prewarm --model mosaic                      # image setup, measured separately
python3 faststart.py submit  --model mosaic --variant fast-volume --trial 1
python3 faststart.py wait    --job fsc-mosaic-fast-volume-t01-771b60541fc6
python3 faststart.py collect --model mosaic --variant fast-volume --trial 1
python3 validate_trial.py    --model mosaic --variant fast-volume --trial 1
python3 gpu_metrics.py
python3 summarize.py
python3 faststart.py cleanup                                     # removes every task-owned object
```

`./run_checks.sh` runs the whole offline suite: syntax, JSON well-formedness,
that `CAMPAIGN_SUMMARY.json` is current with the receipts, that every receipt
has a semantic verdict and a level that follows the published thresholds, that
no negative duration is ever published, that no snapshot claim exists, and 25
unit tests.

Trials are named deterministically from the campaign, model, variant and trial
index, so re-running `submit` for the same trial replaces that trial rather than
accumulating duplicates.

## Honesty rules the harness enforces

- A boundary that cannot be observed is reported as `null` and named in
  `unavailable`. It is never estimated, and `level_for(None)` returns
  `unavailable` rather than a level.
- Phase boundaries come from the Job, the Kueue Workload, Pod conditions,
  kubelet events and container log timestamps, all read back from the cluster.
- Pod timestamps are second-granular while log timestamps are nanosecond, so a
  sub-second phase can compute negative. Such a value is clamped to zero and the
  clamp is disclosed in `precision_notes`, never published as a negative.
- A cached image is never counted as a pull; the kubelet's own wording decides.
- `readOnly` is set on the mount and never on the claim, because a read-only
  claim marks the whole CSI attachment read-only and would silently strip write
  access from the workspace mount of the same claim.

## Deviations recorded rather than hidden

- BoltzGen and AlphaFold 3 run with `readOnlyRootFilesystem: false`. Neither
  image was hardened against a read-only root the way the Mosaic image was, and
  this campaign measures startup rather than auditing the hardening contract.
  Every writable path is still an explicit mount and the shared artifact plane
  stays mounted read-only. Mosaic keeps `readOnlyRootFilesystem: true`.
- AlphaFold 3 runs in `fs2-academic-poc` outside Kueue. The
  `inference-accelerators` ClusterQueue selects only `fs2-models`, and the
  parameter claim is tenant-private to `fs2-academic-poc`; a cross-namespace
  claim mount is not possible. Its GPU is therefore outside Kueue quota rather
  than borrowed from it, exactly as its original semantic evidence Job ran.
- AlphaFold 3 sets `PYTHONUNBUFFERED=1`. Without it the run buffers its whole
  output and flushes at exit, collapsing every phase boundary onto one instant.
  A first round was run without it and discarded rather than reported with
  fabricated boundaries.
- The trials are pinned to the `h100-reserved-8x` capacity-block flavor. The
  preemptible `h100-1x` pool that carried the original one-off Mosaic
  qualification was at zero nodes for this campaign.
- The quarantine claim `cancer-immunotherapy-academic-assets-rwx-v1` is never
  mounted by any runtime; a unit test pins that.

## Files

| Path | Role |
| --- | --- |
| `campaign_matrix.json` | Model identities, argv, mounts, variants, gates, mechanism evidence |
| `faststart.py` | Prewarm, render, submit, wait, collect, cleanup |
| `validate_trial.py` | Semantic gates per model |
| `gpu_metrics.py` | DCGM telemetry attachment |
| `summarize.py` | Cohort statistics and level assignment |
| `af3-fold-input.json` | AlphaFold 3 fold input for the no-data-pipeline path |
| `receipts/` | One receipt per trial |
| `raw/` | Container logs and cluster objects each timeline was derived from |
| `CAMPAIGN_SUMMARY.json` | Generated cohort summary |
| `run_checks.sh` | Offline check suite |
