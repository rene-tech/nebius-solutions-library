# GROMACS multi-GPU extension and qualification

Owner authorized implementation, deployment and benchmark extension on October 3,
2026. Three linked Task Deck workers cover runtime, controller resource contracts,
and public/cost evidence; the parent agent owns integration and live rollout.

## Shape contract

`gromacs-mpi` remains the external-MPI upstream 2026.2 App; it is not the NGC
thread-MPI binary in `gromacs`. Add `gpus_per_node` (1, 2, 4, 8; default 1),
allow `nodes` 1..8, and bound the product at 16 GPUs. One MPI rank per GPU,
existing `threads` per rank. Existing nodes-only requests retain their recipe
identity and one-GPU-per-node resource envelope.

Operator-owned named execution shapes freeze resource requirements and pool
allowlists in the durable plan. One-node requests become one ordinary Job;
multi-node requests retain JobSet/Kueue gang admission and distinct hosts.
No unrelated gang invariant is weakened. Per-node resources are 8 CPUs and
16 GiB RAM per GPU, with the existing 64 GiB scratch envelope. CPU/RAM tuning
is a separately identified experiment, not a silent change to a matched run.

## Hardware boundary

Read-only inventory at 12:28 UTC: both H100 capacity-block hosts are Ready,
8 GPUs each, 128 vCPUs, 1600 GB preset. `nvidia-smi topo -m` reports NV18
between all eight GPUs on each host. Driver 580.159.04, CUDA13.0 node image.
Neither provider instance nor node group belongs to a Nebius GPU cluster;
the instance has one eth0 subnet interface and Kubernetes advertises no RDMA
resource. Intra-node CUDA-aware UCX/CUDA IPC is a candidate. Cross-node TCP
is the currently available transport, **not an InfiniBand/RDMA result**.

At review, 15 of 16 GPUs on those hosts were allocated to other Apps. Owner
has been asked about rolling relocation with verified replacements. Until
approved, do not evict/drain customer-serving workloads. Unavailable full-node
capacity blocks the corresponding live measurements, not implementation.

## Matrix and evidence

- Same TPR, physics, steps, precision and runtime digest: 1x1, 1x2, 1x4, 1x8,
  then 2x8 GPUs for suitable large systems. Also retain the legacy 2x1 path.
- Initial 10,000-step, three-repeat screen, with tuning included. Confirm useful
  candidate layouts with longer repeated runs. No forced halfway reset during
  active PME tuning. These are timing repeats, not independent trajectories.
- REST and typed MCP admission/status/result tests, immutable input hashes,
  idempotent replay, complete downloaded/native artifacts, and bucket receipts.
  Use `run_mpi.py --interface rest|mcp`; use a distinct campaign directory for
  every shape, path, input or runtime. MCP transport tests do not qualify the
  actual seeded agent/installed skill; that remains a separate required path.
- Record actual rank-to-device binding, all GPUs' utilization, native timing,
  CPU and allocation occupancy, queue/staging/export delays, cancellation,
  native checkpoint resume, peer loss and resource release.
- Strong scaling: speedup, ns/day, ms/step, particle-steps/GPU-second, useful
  ns/GPU-hour and on-demand list-price cost. Keep GPU component, full-node
  resource cost and occupied-but-idle time distinct. Never bill retry progress
  twice; union committed intervals within each logical repeat.
- Compare public results only with exact source/methodology/hardware caveats.
  Missing comparable public data, phase timestamps or prices remains unknown.

Native `.cpt` recovery is supported work to preserve and requalify. CUDA process
snapshot restore across MPI ranks is not implemented and must not be advertised.

## Findings from the continuing REST cohort

The 12.5-million-particle PEP variants need more than the old 4 GiB *request*
output allowance for three retained repeats. Their generated request now uses
24 GiB, within the unchanged 48 GiB API bound and 64 GiB scratch envelope.
This is not a cloud quota change or deletion of earlier results. The approved
shared QA bucket remains 100 GB.

REST-C PEP-h operation `2d74acfe-cf85-4ee7-b2a8-9c3c95c96d70` failed after
performing native simulation. Loki shows final inventory raised `ValueError:
workflow exceeds the workspace file/byte budget`, leaving no result.json;
the collector subsequently reported an unavailable failed-native result.
The runtime worker is fixing bounded terminal diagnostic publication. Preserve
the failed attempt and committed checkpoints; do not count it as no computation
or successful delivery. The next corrected request is a distinct, explicitly
linked retry cohort. This failure is independent of the previously fixed MIME
identity collision in diagnostic artifact publication.

## Primary implementation references

- [GROMACS 2026.2 performance guide](https://manual.gromacs.org/2026.2/user-guide/mdrun-performance.html)
- [GROMACS 2026.2 installation guide](https://manual.gromacs.org/2026.2/install-guide/index.html)
- [MPINAT benchmark inputs and methodology](https://www.mpinat.mpg.de/grubmueller/bench)

No complete multi-GPU qualification claim yet. Exact image digests, operation
IDs, pass/fail bounds and rollback are recorded after deployment, not inferred
from passing local tests.
