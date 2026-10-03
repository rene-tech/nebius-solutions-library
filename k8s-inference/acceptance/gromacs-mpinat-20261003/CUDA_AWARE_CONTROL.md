# Same-source Open MPI CUDA-aware control

This is bounded native runtime qualification, separate from hosted REST/MCP
acceptance. No service image, catalog default, quota, customer workload, GPU
driver or placement policy was changed by this lane. Evidence below is under
`/home/tux/secure-handoff/fs2-gromacs-mpinat-20261003/runtime-cuda-aware-r2/`.

## Exact build and independent communication facts

The baseline is `gromacs@sha256:c6c353e55deade8c8fe7a2f02e68bcdb639c7680c8435b665e72815d0213c9ea`.
The candidate is `gromacs@sha256:5884569e80a4d8dd5cc04b05fe7700165139874cde00b2774cba7c7c96965e23`,
both in `cr.eu-north1.nebius.cloud/e00akg9ndpx77eaexh/fs2-platform`.
Candidate build source: `38c9bf012b5ab8d0938c11cdfb73d39e726dcb7a`.

The retained original builder and configure log prove that automatic CUDA
library discovery concatenated the compat and stub directories into malformed
linker input. Open MPI 5.0.8 configured successfully **without** CUDA-buffer
support. UCX's separate CUDA transports did not establish Open MPI support.
The candidate uses the same archive/toolchain/options plus the explicit build
library directory `/usr/local/cuda/targets/x86_64-linux/lib/stubs`. Build gates
require positive configure/header/component/query support and forbid runtime
stub search paths. No `GMX_FORCE_GPU_AWARE_MPI` is used.

All 685 entries in the GROMACS, UCX and native worker installation trees are
identical between images; protected-tree inventories have the same SHA256,
`5df42e00e79b074f86656a155bf2a651c2d47321d1d83af9913b07d5957b11c2`.
Only Open MPI and additive build/probe provenance differ. This byte identity is
not itself evidence of numerical equivalence or performance.

The actual two-L40S probe passed initialized `MPIX_Query_cuda_support=1` on both
ranks, unique allocated GPU UUIDs, and exact integer contents for Sendrecv,
Isend/Irecv/Waitall and Allreduce using `cudaMalloc` buffers of 4 B, 4 KiB and
1 MiB. Receipt: `device-probe-l40s-01/receipt.json`, SHA256
`9bc18f8b4486e70582b3e360e40417948c2dfaa52e20a65c2d3b5c802216b2a7`.
This uses configured UCX-local transport; it is not measured bandwidth, RDMA or
an application speedup. Both GPUs were released before hosted acceptance.
Original failed context/build-verifier attempts and the expected baseline
compile-time rejection remain in `summary-01.json` and its referenced logs.

Primary methods, accessed 2026-10-03:

- [Open MPI 5.0.8 CUDA build instructions](https://docs.open-mpi.org/en/v5.0.8/tuning-apps/networking/cuda.html).
- [Initialized CUDA support query](https://docs.open-mpi.org/en/v5.0.8/man-openmpi/man3/MPIX_Query_cuda_support.3.html).
- Exact source/build detail is in the additive runtime's `cuda-aware/README.md`;
  GROMACS was not rebuilt and retains source `da9e013175bae98b31b34384f6b4864ff29f65a5`.

## Matched scientific inputs and native checks

All controls use the frozen 81,743-atom MPINAT MEM input, three independent
restarts of the same initial state, each 10,000 steps / 20 ps with 2 fs steps.
Original TPR SHA256:
`5099268bf3a3d948c03b3b78432f29a2c5d4207b54d02e7e294f8138b587d473`.
Finite benchmark TPR SHA256:
`4a0d845ae01b8c95b1077cd099c5769fce99d939844e24d21f1d1d90752540b9`.
Input archive SHA256:
`a2748bf521f68334fa0ebb38a5cd5957a82b4a9718b948e1fc9ac253342743d2`.

Both images use eight OpenMP threads per rank and explicit
`-nb gpu -update cpu -bonded cpu -pme gpu -pmefft gpu -notunepme`;
`-npme 0` for one rank, `-npme 1` for two ranks. Cutoffs remain 1.0 nm and the
mesh remains 96×96×80, order 4. Native neighbor-list adjustment is retained and
disclosed, not confused with a physical cutoff change. No forced halfway timing
reset. Original benchmark constraints, thermostats, barostats, seeds and output
cadence are preserved, not recommended as new production-science defaults.

Every artifact inventory is rehashed. Checks require all requested steps,
complete observed rank/GPU bindings, checkpoint step 10,000, 81,743 finite final
coordinates/velocities, positive periodic cell and kinetic temperature, native
finite energy records and no LINCS warning. An independent CPU-only exact-image
`gmx_mpi dump -cp` reads every final checkpoint, hashes/scans its full stream,
and retains only a bounded header. This proves readable native checkpoints,
**not** interruption/recovery or CUDA process snapshots.

The original cadence retains initial/final energy records, a single final XVG
sample, and a native averaged decomposition; no per-step energy time series is
invented. The parser separates averaged records from the final time point.

## One-rank L40S results

`mem1-baseline-01` and `mem1-candidate-01` passed all native gates and were
deleted with absence observed. Driver: 580.173.02. Native, warm-up-inclusive
rates in ns/day:

| Image | Repeat 1 | Repeat 2 | Repeat 3 | Mean | Sample SD |
| --- | ---: | ---: | ---: | ---: | ---: |
| Baseline | 188.637 | 188.307 | 195.859 | 190.934 | 4.268 |
| Candidate | 183.859 | 195.398 | 201.098 | 193.452 | 8.783 |

The variation overlaps and the shared node had unrelated admitted workload.
These three serial timing repeats are not independent ensemble samples; they
establish no reliable speedup, confidence interval or strong-scaling benefit.

The strict initial printed-energy equality check **failed**, and remains failed
in `mem1-comparison-01/summary.json` (SHA256
`89fad0b381b214ab4fcbdb282fa82cd0c76785ec7262ade9fbfa434fe812d6af`).
All actual native checkpoint/finite-state checks passed separately. Differences
include one 10 kJ/mol printed potential-energy unit, 0.1 kJ/mol LJ units, small
pressure differences and 1e-10 constraint-residual units.

The subsequently requested descriptive screen compares each initial term with
the three baseline values' minimum/maximum expanded by **one actual baseline
printed unit**. All 16 terms fall within that envelope. It preserves every raw
token, per-term unit and repeat, uses decimal arithmetic at inclusive bounds,
does not divide by zero or impose a blanket relative tolerance, and is explicitly
not a pre-registered scientific acceptance criterion. In this actual fixture no
initial printed term is zero; a synthetic zero-term regression tests that case.

Examples of the observed inclusive bounds:

| Term | Bound | Added printed unit |
| --- | --- | --- |
| Potential | −1,287,830 to −1,287,810 kJ/mol | 10 kJ/mol |
| LJ (SR) | 49,249.5 to 49,249.9 kJ/mol | 0.1 kJ/mol |
| Pressure | 412.997 to 413.008 bar | 0.001 bar |
| Constraint RMSD | 1.53829e−5 to 1.53832e−5 | 1e−10 |

The complete additive supplement is
`mem1-printed-range-01/printed-range-supplement.json` (SHA256
`9493a4f89072c97abd25fd321bf9bd5860b614a3e19509ef3d9ccdb4ac0fad7a`).
It binds and does not overwrite/relabel the strict failure. This observed range
is not a population confidence bound, floating-point error proof or ensemble
equivalence. Final states can differ; their endpoint spread is retained without
post-hoc forcing equality.

## Cross-node and further controls

`qualify_cuda_two_node.py` changes only shape from 1×2 to 2×1 in a separately
frozen request, preserving the archive byte-for-byte. Its JobSet reuses the
unchanged worker's attempt-local SSH and rank-binding protocol, requires two
explicit free H100 QA nodes, and retains all-namespace occupancy checks for both.
No secret seed/private key is exported. The candidate additionally tests actual
device buffers before running MD. GROMACS keeps the existing **host-staged TCP,
direct-GPU-communication-disabled** cross-node profile. No RDMA assertion follows.

Both 2×1 H100 cohorts completed and their JobSets/Pods were deleted, with absence
observed. Candidate device-buffer validation passed on both H100s through the
existing configured TCP profile, using the same three buffer sizes/operations
as the L40S probe. Native functional and independent CPU checkpoint checks
passed for all six MEM runs. Driver remained 580.173.02.

| Image | Repeat 1 | Repeat 2 | Repeat 3 | Mean | Sample SD |
| --- | ---: | ---: | ---: | ---: | ---: |
| Baseline | 59.787 | 59.552 | 59.268 | 59.536 | 0.260 |
| Candidate | 59.146 | 59.284 | 59.190 | 59.207 | 0.070 |

No cross-node speedup was observed. `mem2x1-candidate-01/receipt.json` has SHA256
`a5a48e8fc3b9ad8256d47b98a980e1efb1c14d267c2f87b7a1c8ec9df334e467`;
it is accepted by the read-only `bind_candidates.evidence` adapter, together
with `mem1-candidate-01`, but this adapter result is not promotion.

The strict comparison again failed initial printed equality:
`mem2x1-comparison-01/summary.json`, SHA256
`dec47872e95ce87d87aedc2639737271fb87f92098f93e870c06af4917a4b993`.
All initial printed energy terms match. The subsequent range screen also stays
**outside_bounds** for one pressure observation. Baseline values are
412.922/412.923/412.928 bar; the fixed range plus one 0.001 bar unit is
412.921–412.929 bar. Candidate values are 412.925/412.922/412.919 bar, with the
last 0.002 bar below that descriptive envelope. Constraint residuals remain
inside the explicit 1.53717e−5–1.53720e−5 envelope; every other term is inside.
The supplement is `mem2x1-printed-range-01/printed-range-supplement.json`, SHA256
`8eb59bc1ce81b8ee5501b69b2c281d57cacd51851db23b480576a6dbd5b69f64`.
Neither failure is widened or relabelled, and no repetitions are added to chase
a passing range. Three baseline observations do not define a statistically
justified scientific release threshold.

Interpretation is kept separate from observation: GROMACS documents
nondeterministic GPU force summation and MPI reduction order, so exact printed
equality is not guaranteed even for repeated unchanged calculations. That is
relevant context, not proof of the cause of this individual difference. The
0.002 bar envelope miss alone establishes neither a runtime defect nor ensemble
validity. [Official reproducibility discussion, accessed 2026-10-03](https://manual.gromacs.org/2026.2/user-guide/managing-simulations.html#reproducibility).

The matched 1×2 L40S application control is queued behind existing whole-node
hosted jobs; no GPUs are held while waiting. New 1×4, 8/16-GPU, actual native
interruption/recovery and hosted candidate claims remain unqualified.

## Reproduction and tests

`cuda_mpi_control_report.py` takes `--baseline`, `--candidate`, `--fixture`,
`--ranks` and a new `--output` directory. Its strict mode independently rereads
checkpoints and native inventories. `--strict-summary` creates only an additive
printed-range supplement from rehashed original logs and a bound strict report.
Both modes exclusively create outputs and never rewrite originals.

```text
python3 -m unittest -v test_cuda_mpi_control_report.py test_qualify_cuda_two_node.py \
  test_qualify_candidate.py test_candidate_report.py test_pme_control.py
```

45 focused tests pass. The new tests cover native statistics parsing, nonfinite
values, changed inputs/kernels, per-term resolution including zero/small terms,
the original failed strict criterion, guarded two-node placement, unchanged
science inputs and the independent mandatory device-buffer gate.
