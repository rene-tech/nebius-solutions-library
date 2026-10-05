# Full-node InfiniBand qualification — 2026-10-05

This is a separate successor to the immutable [TCP measurements](FINDINGS.md),
not a reinterpretation of them. Public RDMA admission is still disabled pending
native GROMACS and then exact-release REST/MCP qualification.

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

`qualify_rdma.py` owns only exact labelled test JobSets, verifies image digests,
GPU/RDMA resources and current free capacity, and deletes only its retained UID
before proving Pod absence. `host_collectives.c` adds Bcast, Scatterv and
Alltoall checks at 9 MB, 16 MiB and 32 MiB per destination. Every byte is checked;
these are correctness controls, not bandwidth measurements. An explicit
`--transport tcp-host-staged --host-collectives` control is allowed only without
an MD input; it is never an automatic fallback for a claimed RDMA result.

```bash
components/control-plane/.venv/bin/python acceptance/lynx-performance-20261005/qualify_rdma.py \
  --name fs2-lynx-rdma-host-UNIQUE \
  --image "$EXACT_RDMA_CANDIDATE" --host-collectives \
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
