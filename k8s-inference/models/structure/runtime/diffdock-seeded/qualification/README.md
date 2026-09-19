# DiffDock seeded candidate: independent retained-evidence review

This is isolated **runtime and numerical-repeatability evidence**, not public
customer acceptance or a claim of accurate docking for arbitrary molecules.
No new inference was submitted for this review and no live configuration changed.

## Exact candidate

- Source: `a5583e15957b1766c0668dba5af265a72f054f8b`.
- Image: `cr.eu-north1.nebius.cloud/e00akg9ndpx77eaexh/fs2-models/h100-fleet/diffdock@sha256:9766b4fb2a22787874bd8d90306980a4cb1ff9808941f0f1ae429d8b8f7cc948`.
- Upstream: `gcorso/DiffDock`, v1.1, revision
  `85c49b60d3e0b0182a59ee43a34a6d7036981284`; MIT.
- The build inherits the existing base image `471db264…` and patches runtime
  source, not weights. Retained adapter identities match the original candidate's
  release archive SHA `5a95b6a1555be47ab1d6f0a8ffd25152f7fe32f5956005bb821e13e7a37d4a3d`
  and ESM checkpoint SHA `ea9d0522b335a8778dea6535a65301f10208dece28cd5865482b0b1fc446168c`.
  This remains an upstream fallback; equivalence to NVIDIA NIM is unproven.

Two independent processes on different H100 nodes each executed
12 frozen complex/seed settings twice: **48 calls and 192 generated poses**.
All request/result/reference hashes, four-pose counts, finite coordinates,
ligand topology and independent reference-frame RMSDs were checked again.

| Process | Node | H100 UUID | Model load | Median inference |
| --- | --- | --- | ---: | ---: |
| r6 | `computeinstance-e00p2817cbemf7w6q5` | `GPU-1462b7c3-181d-f4ac-fa60-d9122abbc5d8` | 15.37 s | 6.17 s |
| r7 | `computeinstance-e00sfarnxpxmaj056b` | `GPU-1bdbdb30-4e41-5144-ef59-8b93002148dc` | 16.55 s | 6.08 s |

Both use driver 580.159.04. These times exclude public queueing, new-node
provisioning and image pull. The Pods completed at 01:34:14 UTC on 19 September 2026;
root owns lifecycle cleanup and any subsequent promotion.

## Repeatability: distinguish the two comparisons

- **Matched request order across processes:** all 24 corresponding results have
  zero difference in serialized coordinates and confidence values.
- **Repeated requests within a process:** the largest coordinate difference is
  **0.0016 Å**, with maximum confidence difference **0.000226975**. These pass
  the predeclared 0.01 Å / 0.001 tolerances, but are **not all byte-identical**.
- This does not establish arbitrary request-order invariance, other GPU/driver
  reproducibility, snapshot behavior or floating-point equality before SDF
  serialization. The fixed startup torsion-normalization table matches across
  processes (`c659445fea83bdaadd10e5585f9e874aa23a376b162c03f992578d7c0ea04a1c`).

## Scientific quality is separate

The six public experimental references are [1A52](https://www.rcsb.org/structure/1A52),
[1IEP](https://www.rcsb.org/structure/1IEP), [1STP](https://www.rcsb.org/structure/1STP),
[3ERT](https://www.rcsb.org/structure/3ERT), [3O96](https://www.rcsb.org/structure/3O96)
and [3PTB](https://www.rcsb.org/structure/3PTB). The frozen requests use a bound
receptor chain without waters/cofactors, seeds 19 and 23, four poses and 18 steps.
Experimental ligand coordinates are evaluation-only; training overlap is unknown.
This is not a paper/PDBbind reproduction or affinity benchmark.

The metric is symmetry-aware heavy-atom RMSD in the original receptor coordinate
frame, **without aligning the ligand onto the reference**. Six of 12 distinct
complex/seed settings have a top-ranked pose below 2 Å; best-of-four succeeds for
the same six. Repetitions do not create 48 independent scientific test cases.

| Complex / ligand | Top-ranked RMSD, seeds 19 / 23 | Best-of-four RMSD, seeds 19 / 23 |
| --- | ---: | ---: |
| 1A52 / EST | 9.73 / 33.67 Å | 9.73 / 33.67 Å |
| 1IEP / STI | 1.97 / 1.70 Å | 1.97 / 1.53 Å |
| 1STP / BTN | 1.43 / 1.00 Å | 1.43 / 1.00 Å |
| 3ERT / OHT | 22.20 / 17.93 Å | 12.21 / 6.33 Å |
| 3O96 / IQO | 5.16 / 4.39 Å | 4.96 / 4.38 Å |
| 3PTB / BEN | 0.21 / 0.22 Å | 0.21 / 0.22 Å |

The 3O96 / seed 19 rank-four output has RMSD **705.87 Å**. It is chemically parsable
with finite coordinates but is a severe scientific outlier, not a successful
binding pose. Its returned confidence is -6.129574, an uncalibrated model score,
not a binding probability. It is preserved, not filtered or concealed by a
service pass.

## Residual RDKit warning: proven reference metadata origin

All 192 generated SDFs explicitly contain the `3D` header flag and parse without
the reported RDKit warning. Each of the six unchanged RCSB ModelServer reference
SDFs instead omits that flag despite nonzero Z coordinates. Parsing each source
reference reproduces the warning independently. The old evaluator parses a
reference once per predicted pose, explaining its 192 warnings exactly.

The reference coordinates and hashes are unchanged. `retained-r6-r7.json`
preserves each actual header, parser diagnostic, source hash and the original
stderr hash. No logger suppression or rewritten reference was used to obtain
this result; diagnostics were captured and attributed to their exact parse.

The runtime logs also retain 67 warnings per process in three separate classes:
the existing torch-cluster compatibility shim lacks the optional `knn` attribute,
TorchScript type annotations trigger warnings, and upstream `parse_chi.py` reports
an invalid cast. Those warnings are not the SDF warning and are **not waived as
a clean public release**. Their original messages remain in the receipt.

## Reproduction and preserved history

`inspect_retained.py` reads the protected original r6/r7 results, independently
checks their bytes/coordinates and prints a report. It makes no network or model
calls. The recorded analysis used RDKit 2025.09.6 and NumPy.

```sh
python inspect_retained.py \
  --campaign /path/to/scientific-qualification-20260918 \
  --references /path/to/qualification-datasets-v5
```

Portable receipt: `retained-r6-r7.json`. Original cross-process report SHA256:
`3b8b617b11e1a69fd8edbbc0bc236454729e1d2ad1891b74e8a31ac4e49a2afc`.
Build receipt SHA256:
`ba8fd27815b1692656083234ab1b3c53ce7343677fe0e68a2216e45a5bb13363`.
Original r1/r2/r3/r4/r5 candidate receipts, numerical mismatches, failed
comparisons and logs remain under their distinct protected cohort directories.
This report neither replaces those failures nor qualifies the public route,
LibreChat integration, snapshot compatibility, concurrency or final release.
