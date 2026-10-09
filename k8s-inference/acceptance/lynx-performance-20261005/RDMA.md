# Full-node InfiniBand qualification — 2026-10-05

This is a separate successor to the immutable [TCP measurements](FINDINGS.md),
not a reinterpretation of them. Native GROMACS has passed, including three
1 ns repeats. Exact-release public REST/MCP qualification remains pending;
native measurements alone do not establish public delivered performance.

## Infrastructure and ownership

The reviewed replacement changed only the two reserved H100 full nodes. It
retained the existing STRICT 16-GPU capacity block, subnet, shared filesystem,
security groups and node envelope. It did not change the active customer L40S
Pod, other pools, GPU quotas or the internal QA concurrency limit.

- Node group: `mk8snodegroup-e00twzfv2vh6gs8j4p`.
- GPU cluster: `computegpucluster-e00p8hjysxfyk1n58x`, `fabric-2`.
- Nodes: `computeinstance-e00s8g6t7z6qvz3f9p` and
  `computeinstance-e00zgn138sxphp909c`, eight H100 GPUs each.
- Provider-managed image owns the existing GPU driver, OFED and GPU allocator.
  No second GPU/OFED owner was installed. A pinned allocator-only DaemonSet
  exposes one exclusive eight-HCA bundle per node as `rdma.fs2.nebius/hca`.
- Scientific test Pods remain non-root UID 10001, with no host filesystem,
  host IPC or privileged container. The initial IPC_LOCK experiment added only
  a bounding capability, not an effective capability; that negative result is
  retained and does not establish elevated memlock capability.

Private evidence is under
`/home/tux/secure-handoff/fs2-h100-infiniband-reprovision-20261005`.
The exact replacement plan SHA256 is
`283ff57cd98d37955f8d50ee4ab2382e1c3b12d06c5be6ef0ea1633c92e8db20`;
the allocator-only two-object plan SHA256 is
`263bb1eb9d387facb56e3d49b96fe3540ee53f0d04fb4672c9f87fe093b8f09b`.
The follow-up refresh-only plan updated outputs without cloud resource actions.
Broad stale Terraform desired-state drift was not applied.

## Worker and observed limits

The additive immutable worker supplies the matching userspace verbs provider;
it does not replace the GROMACS, Open MPI or UCX binaries or host drivers.
`ucx-rdma` is an operator-owned, fail-closed transport: UCX accelerated RC,
CUDA copy/IPC and local shared memory, with no TCP data fallback. Each rank
selects its nearest HCA by actual GPU UUID and PCI ancestry, not by assuming
GPU and NIC ordinals match. All eight rails are represented on each node.

Initial failures are retained: missing userspace provider; default UCX
allocation exceeding the container's 8 MiB memlock limit; ineffective
IPC_LOCK; an observer parser that missed UCX 1.19 first-use protocol tables;
and singleton GROMACS commands unnecessarily opening the distributed transport.
Singleton version/dump/analysis calls now use a local-only MPI environment;
actual mdrun retains the fail-closed distributed transport.

The CUDA-buffer probe passes actual Sendrecv, nonblocking transfer and Allreduce
at 4 B, 4 KiB and 1 MiB on sixteen unique physical GPUs. Its first-use UCX
protocol tables show inter-node CUDA-memory zero-copy `rc_mlx5` paths. That is
executed-buffer/protocol evidence, not a hardware-counter bandwidth claim.
The first native run past singleton setup stalled before step zero. A small
CUDA-buffer success is therefore explicitly insufficient for GROMACS readiness.

The subsequent exact-image host control (`native-host-r10`) reached a real
`ibv_reg_mr` failure at the 8 MiB locked-memory limit during the first 9 MB
broadcast. The separately requested TCP control (`native-tcp-control-r11`)
passed all nine Bcast/Scatterv/Alltoall checks. Neither the RDMA timeout nor the
earlier missing-compiler harness failure is relabelled as a successful MD run.

The next candidate therefore adds IPC_LOCK file capabilities only to four fixed,
root-owned executables under `/opt/fs2-rdma/bin`: the unchanged GROMACS binary,
the CUDA communication probe, the host-collective probe and a fixed memlock
diagnostic. There is no arbitrary-command capability helper. A frozen RDMA
stage alone permits that capability; original executable paths, old local/TCP
shapes and all companion/init containers remain drop-ALL with escalation
disabled. Trusted image library directories support secure executable loading;
the probe checks actual effective/permitted/bounding IPC_LOCK, UID 10001 and a
64 MiB `mlock`, without changing any limit.

Exact image `c8321a27df6490e3ca33d7f2d80ee5d5a4917b787c612e98c1704346f1cbdece`
(source `33349d2489685487260457d3d90fcd743dd61817`) subsequently passed
`native-memlock-r12`: both probes and all sixteen actual GROMACS processes had
UID 10001 and only effective/permitted/bounding IPC_LOCK. The 8 MiB limit stayed
unchanged, the fixed 64 MiB mlock succeeded and the secure loader removed
`LD_LIBRARY_PATH`. All nine large host collectives and actual CUDA-buffer
zero-copy verbs checks passed, with no TCP data fallback. Three unchanged
50,000-step Lynx repeats then passed 37 inventory hashes, finite energies and
coordinates, original TPR identity and monotonic native steps. Native-inclusive
rates were 345.117, 327.223 and 349.147 ns/day (median 345.117); the separate
workflow wall was 163.384 seconds for 0.3 ns, including native preparation and
analysis. These are short native measurements, not public delivered throughput.

Same-image `compat-local-h100-r12`, `compat-local-l40s-r12` and
`compat-tcp-2x1-r12` passed their three-repeat original MPINAT controls with
legacy drop-ALL execution paths. Every task Pod was UID-fenced and confirmed
absent. The longer unchanged three-by-1ns RDMA confirmation is separate; its
explicit `--native-timeout 1800` extends only the acceptance harness lifetime,
not production limits. Public REST/MCP and performance publication remain gated.

Short native receipt SHA256:
`9dc228adeb944cd3a241dd2e3cca61cba028ff3cbab0dcfee53e0a4667b80897`.
The original failed builds, timeouts and controls remain alongside it.

## Longer native comparison and recommendation

`native-confirm-r13` finished at `2026-10-05T19:14:21.551710+00:00` on the
same exact `c8321a27…` image. Three 500,000-step repeats each simulated 1 ns
from the original 185,486-atom TPR. All 46 inventory hashes, finite energies,
coordinates and monotonic native checkpoints passed. Host collectives,
CUDA-buffer communication, actual effective IPC_LOCK and no-TCP transport
checks passed again. Both exact Pod UIDs were deleted and absence observed;
the read-only sampler subsequently exited naturally after its STOP marker.

| Native configuration | Work | Median native-counter ns/day | Repeat range | ns/day per allocated GPU |
| --- | --- | ---: | ---: | ---: |
| One H100, GPU update/list 200 | 3×1 ns | 206.88 | 206.65–209.22 | 206.88 |
| One node, eight H100s, CPU update | 3×1 ns | 318.53 | 295.87–326.44 | 39.82 |
| Two nodes, sixteen H100s, RDMA/CPU update | 3×1 ns | 350.90 | 348.84–363.61 | 21.93 |
| Earlier sixteen H100s, TCP/CPU update | 3×0.1 ns | 55.78 | Short screen | 3.49 |

The new sixteen-GPU median is **10.2% higher** than the retained eight-GPU
median at twice the GPU allocation, or about 55% of its per-GPU efficiency.
The eight- and sixteen-GPU long runs use eight threads/rank, bonded GPU,
fixed GPU PME/FFT, one PME rank, CPU update, unchanged output cadence and no
explicit list-interval override (native logs select 80). They differ in rank
layout (7 PP + 1 PME versus 15 PP + 1 PME), 64 versus 128 requested vCPUs,
node generation, runtime wrapper/provider/file capability and transport.
This is an observed configuration comparison, not a controlled estimate of
InfiniBand's isolated speedup. The old TCP case is also ten times shorter;
neither its ratio nor the two-thread TCP variant's 58.01 ns/day is a matched
long-run scaling factor. Single-GPU GPU-update/list-200 numbers are a separate
protocol, not the same multi-rank execution.

The cost-efficient tested customer recipe remains the [long public single-L40S
recipe](FINDINGS.md): 201.0/196.6 delivered ns/day via REST/raw MCP with eight
vCPUs, bonded GPU and list interval 200. One-node eight H100s remains a
latency/per-GPU-efficiency compromise. Sixteen RDMA H100s now has the highest
measured single-trajectory native rate, but only a modest gain over eight and
not a cost-efficiency win. Public RDMA delivery, checkpoint and durable-accounting
verification must pass before recommending it as an end-to-end option. No
customer job or default is changed by this recommendation.

The separate [MPS screen](../lynx-mps-20261005/README.md) addresses aggregate
independent-process throughput: H100 four-client MPS reached 218.51 aggregate
process-wall ns/day versus 172.61 without MPS for one process; L40S two-client
MPS reached 202.17 versus 177.95, with overlapping short-run ranges. Individual
trajectories became slower. Those 100 ps screens exclude hosted export and
are not comparable to either native-counter or public-delivery clocks here.
MPS is neither an acceleration switch for one ongoing trajectory nor a
qualified public sharing mode. This distinction follows the scope of the
[NVIDIA MPS/MIG experiment](https://developer.nvidia.com/blog/maximizing-gromacs-throughput-with-multiple-simulations-per-gpu-using-mps-and-mig/),
not its A100/older-GROMACS speedup numbers.

### Clocks, allocation share and limits

The three native counters total **731.524 s** (380.34 million atom-steps/s).
Actual mdrun subprocess wall is **798.969 s**; the native supervisor scope is
**820.370 s**, or 315.96 ns/day, including input copies, worker workflow and
local evidence retrieval. It is not server accepted-to-delivered throughput.
Analysis-command process wall is 1.875 s. Peer input staging measured 0.387 s
and 8,996,516 bytes. The retained inventory totals 123,922,737 bytes, not a
network-transfer total. Separate initialization, pure integration, checkpoint
publication and export durations remain null; no residual is assigned to them.
Local checkpoint replay is zero; remote durable replay is unknown because
this native qualification has no public checkpoint/export path.

The existing ledger's allocation and pricing helpers—not a second accounting
model—bound both Pods from scheduling through observed release. Their total
is **14,389.84–14,760.83 GPU-s**, equivalent to **$17.987–18.451** for this
3 ns native qualification. It includes host/CUDA gates and preparation and
must not be called steady-state MD cost. The native counter-window share alone
is $14.630 ($4.8768/ns); comparable three-repeat counter-window shares for
one/eight H100s are $0.5203/$2.7598 per ns, not complete workflow costs.

These are dated **2026-10-05 on-demand allocation-share scenarios**, using the
actual two `8gpu-128vcpu-1600gb` H100 presets at $36/node-hour, $72/hour total,
from [official compute pricing](https://docs.nebius.com/compute/resources/pricing).
They are not the reservation invoice and exclude storage, network, tax,
discounts and idle VM time outside the Pod bounds. Historical failed native
qualification attempts remain separate; they are not included in this r13
success cost and are not treated as free or retroactively successful.

The 63 retained samples per Pod span 864.64 s and include non-MD work. Mean
CPU usage was 54.33/48.46 cores; unweighted GPU activity averaged 25.24/25.70%
and device power 167.19/166.72 W. These are not an energy integral or a
native-only utilization average. The first native log's rank-weighted cycle
categories include coordinate communication 19.4%, force wait/communication
17.4%, constraints 18.6%, launch PP GPU operations 12.4%, force 3.6% and PME
GPU mesh 2.0%. They are not network-only wall or kernel measurements. Executed
transport is now verbs rather than TCP, but the retained host accounting
does not isolate that change's gain, justify linear scaling or identify one
exclusive remaining cause.
The [GROMACS performance guide](https://manual.gromacs.org/2026.2/user-guide/mdrun-performance.html)
likewise treats rank/thread/offload choices as workload-dependent measurements.

Private fixed evidence under the infrastructure evidence root above:

- `native-confirm-r13/receipt.json`, SHA256
  `bea72860f56382db6b7c10babcfe160ce95d8ac306013c20b6b1391c7327e0b3`.
- `native-confirm-r13-accounting-r1/report.json`, SHA256
  `c82feeaf5e9bf576ddaed685073194ecd9eff999e03e1760c8ce31edab7a1f1b`.
- `native-confirm-r13-samples/samples.jsonl`, SHA256
  `26b3773048422e64697e99d1ab508f38bf2e62b6f0a1c2760495315061ccfe91`.

`summarize_rdma.py` rehashes the exact receipt-bound fixture and native outputs,
then reuses the existing phase, price, allocation-bound and sampled-counter
calculations. It copies no trajectory and makes no API/DB/GPU call. To project
a different final receipt, use a fresh output directory and stopped sampler:

```bash
components/control-plane/.venv/bin/python acceptance/lynx-performance-20261005/summarize_rdma.py \
  --receipt "$FINAL_RECEIPT" --samples "$STOPPED_SAMPLES" \
  --references acceptance/lynx-performance-20261005/cost_references.json \
  --pricing-date 2026-10-05 --output "$FRESH_PRIVATE_ACCOUNTING"
```

`qualify_rdma.py` owns only exact labelled test JobSets, verifies image digests,
GPU/RDMA resources and current free capacity, and deletes only its retained UID
before proving Pod absence. `runtime/rdma/host_collectives.c` adds Bcast, Scatterv and
Alltoall checks at 9 MB, 16 MiB and 32 MiB per destination. Every byte is checked;
these are correctness controls, not bandwidth measurements. An explicit
`--transport tcp-host-staged --host-collectives` control is allowed only without
an MD input; it is never an automatic fallback for a claimed RDMA result.

```bash
components/control-plane/.venv/bin/python acceptance/lynx-performance-20261005/qualify_rdma.py \
  --name fs2-lynx-rdma-host-UNIQUE \
  --image "$EXACT_RDMA_CANDIDATE" --file-ipc-lock --host-collectives \
  --output "$FRESH_PRIVATE_EVIDENCE"
```

Only after host and device communication pass should `--input` select the
unchanged finite 2×8 Lynx fixture for the matched three-repeat comparison.
Keep all molecular parameters and output cadence unchanged; only the recorded
finite step override differs from the original TPR.

## Publication ordering

1. Roll compatible schema-38 readers with the old catalog and old publication
   ConfigMaps; verify every durable-state consumer before exposing new shapes.
2. Bind the genuinely qualified image, native proof and exact source recipe,
   including `Containerfile.mpi-rdma`, to the new `multi-node-8gpu-rdma` shape.
   Preserve the eight older shapes and request schemas.
3. The frozen new shape requires exactly two Pods, each with eight GPUs,
   one RDMA bundle and the exact GPU-cluster label. Per-node scheduling facts
   and the Kueue GPU/RDMA resource group must agree: aggregate 16 GPUs/2 bundles.
   Old GPU-only placement facts cannot qualify it.
4. Publish scoped content-addressed execution/scheduling/admin/envelope changes,
   then prove actual REST and raw MCP admission, native results and accounting.
   No API rollout, profile publication or customer-readiness claim is performed
   by the native helper.

After any shaped record exists, retain compatible readers even during a
runtime/config rollback. Historical frozen plans, failed attempts and TCP
receipts remain unchanged.

Primary references checked October 5:
[Nebius managed GPU ownership](https://docs.nebius.com/kubernetes/gpu/set-up),
[UCX network selection and GPU zero-copy](https://openucx.readthedocs.io/en/master/faq.html),
and [NVIDIA's RDMA container examples](https://docs.nvidia.com/networking/display/kubernetes25100/deployment-guide-kubernetes.html).
The installed image and retained process/device evidence remain authoritative
for this exact deployment.
