# Lynx exact-input performance findings — 2026-10-05

For this membrane system, retain one L40S and eight vCPUs, offload bonded work
to the GPU, and use neighbour-list interval 200 with the original positive
Verlet-buffer tolerance. The longer public confirmations delivered **201.0
ns/day via REST and 196.6 ns/day via raw MCP**, versus native-counter medians
220.7 and 216.6 ns/day. This is the cost-efficient tested recipe. One-node eight
H100s is a separate latency option; sixteen H100s over the existing TCP network
were substantially slower and are not recommended from these measurements.

This is a fixed **pre-InfiniBand** milestone, cutoff
`2026-10-05T16:59:55.274732+00:00`. Subsequent infrastructure or runtime changes
do not retroactively qualify these receipts. The running customer operation,
its key, bucket, limits and frozen runtime were not changed by this experiment.

## Exact science and protocols

Original private TPR SHA256:
`e2ee73571f0dd9855709d2a957e41e5ad52316b3f1d2b1808e65476f4bd8ef10`.
It contains 185,486 atoms, a CHARMM membrane, 310 K, 2 fs, PME and force-switch.
Finite copies change only the recorded step bound. Force field, physical
cutoffs, PME accuracy, constraints, state, and original energy/log/trajectory
cadence remain unchanged. List tuning retains the original 0.005 Verlet
tolerance and native automatic buffer adjustment. The original compressed
trajectory interval is 500,000 steps (1 ns).

The longer single-GPU recipe uses `-nb gpu -bonded gpu -pme auto -update auto
-pin auto -nstlist 200`, eight OpenMP threads, and three 500,000-step repeats.
Public runs retain five-minute segments and real checkpoint/export handling.
Each repeat restarts the same initial state: these are timing replicates, not
independent scientific ensembles or proof of convergence. Pinning was disabled
by the container CPU quota in the inspected native logs.

Multi-rank cases use CPU update because this TPR's nonconsecutive update groups
cannot use domain-decomposed GPU update. They use bonded GPU, fixed GPU PME,
`-notunepme`, one PME rank when multi-rank, and no multi-GPU FFT. They must not
be pooled with the auto-tuned GPU-update single-GPU protocol. The native engine
dispatch logs are authoritative; the current [GROMACS performance guide](https://manual.gromacs.org/2026.2/user-guide/mdrun-performance.html)
supports measuring these workload-dependent placement/list choices, not assuming
that all GPUs or rank counts benefit equally.

## Screens and longer native confirmation

Short screen medians, three 50,000-step repeats, native-counter ns/day:

| Single GPU, eight requested vCPUs | L40S | H100 |
| --- | ---: | ---: |
| Auto offload control | 104.93 | 114.11 |
| Bonded GPU | 191.85 | 183.31 |
| Bonded GPU, list interval 200 | 213.88 | 196.88 |

The L40S list screen partly overlapped a peer on another GPU of the same host;
the longer confirmations below are stronger evidence than that short median.
The separate matched direct-CLI CPU8/CPU16 warm medians were 192.13/190.81
ns/day: doubling CPU allocation did not help. Four threads gave 172.83 ns/day
while still reserving eight vCPUs. Explicit affinity did not become active.
There is no measured justification for a larger public CPU envelope or an
affinity default change.

H100 MPI 1/2/4-rank short medians were 122.32/101.11/204.33 ns/day. Adding the
second rank was not beneficial. Longer native three-by-1ns confirmations gave:

| Native configuration | Median ns/day | Repeat range | Checkpoint scope |
| --- | ---: | ---: | --- |
| One H100, tuned GPU update | 206.88 | 206.65–209.22 | Local; 60-minute segment cap |
| Eight H100s, 7 PP + 1 PME, CPU update | 318.53 | 295.87–326.44 | Local; 60-minute segment cap |

The eight-GPU result is faster per trajectory, not eight times faster or more
efficient per allocated GPU. Its hardware, update protocol and checkpoint path
differ from the public L40S result; no matched single-factor speedup is claimed.
These finite confirmations are not a fourteen-day unattended qualification.

## Actual public completion and allocation cost

Every selected operation succeeded on one attempt, passed idempotency and
download-hash checks, and has its admitted plan retained. All 18 logical repeats
passed finite energy, 185,486-atom coordinate and native checkpoint continuity
checks. Long runs preserve both segments per repeat and combine all trajectory
parts for native validation. **Zero repeated durable steps** were found.

“Delivered” below is validated simulated time divided by server accepted-to-
completed wall time, including queue/setup/checkpoint/analysis/export work in
that interval. It excludes input upload and subsequent client download. Native
counters include initialization/tuning and are not pure GPU compute. MCP means
the raw hosted MCP interface, not an actual agent or LibreChat session.

| Case / operation prefix | Allocated GPUs | Work | Native median ns/day | Delivered ns/day | Scheduler GPU-s | Allocation-share USD |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| REST MPI 1×1 `6f536637` | 1 L40S | 3×0.1 ns | 107.48 | 81.08 | 311 | 0.1973 |
| MCP MPI 1×8 `6451d349` | 8 H100 | 3×0.1 ns | 304.69 | 100.47 | 1,992 | 2.4900 |
| REST MPI 2×8 `9ac94bdc` | 16 H100 | 3×0.1 ns | 55.78 | 44.21 | 9,192 | 11.4900 |
| REST tuned single `66546959` | 1 L40S | 3×1 ns | 220.69 | 200.98 | 1,281 | 0.8129 |
| MCP tuned single `56395169` | 1 L40S | 3×1 ns | 216.61 | 196.58 | 1,312 | 0.8325 |
| REST MPI 2×8, two threads/rank `26fdc0dd` | 16 H100 | 3×0.1 ns | 58.01 | 44.81 | 9,080 | 11.3500 |

The long REST/MCP cases achieved **215.73/211.01 million atom-steps/s** over
their server wall clocks (1289.680/1318.531 s). Durable allocation efficiency
was 8.43/8.23 simulated ns per GPU-hour, at $0.2710/$0.2775 per useful ns.
The short 8-GPU and 16-GPU startup/export costs must not be extrapolated as
steady-state long-trajectory costs. The earlier customer-observed ~105.5
effective ns/day is useful context, not a contemporaneous matched control.

Costs use the dated 2026-10-05 [official compute rates](https://docs.nebius.com/compute/resources/pricing)
and actual [VM presets](https://docs.nebius.com/compute/virtual-machines/types):
$4.50/H100-hour and a one-quarter allocation share of the L40S AMD
4GPU/128vCPU/768GiB preset ($9.1376/node-hour, $2.2844/GPU-hour). This is an
on-demand list-price scenario, **not** the actual reservation invoice. It
excludes storage, network, tax, discounts and idle VM time outside Pod allocation.
The six public cases total $27.17275; native probes are outside this selected
public accounting total, not free. Each allocation is charged once, including
the full multi-node multiplier. The six attempts contain no native retry waste;
observer/coordination failures are retained separately below.

## Why the current sixteen-GPU case loses

Both full nodes had NVLink within the node but no provider GPU-cluster
attachment, only the VPC interface, and no allocatable RDMA resource. The actual
launcher retained `FS2_GROMACS_MPI_TRANSPORT=tcp-host-staged`,
`OMPI_MCA_pml=ob1`, `OMPI_MCA_btl=self,sm,tcp`, and
`GMX_DISABLE_DIRECT_GPU_COMM=1`. This is a missing attachment/runtime path on
these existing nodes, not evidence that the region lacks RDMA capacity.

The first 2×8 native segment took 150.990 s. Rank-weighted host timing categories
included send-X-to-PME 9.2%, coordinate communication 15.5%, wait/communicate
forces 18.0%, wait/receive PME forces 22.6%, and constraints 19.3%; force and
PME GPU mesh categories were only 0.7% and 0.5%. These are rank-weighted native
host accounting categories, **not** measured network-only wall or
GPU kernel time. They support a communication/wait-heavy diagnosis, not a
claim that the network is the sole cause. Initial peer input staging was only
8,996,516 bytes in 0.373 s.

Reducing OpenMP threads per rank from eight to two did not rescue performance.
The bounded variant also used a newer wrapper and opposite coordinator node,
so its 58.01 versus 55.78 ns/day is not a clean single-factor gain. GPU means
remained only about 3–6% over sampled whole-Pod intervals. Current provider
[GPU-cluster documentation](https://docs.nebius.com/kubernetes/gpu/clusters)
describes the attachment required for InfiniBand; reprovisioning and actual
Pod/UCX qualification are a separately authorized follow-up, not part of this
frozen result.

## Measured phases, utilization and I/O limits

The existing lifecycle ledger and bounded PostgreSQL latest rollups provide
the allocation clock, with `reconciled/application_observed` quality. All six
retain `trace_context_missing`; the short 8-GPU case also has incomplete phase
classification. Generic top-level reserved-GPU zero is not a cost measurement.
Sparse observer bounds are retained but are not substituted for the durable
scheduler clock when their lower bounds are loose.

Long REST/MCP native-counter wall sums were 1175.385/1199.810 s; measured
analysis-command process wall was 0.388/0.387 s and init-container reservation
was five GPU-seconds each. The 2×8 baseline native-counter wall sum was 460.975
s, corresponding to 7375.6 reserved GPU-seconds across sixteen GPUs, not pure
GPU compute. These phase clocks overlap or have different boundaries and are
not a complete partition. Pure native initialization, checkpoint publication,
export-only and integration-only durations remain **null**. No residual wall
time is relabelled as a measured phase.

Long REST/MCP whole-container samples averaged 7.40/7.41 CPU cores, GPU activity
81.9/81.8%, and power 298/305 W. These irregular means include preparation and
analysis; they are not an energy integral or a native-only average. Cgroup
throttled-period fractions 0.685/0.571 count affected periods, not percentage
work lost; the CPU16 experiment did not improve throughput.

The compressed input bundle is 4,378,022 bytes. Retained remote object
inventories for long REST/MCP total 93,629,264/93,625,961 bytes, distinct from
SDK artifact inventories and from actual network-transfer totals. Total bytes
transferred across repeated checkpoint/export publications remain unknown.
Checkpoint final generation19 and exact nonrepeated durable intervals are
verified for each long public run.

## Runtime identity, evidence and adoption boundary

Short MPI cases used worker `938cc6124eaf7840965bebd24b9ae04014e98651a4260f39266a29c8131c202b`.
Long single confirmations used
`5acd77d66257593896c2fe09cac15392adfffaf00fc7f7625d228a6c8d6084dd`.
Long native1×8 and final public2×8 used
`fc28fa44489a93a6c73ccb0852e3970025cda1dda65a97d430f8076f2d2a3a92`.
All are immutable `fs2-platform/gromacs` images. Full registry paths, actual
image IDs, node/resource claims and companion digests are in the frozen plans.
The long REST/MCP companions differ (`ec7ff683…`/`6f0703fa…`); API rollouts do
not retroactively change an admitted engine or companion. Normalized long
parameters excluding only output prefix have matching SHA256
`e3b5cd5bf648a7804c451903a877b213ee81e8f9d09ef4b4e262e91725bca1ac`.

Private evidence root:
`/home/tux/secure-handoff/fs2-lynx-performance-20261005/benchmark-findings-r2`.
The existing [collector](collect_report.py) reuses the MPINAT ledger/cost code;
no second accounting model or public-benchmark comparator is invented for this
private input. Independent validation rehashed 1,021 unique files, including
274 public artifacts and 481 native inventory files: six successes, six
attempts, eighteen repeats, 7.2 ns, fifteen native probe cases, six terminal
rollups, SQLite integrity OK and zero foreign-key errors.

- `report.json` SHA256 `bb74b7a0387d0cef53ecce73af9471c081538c0882df416c91f1f756099224a2`.
- `durable-lifecycle.json` SHA256 `e8679017bda9402e34d25be3bfb75e75cb0bf5755409cd7fe5ecbc66472cce93`.
- `independent-validation.json` binds this cutoff and those exact files.

The partial r1 report/CSV-projection failure remains immutable. The adapter
fix leaves `public_comparisons=[]`; it does not fabricate a published speedup.
An initial independent-validator path error and pre-admission slot/origin
checks are retained as local evidence failures, not simulations or extra
charges. The short 1×8 Kueue object expired before capture; its frozen plan and
actual eight-GPU Pod remain, while both 2×8 cases have exact Kueue16/Pod8+8 proof.
Task Pods were UID-checked, deleted and confirmed absent; both task observers
exited naturally after STOP markers. Original molecular outputs stay private.

The reusable request recipe works on the actual hosted REST/MCP release, but
no customer default was silently changed. Longer continuation/restart recovery
belongs to its separate qualification. [MPS ensemble results](../lynx-mps-20261005/README.md)
are also separate: aggregate independent-process throughput cannot be compared
directly with this single-trajectory native or server-delivered clock.
