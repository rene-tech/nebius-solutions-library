# Explicit PME protocols and fair comparisons

Evidence date: 2026-10-03. This is an additive native benchmark protocol, not a
runtime default, image replacement, or admission/capacity change.

## What the current baselines actually mean

`prepare.parameters()` leaves PME automatic for MEM. `run_mpi.shape_parameters()`
currently delegates to it; the worker adds `-pme auto` when absent. The separate
candidate regression explicitly requested CPU PME for its MPI shapes. Neither
an MPI App label nor GPU allocation proves GPU PME. Compare actual native
commands and dispatch lines, not just submitted parameters.

Single NGC and external-MPI binaries differ. Even on one GPU, their timings
cannot be treated as strong scaling of one executable. Prior CPU-PME regression
also allowed bonded placement and PME tuning to vary automatically.

## Named fixed-input controls

`pme_control.py` preserves the original MEM bundle and 10,000-step conversion.
Both variants use `-nb gpu -update cpu -bonded cpu -notunepme`. CPU PME uses
`-pme cpu -pmefft cpu -npme 0`. GPU PME uses `-pme gpu -pmefft gpu`, with the
following layout:

| Total topology | PME layout for this exact build | Qualification meaning |
| --- | --- | --- |
| 1 node × 1 GPU | `-npme 0`, shared PP/PME rank | Native fixed-input control |
| 1 node × 2 GPUs | `-npme 1`, one PP and one PME rank | Native fixed-input control |
| 1 node × 4 GPUs | `-npme 1`, three PP and one PME rank | Source-valid choice; GPU-PME run not measured here |
| 1 node × 8 GPUs | `-npme 1`, seven PP and one PME rank | Source-valid choice; unqualified capacity/layout |
| 2 nodes × 8 GPUs | `-npme 1`, fifteen PP and one PME rank | Source-valid choice; unqualified capacity/layout/network |

Disabling PME autotuning keeps the original 1.0 nm cutoff, 96×96×80 order-4
mesh and 2 fs timestep throughout these controls. This is intentionally separate
from the auto-tuned public baseline. Native neighbor-list adjustments are
retained; they are not relabelled as physical cutoff changes. All timings retain
startup/warm-up. Three repetitions restart the same state, not independent
equilibrium configurations. No forced halfway reset is used.

The candidate has cuFFT but no distributed GPU FFT library. Do not use multiple
full-GPU PME ranks or force experimental GPU PME decomposition. GPU PME requires
compatible input: PME electrostatics, order 4, dynamical integration and no
LJ-PME. CPU update preserves MEM's original coupled all-bond constraints. Do not
change those constraints, timestep, masses or force field to obtain a speedup.
The exact source rejects free-energy perturbation in hybrid GPU-PME/CPU-FFT
mode, but does not contain a blanket FEP rejection for full GPU PME.

Primary references: [GROMACS 2026.2 performance guide](https://manual.gromacs.org/2026.2/user-guide/mdrun-performance.html),
[native options](https://manual.gromacs.org/2026.2/onlinehelp/gmx-mdrun.html),
[exact task-selection source](https://github.com/gromacs/gromacs/blob/da9e013175bae98b31b34384f6b4864ff29f65a5/src/gromacs/taskassignment/decidegpuusage.cpp),
[exact input restrictions](https://github.com/gromacs/gromacs/blob/da9e013175bae98b31b34384f6b4864ff29f65a5/src/gromacs/ewald/pme.cpp),
and [exact GPU FFT selection](https://github.com/gromacs/gromacs/blob/da9e013175bae98b31b34384f6b4864ff29f65a5/src/gromacs/ewald/pme_gpu_internal.cpp).

## CUDA-aware MPI gap: evidence, not an override

The exact candidate's Open MPI reports `opal_built_with_cuda_support:false` and
only the null accelerator component, despite a recorded `--with-cuda` configure
option. An initialized runtime query under the UCX-local environment returns
`MPIX_Query_cuda_support=0`. UCX itself exposes CUDA copy and IPC transports.
Those observations are compatible: UCX capability and an extension name alone
do not establish Open MPI CUDA-buffer support. The one-rank warning has no
inter-rank communication consequence; larger layouts can incur host staging.
No `GMX_FORCE_GPU_AWARE_MPI` override was applied.

The installed final image does not contain the original intermediate
`/src/openmpi-5.0.8/config.log` or GROMACS CMake cache. Thus the precise historical
failed configure/link check remains unconfirmed, not fabricated from the flag.
The exact Open MPI configure source checks CUDA headers/library and `cuMemFree`;
the recorded CUDA build image installs the compatibility driver and sets its
stub-library path. Explicit path resolution must be checked in a fresh CPU
build rather than assuming the automatic discovery succeeded.

Bounded counterfactual plan, separate from the functional rollout:

1. Reuse the exact CUDA/UCX/Open MPI/GROMACS source pins. Retain configure logs
   and the resolved CUDA header/stub paths; pass the verified single
   `--with-cuda-libdir` explicitly. Preserve compiler, scientific source and
   precision. If a different configure failure appears, fix that evidence-backed
   cause rather than substituting a version silently.
2. Fail the build unless Open MPI reports CUDA support true and contains its
   CUDA accelerator, and GROMACS records its MPI extension/query detection.
   Retain these assertions and CMake cache in build provenance.
3. Qualify a separate immutable image on two free GPUs: initialized query true,
   real device-buffer send/receive and collectives with checked values under
   the exact UCX profile, then matched native MEM controls, checkpoint/readback
   and numerical checks. No force flag before such an MPI buffer test.
4. Compare direct-communication dispatch and repeated matched timings. Do not
   infer RDMA from CUDA-aware MPI or extrapolate a one-node test to two nodes.
   Only root may promote a qualified successor; the current functional image
   remains unchanged.

Sources: [Open MPI 5.0.8 CUDA guidance](https://docs.open-mpi.org/en/v5.0.8/tuning-apps/networking/cuda.html),
[initialized runtime query](https://docs.open-mpi.org/en/v5.0.8/man-openmpi/man3/MPIX_Query_cuda_support.3.html),
[exact CUDA configure check](https://github.com/open-mpi/ompi/blob/v5.0.8/config/opal_check_cuda.m4),
[exact GROMACS configure detection](https://github.com/gromacs/gromacs/blob/da9e013175bae98b31b34384f6b4864ff29f65a5/cmake/gmxManageGpuAwareMpi.cmake).

## Reproduction and evidence

Private root:
`/home/tux/secure-handoff/fs2-gromacs-mpinat-20261003/runtime-qualification-r1/pme-controls-01`.
`fixtures/mpi{1,2}-{cpu,gpu}` are immutable named requests/bundles. Corresponding
run directories retain native help, exact image/Pod/GPU identity, full native
logs, results, finite-state validation and cleanup. `references/` retains the
downloaded official documentation and exact source bytes. The report generator
rehashes inventories, verifies native PME dispatch and original numerical
settings, and compares only identical engine/TPR controls:

```text
python3 -m unittest -v test_pme_control.py test_qualify_candidate.py test_candidate_report.py
python3 pme_control_report.py --root /path/to/pme-controls-01 --output /new/summary.json
```

The 25 focused tests include changed-cutoff, wrong-dispatch, stale reset,
unsupported shape, output-inventory generation, and image-identity negatives.

## Measured fixed-input result

All four controls passed on the exact external-MPI candidate
`sha256:c6c353e55deade8c8fe7a2f02e68bcdb639c7680c8435b665e72815d0213c9ea`,
L40S driver `580.173.02`. One control at a time used at most two GPUs. Native
inventories, all 12 final 81,743-atom coordinate sets/cells and checkpoints were
validated. Every run reached 10,000 steps / 20 ps. Original and finite TPR hashes
are identical across all controls; native logs confirm the intended cutoff,
grid, task placement and no PME tuning. All owned Pods were removed; final
task-label query returned no remaining Pods at 13:20 UTC.

| Ranks / PME | Native ns/day repetitions | Mean ± sample SD |
| --- | --- | --- |
| 1 / CPU | 33.188, 32.180, 32.595 | 32.654 ± 0.507 |
| 1 / GPU | 195.398, 194.926, 194.717 | 195.014 ± 0.349 |
| 2 / CPU | 23.889, 24.116, 36.518 | 28.174 ± 7.227 |
| 2 / GPU | 76.644, 76.488, 75.033 | 76.055 ± 0.889 |

Within fixed rank counts, ratios of mean GPU-PME to CPU-PME rates are **5.972×**
and **2.699×**. The high two-rank CPU variation is retained; these are not
confidence intervals. GPU PME is beneficial in these controls, but the tested
two-GPU layout is slower than one GPU for this MEM input. Do not market it as
positive strong scaling or extrapolate it to larger systems/topologies. These
native rates also exclude API queueing and full operation/artifact latency.

Exact receipt: `pme-controls-01/summary-01.json`, SHA256
`49c9c430f26103c33dbc409c09a7f779962a62e794846f9f096bf22f145b2739`.
Original TPR SHA256:
`5099268bf3a3d948c03b3b78432f29a2c5d4207b54d02e7e294f8138b587d473`.
Finite TPR SHA256:
`4a0d845ae01b8c95b1077cd099c5769fce99d939844e24d21f1d1d90752540b9`.
The exact Open MPI/UCX/query observations are in `mpi2-gpu/{ompi-info.txt,
ucx-info.txt,mpi-cuda-query.txt}`; the latter preserves both its successful
initialized query (`0`) and original PMIx warning bytes.

## Additive build investigation

A later search found the retained original build-stage image in the separate
`gromacs-engine` repository (`sha256:7e531c7b…`), resolving the earlier missing-log
limitation without altering these measurements. Its configure log proves that
the automatic CUDA-library search joined the compat and stub directories into a
malformed linker argument. The failed symbol check silently set CUDA support to
zero. The existing GROMACS binary already imports the runtime MPI CUDA query.
The separate [candidate recipe](../../models/molecular-dynamics/gromacs/runtime/cuda-aware/README.md)
pins that build image and replaces only same-version Open MPI with an explicit
single stub directory plus positive build gates. This is a new, unpromoted image;
the functional measurements above remain bound to `c6c353e5…`.
