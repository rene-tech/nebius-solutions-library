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

## Completed native experiment

Project `project-e00rene`, region `eu-north1`, namespace `fs2-models`, context
`nebius-mk8s-k8s-inference-h100-e00j5z9te7x5dd9g6a`:

| GPU | Existing node | Task Pod |
| --- | --- | --- |
| H100 80 GB | `computeinstance-e00cjp2bfywsenpzzv` | `fs2-lynx-perf-cpu-mps-h100-r1` |
| L40S | `computeinstance-e00r165maajnbg7wrr` | `fs2-lynx-perf-cpu-mps-l40s-r1` |

Both observed driver `580.173.02`, default compute mode, one visible GPU.
Full immutable input/runtime/shape/ownership records and outputs are private:
`/home/tux/secure-handoff/fs2-lynx-mps-20261005/{h100,l40s}-r1/`.
Both runs finished all 18 cohorts, 42 native trajectories per GPU. Every native
validation passed, and the reporting pass independently verified the sizes and
SHA-256 hashes of **462 native output files per GPU**. The exact task-owned
Pods were deleted and their absence observed at 16:58:13 (H100) and 16:59:34
(L40S) UTC. No customer Pod was deleted or moved.

The H100 launcher's first result-copy command failed after the native process
exited successfully. Its exception handler retained a complete fallback copy.
`report.py --recover-export` explicitly verifies its input/harness hashes, every
cohort receipt and all native output hashes. The original failed launcher
receipt is **not** changed to success. This is successful native measurement
with a recovered observer-export failure, not an error-free end-to-end run.
The L40S launcher completed its first export successfully. Future launches now
retain transfer errors and make at most three attempts to distinct destinations.

The existing node groups were `mk8snodegroup-e00pcb1vvq65bgvxfk` (H100) and
`mk8snodegroup-e00dkjxp9gf3gd7yhf` (L40S), with presets `1gpu-16vcpu-200gb`
and `1gpu-16vcpu-64gb`. Both reported reservation policy `FORBID`. Preemption
fields were omitted from the inspected provider responses; these receipts do
not establish preemptible status. No new node was provisioned for this test.

## Results and recommendation

Median aggregate **process-wall** throughput, ns/day, across three repetitions:

| Processes per whole GPU | MPS | H100 | L40S |
| --- | --- | ---: | ---: |
| 1 | Off | 172.61 | 177.95 |
| 2 | Off | 172.62 | 190.74 |
| 4 | Off | 166.38 | 162.84 |
| 1 | On | 155.53 | 158.29 |
| 2 | On | 206.07 | 202.17 |
| 4 | On | 218.51 | 190.26 |

- H100: four MPS clients improved the median aggregate rate **26.6%** against
  one process without MPS. Cohort rates ranged 214.36–222.76 ns/day, versus
  156.92–174.35 for the baseline. Individual 100 ps simulations became slower:
  median wall time **154.64 s**, versus **50.05 s**. Two clients are a useful
  lower-latency compromise, with a 19.4% aggregate gain.
- L40S: two MPS clients improved the median aggregate rate **13.6%**, but the
  measured range 165.16–204.67 ns/day overlaps the single-process baseline.
  Median per-simulation wall time rose from **48.55 s to 85.47 s**. Compared
  specifically with two non-MPS clients, the median gain was only 6.0%.
  Longer repeats are needed before calling this a dependable capacity increase.
- One MPS client was slower than the non-MPS baseline on both GPUs. MPS is
  therefore **not** an acceleration switch for Lynx's single ongoing trajectory.
  No production default, customer job or public resource shape was changed.
- H100 four-client MPS is the stronger candidate for future ensemble/batch
  qualification. L40S two-client MPS remains provisional. These short cohorts
  are not a substitute for the separate 1 ns single/MPI confirmation runs, and
  neither rate includes API queueing, object export or final delivery.

Rebuild the evidence-backed report without overwriting existing receipts:

```bash
python3 k8s-inference/acceptance/lynx-mps-20261005/report.py \
  --recover-export --output /new/private/path/mps-report.json \
  /home/tux/secure-handoff/fs2-lynx-mps-20261005/h100-r1 \
  /home/tux/secure-handoff/fs2-lynx-mps-20261005/l40s-r1
```

Final private report: `report-verified-r2.json` in that experiment directory.
It also retains exact inputs, image IDs, commands, per-process native ns/day,
CPU consumption, 500 ms GPU utilization/memory/power samples, cleanup and the
observer failure. Missing measurements remain unknown. Twenty-four focused
tests and Ruff passed, including corrupt/mismatched evidence rejection and
bounded copy retry behavior. Original live script hashes remain in receipts;
later harness/report changes do not retroactively change those executed bytes.

## Adoption decision

Do not expose fractional GPU claims merely because the native experiment
improves throughput. The existing platform owner must know the measured
per-worker capacity, isolate requests/workspaces, queue fairly, account for
the shared physical reservation once, and recover each trajectory correctly.
An ensemble workflow within one admitted whole-GPU job is a narrower possible
first application. The existing whole-GPU REST/MCP contract remains unchanged.
