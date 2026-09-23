# Molecular-dynamics Apps

GROMACS, NAMD, AMBER and LAMMPS use the Scientific AI platform's existing durable
scientific-workflow API, queues, model grants, operation tracking and customer
Object Storage. They are shared Apps, not a new deployment per tenant. Native
input files and explicit typed stages remain the scientific source of truth.

## Runtime and evidence map

| App | Selected runtime | Evidence and important limits |
|---|---|---|
| GROMACS | NVIDIA-distributed GROMACS 2026.2-dev, CUDA 13, PLUMED integration | [Runtime](gromacs/README.md), [extended workflows](gromacs/qualification/P2-20260923.md), [matched L40S optimization](gromacs/qualification/SM89-20260923.md). Native-SM89 compilation alone did not establish a useful improvement; the qualified NGC runtime remains selected. |
| NAMD | NVIDIA NGC NAMD 3.0.2, CUDA 12.9; exact-r5 wrapper | [Qualification](namd/qualification/REPORT.md). Resident/offload and Colvars history are distinct modes; unsupported GBIS/spinAngle combinations are not advertised as working. Native multi-node capability is not inferred from this single-node container. |
| AMBER | Private academic PMEMD 26 plus AmberTools 26; exact-v3 wrapper | [Runtime and tools](amber/README.md). Not an AMBER NIM or an AmberTools replacement for PMEMD. SPFP/DPFP are explicit. Broad TI/convergence claims remain excluded; retained strict upstream SPFP failure and distant-lambda overflow are documented. |
| LAMMPS | NVIDIA NGC 22 Jul 2025, CUDA Kokkos + Serial | [Native campaign](lammps/qualification/README.md). Six potentials/styles have repeated H100/L40S evidence; that is not qualification of every compiled style. Native H100/compatible L40S and KISS FFT details are explicit. |

H100 and L40S qualification does not establish compatibility or performance on
an untested GPU, architecture, driver or changed image. Published NVIDIA HPC
performance numbers have different systems/GPU counts; they are context, not a
matched speedup baseline for these single-GPU, output-enabled workflows.

## Canonical four-engine acceptance

[The comparison case](comparison/README.md) implements one AmberTools-built
ACE–ALA–NME ff14SB/TIP3P master: minimization, 100 ps NVT, 100 ps NPT and 1 ns
production, with 2 fs integration and 1 ps output. Its topology audit checks
actual parameters, exclusions and Amber 1–4 factors before native dynamics.
Small engine-specific numerical and kinetic-estimator differences are measured,
not hidden by changing charges or replacing observed values with target values.

[Hosted qualification records](qualification/hosted-two-cohort-226/README.md)
distinguish ordinary-key execution from native-only tests and later read-only
artifact recovery. The immutable runtime/release and precise input variants
matter: earlier NAMD automatic-PME and GROMACS autotuned-PME runs are historical,
not silently relabeled as the final fixed-grid/fixed-cutoff case.

At this document's initial publication, final LAMMPS production and the final
four-way report/video remain in progress. Do not use source or image presence
as a completion claim; the final case inventory and scientific receipts are the
authority. The acceptance case is deliberately short and does not prove ensemble
convergence, correct force-field selection for another molecule, or free energy.

## Continuation and GPU process snapshots

Native closed-workflow checkpoints and process-memory snapshots are different
features. Closed generations preserve native coordinate/velocity/cell, topology,
configuration, logs and supported bias state. A retry may replay an uncommitted
segment; stochastic state is not universally serializable across engines.

[Exact snapshot observations](qualification/GPU-SNAPSHOT-20260923.md): one
GROMACS fresh-Pod persistent continuation passed, without established net
end-to-end benefit. NAMD capture passed but CRIU restore failed on an open
thread-stat descriptor. LAMMPS persistent dumping was blocked by GDRCopy.
AMBER GPU-process restore is unqualified. Those modes are not enabled as a
blanket production acceleration feature. Native checkpoint recovery is the
supported fallback with separate evidence.

These stateful snapshots are specific to a workflow's molecule and simulation
state. They cannot serve as generic cached model weights or generate independent
replicas merely by copying a captured state.

## Storage, client and operational boundaries

Each workflow requests its existing customer-bucket destination and a relative
output prefix. The worker uses local scratch; a companion publishes complete,
hash-inventoried closed generations. Native traces are not written through an
unqualified in-process S3 filesystem mount. Preserve manifests and every native
trajectory/restart part. Do not silently reduce output cadence to meet a quota.

The portable client skills live in `rene-tech/serverless-ai-cookbook`, under
`skills/scientific-ai/{gromacs,namd,amber,lammps,scientific-batch}`. The workbench
keeps its API helper and CPU MD-analysis environments separate. It must discover
the actual granted App and schema; an installed skill is not access entitlement.

Timing reports separate native production throughput, queueing, setup, checkpoint
export, retained GPU allocation and end-to-end completion. Existing lifecycle
rollups are application observations, not hardware-busy measurements or bills;
missing trace context and phase-classification gaps remain visible. Failed
native logs remain downloadable after worker teardown without converting failed
trajectories into successful results.
