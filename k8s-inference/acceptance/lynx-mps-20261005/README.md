# Matched MPS throughput experiment — 2026-10-05

This is an extension of the exact Lynx GROMACS performance qualification, not
an enabled customer GPU-sharing mode. Parent Task Deck card:
`fs2-lynx-gromacs-longrun-performance-r20261005`; child:
`fs2-gromacs-mps-throughput-r20261005`.

## Research translated into a test

The requested [NVIDIA MPS/MIG article](https://developer.nvidia.com/blog/maximizing-gromacs-throughput-with-multiple-simulations-per-gpu-using-mps-and-mig/)
uses A100 and GROMACS 2021.2 on smaller systems. Its main opportunity is
overlapping independent simulations to improve **aggregate throughput**, not
making one trajectory finish sooner. Its numeric speedups are not H100/L40S
predictions. CPU budget, GPU-resident execution and actual molecular system
size affect whether overlap helps. The tested Lynx system has 185,486 atoms.

Current [MPS documentation](https://docs.nvidia.com/deploy/mps/latest/index.html)
describes private control/pipe directories and client inspection. The test
uses the installed controller, same UID as the GROMACS processes, and verifies
their actual PIDs in the MPS server's client list. Merely starting a daemon is
not evidence that GROMACS used it.

[NVIDIA's supported-MIG list](https://docs.nvidia.com/datacenter/tesla/mig-user-guide/supported-gpus.html)
includes H100, not L40S. No host was drained or repartitioned for this work.
MIG is hardware partitioning; MPS shares execution resources; MPI distributes
one simulation across devices. These are not interchangeable optimizations.

## Exact experiment

- One exclusively allocated GPU, fixed total eight requested/limited vCPUs and
  16 GiB RAM. One, two or four independent GROMACS processes get eight, four or
  two OpenMP threads respectively. No host-wide MPS/device-plugin change.
- MPS off/on, three alternating-order repetitions, 50,000 steps (100 ps) per
  process. This is a performance screen; any adopted winner needs longer runs
  and actual hosted scheduling/recovery/accounting qualification.
- Same finite TPR derived from the exact original SHA256
  `e2ee73571f0dd9855709d2a957e41e5ad52316b3f1d2b1808e65476f4bd8ef10`.
  Scientific protocol, original output cadence and Verlet tolerance remain
  unchanged. CLI uses bonded GPU and neighbour-list interval 200, as screened
  separately by the main performance task. CPU pinning is explicitly off for
  all compared cohorts; the Pod CPU quota is fixed.
- Identical input replicas measure timing, **not independent scientific
  ensemble sampling**. Independent outputs, energy/checkpoint validation and
  content hashes are retained for every process. No forced timing-counter reset.
- Report native ns/day per simulation, wall time per simulation, aggregate
  process-wall ns/day, cgroup CPU, GPU utilization/memory/power and exact runtime.
  Aggregate process-wall throughput includes CUDA initialization and native
  output, but excludes external object export and subsequent analysis.
- Runtime: existing single-GPU worker `sha256:5acd77d66257593896c2fe09cac15392adfffaf00fc7f7625d228a6c8d6084dd`.
  Scripts themselves are independently hashed in each receipt.

The launcher checks fresh free capacity before creating a uniquely owned Pod,
records its actual image/UID and deletes only that exact Pod after evidence
capture. Customer jobs and credentials are untouched; operator experiments
are labelled `system`. There is no new cloud instance or bucket.

## In-progress run

Project `project-e00rene`, region `eu-north1`, namespace `fs2-models`, context
`nebius-mk8s-k8s-inference-h100-e00j5z9te7x5dd9g6a`:

| GPU | Existing node | Task Pod |
| --- | --- | --- |
| H100 80 GB | `computeinstance-e00cjp2bfywsenpzzv` | `fs2-lynx-perf-cpu-mps-h100-r1` |
| L40S | `computeinstance-e00r165maajnbg7wrr` | `fs2-lynx-perf-cpu-mps-l40s-r1` |

Both observed driver `580.173.02`, default compute mode, one visible GPU.
Full immutable input/runtime/shape/ownership records and outputs are private:
`/home/tux/secure-handoff/fs2-lynx-mps-20261005/{h100,l40s}-r1/`.
Early one-process off/on cohorts passed, including observed MPS client PIDs.
Multi-process repetitions and final recommendation remain pending.

## Adoption decision

Do not expose fractional GPU claims merely because the native experiment
improves throughput. The existing platform owner must know the measured
per-worker capacity, isolate requests/workspaces, queue fairly, account for
the shared physical reservation once, and recover each trajectory correctly.
An ensemble workflow within one admitted whole-GPU job is a narrower possible
first application. The existing whole-GPU REST/MCP contract remains unchanged.
