# MPINAT and Lynx benchmark findings — 3 October 2026

Milestone snapshot generated **14:01:36 UTC**; this is not a live dashboard or
a completed release qualification. No new GPU work, admission-policy change,
customer credential or customer workload was used to prepare this report.

The retained evidence contains complete corrected REST results for **23/24
MPINAT inputs** and six complete external-MPI cases: REST and raw MCP, each at
1×1, 1×2 and 2×1 GPUs. PEP remains running in the selected snapshot. These are
short throughput/transport measurements, not scientific convergence, maximum
platform capacity, an agent-MD pass, or an eight-/sixteen-GPU scaling result.

## Evidence and exact scope

Private root: `/home/tux/secure-handoff/fs2-gromacs-mpinat-20261003/benchmark-findings-r1`.
`report.json` SHA-256:
`c6cdfb20fc085692076edbf7c675ddc516a1a54babc38b534d946a567d691c81`.
It indexes 53 operations/attempts, 373 checkpoints, 373 commands including 116
`mdrun` records, and 48 observed Pod allocations. All 53 selected saved receipts
are indexed; unsuccessful records remain present. `operations.csv`,
`table-data.json`, the copied metadata and `refresh-evidence.json` retain the
numeric rows, full operation/input/image identities, source hashes and cutoff.
The earlier 44-operation and phase-accounting ledgers were not modified.

New recovery covered MPI cohorts 5/6/7/9 and PEP-h. Each MPI checkpoint inventory
had ten manifests and 18 hash-verified text files, with no native errors; their
36 delivered artifacts per case were independently rehashed from retained
files. PEP-h's public read-only collection verified **38 artifacts,
7,585,965,204 bytes and all three native timing rows**. Its checkpoint recovery
verified ten manifests and 17 text files. Nine copied admitted plans were
captured/checked with the existing bounded, read-only PostgreSQL helper. Active
campaign files were not edited. Large pre-existing artifacts were hard-linked
only after hash checks, not duplicated into a new trajectory corpus.

All benchmark inputs are the publisher's downloaded TPR bytes, with full hashes
in the retained fixture provenance; `convert-tpr -nsteps 10000` derives a finite
execution input. Each operation performs three separate starts from that same
finite TPR. These are **timing repetitions, not independent scientific replicas**.
All completed rows below have three successful 10,000-step commands and validated
native/output evidence. Three repeats sum to 0.06 simulated ns, or 0.12 ns for
RIB's 4 fs timestep; they do not create one continuous trajectory of that length.

Runtime identities are deliberately not pooled into a single release verdict:

| Rows | GROMACS worker image SHA-256 | Scope |
| --- | --- | --- |
| REST-B/C | `14ffdae0f0389c7771bae8791c56a5e21736ece630bd11dfb0f3b6a78f8cd643` | Earlier single-GPU image; native GROMACS 2026.2-dev, CUDA 13.0 |
| PEP-h REST-D | `dc5d908c64503c4c4cdc3bede10987d49a9acdde5ef2f4f1d0e61f0628739f93` | New single-GPU release |
| Six MPI cases | `c6c353e55deade8c8fe7a2f02e68bcdb639c7680c8435b665e72815d0213c9ea` | External MPI; native 2026.2-dev-20260505-da9e013-local, CUDA 13.0 |

Worker repository is `cr.eu-north1.nebius.cloud/e00akg9ndpx77eaexh/fs2-platform/gromacs`.
The corrected hosted MPI admission path used API digest
`368a020e0789f3b2f28d16095f8f5430117388722fdc2a8bee7c9f3532a9bbe9`;
its frozen collector digest is separately retained as `d789a4b7…fed68`.
Earlier single-GPU results do not qualify the changed release merely because
the underlying engine family is similar. The older L40S logs retain an sm_89
PTX/JIT warning; successful timing is not a warning-free readiness claim.

## What the public comparison can establish

The primary [MPINAT benchmark page](https://www.mpinat.mpg.de/grubmueller/bench)
provides the input suite under CC-BY-4.0. Attribution: Department of Theoretical
and Computational Biophysics, Max Planck Institute for Multidisciplinary
Sciences, Göttingen. The [publisher's PDF](https://www.mpinat.mpg.de/632182/bench.pdf),
page 3, Tables 9–10, supplies the twelve historical numbers below: one GTX 1080,
Intel E3-1240v6, four cores at 3.7 GHz, GROMACS 2018 and CUDA 8.0. The PDF does
not provide numerical standard-MD results for MEM/RIB/PEP/PEP-h or the eight
CMET/HIF2A/SHP2 fixtures. No substitute baseline is invented for them.

The twelve historical rows match our observed particle count and 2 fs timestep,
but the PDF does not bind each result to the current TPR hash or fully specify
its timing protocol. Its suggested commands reset timers; our corrected runs
do **not** reset and include tuning/warmup. Hardware, engine, offload and shared
node conditions also differ. **No matched speedup is claimed.** RIB is an
additional input-version warning: our downloaded/native input has 2,037,824
particles, whereas the PDF lists 2,136,412; the name alone cannot prove identity.

Hardware labels below are actual observed placement, not requested preferences:
H100 = `gpu-h100-sxm`; L40S-I = Intel `gpu-l40s-a`; L40S-A = AMD `gpu-l40s-d`.
Workers use eight OpenMP threads per rank. H100, L40S-I and L40S-A CPU families
are Sapphire Rapids, Ice Lake and Genoa respectively, with VM presets and their
GPU/CPU/RAM sizes defined by the [official platform documentation](https://docs.nebius.com/compute/virtual-machines/types).
Nodes were shared, not isolated performance hosts.

### Historical numerical rows versus our corrected REST-B measurements

Each ours column is native ns/day **median (minimum–maximum)**, n=3. S* has 3,363
particles; B* has 43,952. All use 2 fs. M particle-steps/s derives from that
median rate, not a GPU-utilization counter. These systems have different
free-energy evaluation work, so particle-steps alone is not a cross-system
measure of computational difficulty.

| Input | Actual GPU | Ours ns/day, n=3 | Historical GTX 1080 ns/day | Ours M particle-steps/s |
| --- | --- | ---: | ---: | ---: |
| SFI | L40S-A | 64.681 (63.268–65.122) | 6.7 | 1.259 |
| SFC | H100 | 470.054 (469.622–506.457) | 85.1 | 9.148 |
| STI | L40S-I | 481.238 (324.376–489.534) | 87.6 | 9.366 |
| STC | L40S-A | 404.199 (400.592–410.149) | 94.2 | 7.866 |
| SNI | L40S-I | 1040.067 (1000.963–1056.757) | 701.0 | 20.242 |
| SNC | H100 | 1088.191 (1077.843–1091.470) | 912.1 | 21.178 |
| BFI | H100 | 37.341 (36.811–39.765) | 4.4 | 9.498 |
| BFC | H100 | 135.464 (125.418–137.916) | 22.4 | 34.456 |
| BTI | L40S-A | 113.294 (109.626–116.866) | 21.2 | 28.817 |
| BTC | H100 | 133.510 (128.328–152.298) | 22.7 | 33.959 |
| BNI | L40S-I | 133.525 (118.305–133.901) | 74.7 | 33.962 |
| BNC | L40S-A | 171.573 (170.035–196.854) | 95.9 | 43.640 |

Historical values are from the main GPU rows of the
[primary PDF tables](https://www.mpinat.mpg.de/632182/bench.pdf), not its separate
CPU-PME alternatives. There is no averaging across reset-policy or hardware
variants. Three short repeats provide a range, not a confidence interval or a
sustainable capacity measurement.

### Completed inputs without a numerical baseline in that PDF

All use one GPU and 2 fs except RIB (4 fs). MEM/RIB are REST-C, PEP-h REST-D;
the other eight are REST-B. USD ranges are entire three-repeat operation
allocation-share estimates defined below, not native-compute-only costs.

| Input | Native particles | Actual GPU | Native ns/day median (range) | M particle-steps/s | Operation USD |
| --- | ---: | --- | ---: | ---: | ---: |
| MEM | 81,743 | L40S-A | 97.612 (82.550–114.505) | 46.175 | 0.072–0.079 |
| RIB | 2,037,824 | L40S-A | 18.937 (17.460–19.097) | 111.662 | 0.502–0.511 |
| PEP-h | 12,495,503 | L40S-A | 2.959 (2.929–2.989) | 213.971 | 1.387–1.406 |
| CMET-eq | 67,291 | L40S-I | 93.859 (84.090–94.695) | 36.550 | 0.067–0.073 |
| CMET-ti | 67,291 | L40S-I | 85.932 (85.268–87.862) | 33.463 | 0.003–0.071* |
| HIF2A-eq | 35,546 | L40S-A | 147.544 (145.175–148.672) | 30.351 | 0.072–0.082 |
| HIF2A-ti | 35,546 | H100 | 143.058 (135.158–156.455) | 29.428 | 0.145–0.159 |
| Ligand-CMET-eq | 6,443 | H100 | 368.660 (362.318–389.182) | 13.746 | 0.091–0.109 |
| Ligand-CMET-ti | 6,443 | H100 | 359.218 (346.030–379.642) | 13.394 | 0.087–0.105 |
| SHP2-eq | 107,326 | L40S-A | 75.152 (69.554–76.028) | 46.677 | 0.094–0.101 |
| SHP2-ti | 107,326 | H100 | 62.862 (62.846–63.361) | 39.044 | 0.206–0.222 |

*CMET-ti has a particularly weak observed release lower bound. Its low endpoint
is not a credible point-price estimate. The same caution applies to BFC's
USD 0.010–0.147 bound; SFC has only an upper bound, USD 0.090. Every operation's
cost/coverage appears in `operations.csv`; missing bounds are unknown, not zero.
PEP-h's hydrogen-only constraints and GPU update differ from PEP's all-bond
fixture: these are distinct inputs, not a speed optimization of identical science.

## Hosted MPI: functional comparison, not controlled strong scaling

All six rows use exactly the same original MEM TPR SHA-256
`5099268bf3a3d948c03b3b78432f29a2c5d4207b54d02e7e294f8138b587d473`,
81,743 particles, 2 fs and three 10,000-step repeats. Shapes mean nodes × GPUs
per node; one MPI rank per GPU, eight threads per rank. The public `auto`
protocol uses `-nb gpu -update cpu`. In these logs, one rank places PME on the
GPU; two ranks select zero separate PME ranks and CPU PME. Two-rank logs map
only PP GPU tasks and record CPU PME mesh/FFT work. Auto PME tuning also changes
grid/cutoff balance during the run. Thus these are not fixed-dispatch scaling
controls, even though the input science is held constant.

| Path / shape | Actual hardware | Native ns/day median (range) | M particle-steps/s, total | Durable ns/allocated GPU-hour | Operation USD |
| --- | --- | ---: | ---: | ---: | ---: |
| REST 1×1 | 1 L40S-A | 82.637 (81.951–104.331) | 39.091 | 1.586–1.756 | 0.078–0.086 |
| Raw MCP 1×1 | 1 L40S-A | 83.649 (81.643–84.280) | 39.570 | 1.593–1.756 | 0.078–0.086 |
| REST 1×2 | 2 L40S-A, same node | 32.064 (30.384–32.696) | 15.168 | 0.438–0.462 | 0.297–0.313 |
| Raw MCP 1×2 | 2 L40S-A, same node | 34.540 (30.502–35.699) | 16.339 | 0.462–0.491 | 0.279–0.297 |
| REST 2×1 | 2 H100, two nodes | 28.457 (27.871–29.104) | 13.462 | 0.419–0.441 | 0.613–0.645 |
| Raw MCP 2×1 | 2 H100, two nodes | 29.000 (28.713–30.397) | 13.718 | 0.427–0.450 | 0.600–0.632 |

The measured auto configuration is faster and cheaper on one L40S than these
two-GPU layouts. That observation does not isolate GPU count, transport or
hardware as the cause. PEPs concurrently occupied other GPUs of the same
four-GPU L40S VM, and cross-node H100 differs in both hardware and topology.
The external-MPI image does not detect CUDA-aware MPI; the two-node transport
is host-staged TCP. No RDMA result is claimed. Separate fixed-PME and corrected
CUDA-aware native experiments are not merged into these hosted results.

Raw MCP exercised `submit_gromacs_mpi_workflow`, idempotent replay,
`get_scientific_status` and `get_scientific_result`, with independent REST
history and native artifact verification. It is **not an LLM/installed-skill
measurement**. Similar REST/MCP native rates do not measure their handshake,
reasoning, queue or end-to-end delivery overhead. There is one operation per
path/shape, so no transport-latency distribution is established.

| Selected operation | UUID |
| --- | --- |
| REST 1×1 | `d22706c0-c87a-4c57-962d-8f41b0367b4c` |
| REST 1×2 | `9a787f2e-a525-4d24-9a0f-32cd5705d1b5` |
| REST 2×1 | `b453cad7-9526-4c7b-a2b7-904e21c13ffe` |
| Raw MCP 1×1 | `bcf5b2c8-7ce8-49e4-86cc-9b690f97f129` |
| Raw MCP 1×2 | `63d0eee8-d398-422c-92eb-7594159b910d` |
| Raw MCP 2×1 | `6c2459a2-abc6-462a-a363-1367b43f2276` |
| PEP-h REST-D | `f59fd84f-6f8b-4664-b65a-5f496122cea6` |

## Units, allocation-share pricing and phase occupancy

For native rate R ns/day, timestep dt ps, N particles and g allocated GPUs:
`particle-steps/s = N × R × 1000 / (86400 × dt)`; divide by g for a per-GPU
rate. Durable progress instead uses the union of committed checkpoint intervals
within each logical repeat. `ns/GPU-hour = durable_ns × 3600 / allocated_GPU_seconds`.
For example MEM, RIB and PEP-h's complete three-repeat operations contain
2.45229, 61.13472 and 374.86509 billion durable particle-steps respectively.
They achieve 19.608–21.702, 75.903–77.288 and 169.175–171.484 million durable
particle-steps per allocated GPU-second. This includes reserved startup/tail
time, unlike the native-rate tables.

Price scenario: **eu-north1, USD on-demand public list prices dated 2026-10-03**,
rechecked against [official Compute pricing](https://docs.nebius.com/compute/resources/pricing).
H100 is USD 4.50/GPU-hour from October 1, not the earlier 3.85. L40S is
1.35/GPU-hour plus VM CPU/RAM: Intel 0.012/vCPU-hour, AMD 0.010/vCPU-hour,
both 0.0032/GiB-hour. Our L40S-I 1-GPU/16-vCPU/64-GiB VM is 1.7468/hour;
L40S-A 4-GPU/128-vCPU/768-GiB is 9.1376/hour, apportioned 2.2844 per allocated
GPU-hour. H100 eight-GPU VM cost is 36/hour; one allocated GPU receives 1/8.
This is a GPU-share allocation model of the actual VM preset, not pricing the
Pod's smaller CPU/RAM request. Costs exclude taxes, discounts, storage, network,
control plane and unallocated VM lifetime; **they are not invoices**.

The generic operation-level runtime may report `gpu_count: 0` after completion,
including an observed two-GPU run. Scientific admission deliberately sets generic
`reserved_gpu_seconds=0`: exact stage/resource accounting belongs to the
[lifecycle ledger](../../components/control-plane/src/fs2_serve/scientific_batch/service.py),
not a guessed generic-worker reservation. These fields are not measured zero
consumption. This offline ledger uses the frozen
attempt/shape, actual per-Pod GPU requests and recorded lifecycle/allocation
observations. Queue wait is distinct from allocated GPU time.

Measured phase scopes below are **reserved GPU-seconds, not GPU busy time**.
They do not partition a timeline and must not be added as if they did. Native
Wall t is nested inside whole `mdrun` process wall. Init-container process spans
include download/validation/startup; collector tail excludes exports overlapping
the worker. The two-node native commands are each correlated with both Pods.

| Observed scope, GPU-seconds | REST MEM MPI 1×1 | REST MEM MPI 2×1 | REST PEP-h |
| --- | ---: | ---: | ---: |
| Init-container processes | 3.000 | 8.000 | 6.000 |
| Native `mdrun` Wall t counters | 58.565 | 364.232 | 1752.426 |
| MPI peer-input staging | 0.0073 | 0.6492 | not instrumented |
| `energy`/`eneconv` command processes | 2.997 | 1.368 | 0.097 |
| Post-worker collector tail | 4.000 | 3.392–13.553 | 20.081–29.841 |
| Entire observed Pod allocation bound | 123.000–136.229 | 490.000–516.106 | 2186.000–2215.841 |

Native counter allocation-share costs for those three columns are USD 0.03716,
0.45529 and 1.11201 respectively, not pure simulation costs. Two-node MPI
input staging recorded 0.324621 seconds and 3,424,316 peer bytes across the
three commands; parallel peer durations are not summed. The one-node stager's
explicit peer list is empty (zero transferred peer bytes), not missing data.
Exact native initialization, pure GPU computation, checkpoint and total export
durations remain **null**. The [measurement contract](MEASUREMENTS.md) records
the smallest missing instrumentation hooks; no runtime instrumentation changed.

### Additive check against durable production accounting

At **14:10:02 UTC**, a bounded read-only selection of these three exact system/qa
attempts from `fs2_telemetry_subjects LEFT JOIN fs2_reporting_lifecycle_latest`
confirmed the production accounting source. The immutable offline `report.json`
above is unchanged; `durable-lifecycle-sidecar.json` is a separate receipt,
SHA-256 `d740aef2344c034b6253f68634f9bb4280e5509e76c982c70b5fc5b5b7e55fee`.

| Operation | Offline GPU-second bounds | Durable scheduler GPU-seconds | Same price model, USD |
| --- | ---: | ---: | ---: |
| REST MPI 2×1 `b453cad7…` | 490.000–516.106 | 494 | 0.61750 |
| Raw MCP MPI 2×1 `6c2459a2…` | 480.000–505.998 | 480 | 0.60000 |
| PEP-h `f59fd84f…` | 2186.000–2215.841 | 2205 | 1.39920 |

All three lie within the independent offline bounds. Their terminal rollups are
`reconciled=true`, quality `application_observed`, and retain the data gap
`trace_context_missing`. The sidecar preserves separate quota, scheduler,
device-allocation and phase clocks, event watermarks/digests and attempt IDs.
The broad production `active_compute` phase includes non-integration worker
activity; it is not substituted for native Wall t or pure GPU compute. A
reconciled lifecycle rollup is not an invoice or an exact utilization integral.

## Failures and retry waste remain part of the result

REST-A had 12/16 failures when its forced half-run timer reset intersected PME
tuning. Its four successes are retained but excluded from corrected tables.
REST-B had 20 successes, three failures and one cancellation; REST-C completed
MEM/RIB but both PEP variants exceeded their then-selected output envelope.
The larger-envelope PEP-h retry now delivered all results; PEP is still pending
in this snapshot. A completed native segment inside a failed operation is not
silently promoted to complete workflow delivery.

Only the reviewed, exact-TPR/request-matched B→C MEM/RIB and B→C→D PEP chains
are grouped. Their repeat-1/2/3 identities remain separate; MPI shape/path
experiments and forced-reset REST-A are never merged into those retry chains.

| Explicit lineage | Duplicate durably completed steps, lower bound | Duplicate simulated ns | Total lineage allocation-share USD |
| --- | ---: | ---: | ---: |
| MEM B→C | 0 observed | 0 observed | 0.088–0.113 |
| RIB B→C | 20,000 (repeats 1 and 2 repeated) | 0.08 | 0.507–1.012 |
| PEP-h B→C→D | 20,000 (repeat 1 completed three times) | 0.04 | 2.281–3.083 |
| PEP B→C→D | 0 established at this cutoff | unknown complete progress | incomplete |

PEP-h's failed predecessors occupied USD 0.894–1.676 of modeled allocation
share before the final USD 1.387–1.406 operation. Its lineage produces 0.06
useful benchmark ns after overlap removal, or USD 38.02–51.38/useful ns under
this scenario. RIB's failed predecessor adds USD 0.0055–0.5006 with wide
telemetry bounds. Duplicate durable particle-steps are at least 40.75648 billion
for RIB and 249.91006 billion for PEP-h. These are **lower bounds on repeated
work**, not total wasted execution: uncommitted/lost steps and exact restart
overhead are unknown. Zero observed overlap is not proof of zero waste.

The first hosted MPI 1×1 operation
`0a828a8f-a743-4055-b605-efe24dba9a1f` failed before engine startup on the
platform's input-authorization path. It remains a failed operation, with no
native throughput; only an allocation-cost upper bound, USD 0.0111, is known.
It is not a bad molecular input or a successful native run. The corrected
API's later operations are listed separately above.

## Lynx: recorded requirements versus our representative assumptions

Detailed source mapping and runnable fixture commands are in
[LYNX_WORKLOADS.md](LYNX_WORKLOADS.md). Current CPU replay evidence is in
[LYNX_AGENT_QUALIFICATION.md](LYNX_AGENT_QUALIFICATION.md), independently retained
as `lynx-agent-r4-cpu-evidence.json` and the actual/assumed delivery verifications.
The exact isolated client is `6d8b2038097b180d5edd997d7346a1b56c879a5f00b4890fcc08b81c2a96b2da`,
source `a09a40c1a099bcb2e99c7e03caa644b30f78e86b`: seeded Kimi-K3/high reasoning,
system/qa, unchanged 25-tool budget. This is not a promoted customer client.

| Evidence class | Workload | Observed scope / remaining gap |
| --- | --- | --- |
| Actual recorded request | Inventory the original uploaded CIF | CPU agent pass, 3.040 s / 2 calls; verified inventory and file link, not MD preparation |
| Actual recorded request | Recommend a compatible small-molecule force field | Advice-only pass, 6.037 s / 1 call; no unsolicited execution |
| Actual recorded request | Parameterize original stereospecific 20-hydroxyecdysone using OpenFF | CPU agent pass, 99.441 s / 7 calls; 89.072 s AM1-BCC CPU-phase wall; neutral 78-atom Sage 2.2.1 ligand export |
| Actual requested integration | Preserve LynxKite pipeline/settings and call GROMACS directly, without an LLM | REST/raw MCP MPINAT evidence above exercises those transports with provider fixtures; the customer's native production bundle/settings were not supplied |
| Actual later-feature suggestion | Estimate performance for different GPU sizes | No validated estimator; shape/cost measurements are bounded observations |
| Assumed controls | Public 1UBQ inventory, CHARMM-family advice, aspirin, methylammonium, undefined stereo, missing file | Six CPU agent behaviors passed; 3.0–21.2 s each. Negative cases ask/report missing choices rather than inventing chemistry |
| Assumed hosted workflow | Packaged alanine: minimization, 20 ps NVT, 20 ps NPT, 20 ps production | Actual seeded-agent MD evidence remains pending for this milestone; not a 1 ns/converged/customer GPCR run |
| Assumed performance matrix | MPINAT inputs, MPI shapes, retention/recovery | This report; not a Lynx-supplied scientific protocol, membrane/FEP production study or capacity estimator |

The CPU replay checked all 30 ligand export file hashes, requested chemical
identity, atom counts and charges. AM1-BCC time is elapsed CPU-work wall, not
GPU occupancy or cumulative CPU-core seconds. A padded empty ligand export box
is not solvation, equilibration, force-field accuracy or GROMACS energy
equivalence. Expected missing-file rejection and the retained minor
stereochemistry naming typo are documented, not hidden. CPU success and
analysis-only recovery of existing MD results do not close the pending
actual-agent/installed-skill MD execution path.

## Remaining boundaries

This milestone does not claim completion of PEP, later 1×4 hosted cases, whole-node
1×8/2×8 capacity-dependent cases, full actual-agent MD, cold-start capacity,
sustainable concurrency, failure-resume capacity or customer production science.
Eight-/sixteen-GPU execution remains limited by available whole-node capacity;
no customer movement is authorized. Missing telemetry, exact phase timers and
unmatched historical input/timing remain explicit uncertainties. Later campaign
results must receive a new snapshot rather than silently changing these tables.
