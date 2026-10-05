# Multi-GPU MPI implementation candidate

This records source behavior, not a GPU qualification or a customer-ready
claim. Root release acceptance must bind actual REST and installed-skill/MCP
results to the final images and admitted resources before promotion.

This October 3 record retains its original local/TCP implementation scope.
The October 5 successor adds an independently native-qualified, operator-selected
2×8 H100 InfiniBand shape; see the [current operator guide](../README.md#gpu-placement-and-the-full-node-infiniband-path)
and [separate RDMA evidence](../../../../acceptance/lynx-performance-20261005/RDMA.md).
Its public REST/MCP gate is separate; neither the old TCP record nor a native
communication probe alone qualifies that successor.

## Request and launch contract

`gromacs-mpi` retains its existing App and parameter-schema identity. `nodes`
accepts 1–8; `gpus_per_node` accepts 1, 2, 4 or 8, defaulting to 1. At most 16 GPUs
may be requested in one workflow. One rank uses one GPU; `threads` remains the
OpenMP thread count per rank, not per whole node. Scientific TPR/MDP settings are
unchanged by this placement choice.

One node uses one admitted Pod and local Open MPI without an SSH server or
credentials. Multiple nodes retain the existing JobSet ownership, distinct-node
placement, coordinator and verified changed-input staging. A hostfile has one
entry per Pod/node with `slots=gpus_per_node`; the launcher requests
`nodes × gpus_per_node` MPI ranks and maps `gpus_per_node` ranks to each node.

Each rank checks MPI world/local counts and the CUDA-visible device count,
resolves its local GPU's UUID through the CUDA driver, and execs GROMACS with
the documented per-rank `GMX_GPU_ID` selection. Other GPUs allocated to the same
Pod stay visible for UCX/CUDA IPC. It never substitutes the coordinator's GPU
UUID on a different node. Each launch emits per-rank GPU, host, CPU-affinity and timestamp
receipts into the native command log. Missing or duplicate receipts are marked
as incomplete evidence, not reported as a measured allocation.

The backend freezes these optional runtime checks with the admitted shape:

- `FS2_GROMACS_MPI_NODES`
- `FS2_GROMACS_MPI_GPUS_PER_NODE`
- `FS2_GROMACS_MPI_RANKS_PER_NODE`
- `FS2_GROMACS_MPI_TOTAL_RANKS`

Any present value that differs from the normalized request is an error.

## Transport and performance boundaries

Transport is operator-owned through `FS2_GROMACS_MPI_TRANSPORT`, not an arbitrary
customer request or an unqualified public RDMA toggle:

- `ucx-local` is restricted to one node. The packaged CUDA-aware UCX/Open MPI
  path uses shared memory and CUDA IPC candidates; GROMACS determines whether
  the input supports direct GPU communication. Unsupported UCX does not silently
  become TCP under a CUDA-aware label.
- `tcp-host-staged` uses Open MPI OB1 with local shared memory and inter-node
  TCP, and explicitly disables GROMACS direct GPU communication. This remains
  the multi-node fallback until a real network profile is qualified.

Receipts distinguish configured transport from observed transport. They do not
claim RDMA, exact network throughput, measured CUDA IPC, or CPU/NUMA pinning merely
because the program starts. `/dev/shm` needs enough capacity for all local ranks.
Native GROMACS and network logs remain necessary for performance qualification.

The existing compiled external-MPI engine is reused. It is upstream GROMACS,
not the NVIDIA NGC thread-MPI executable. A matched 1×1 external-MPI baseline is
needed before evaluating 1×2/4/8 and 2×8 strong scaling. Compare `ns/day`, latency
and useful work per GPU-hour; additional GPUs are not automatically faster or
more efficient. No persistent CUDA-process snapshot is introduced.

References: [GROMACS 2026.2 performance guidance](https://manual.gromacs.org/2026.2/user-guide/mdrun-performance.html),
[Open MPI 5.0.8 CUDA/UCX support](https://docs.open-mpi.org/en/v5.0.8/tuning-apps/networking/cuda.html),
[Open MPI launch/environment options](https://docs.open-mpi.org/en/v5.0.8/man-openmpi/man1/mpirun.1.html).
Per-rank device selection is documented in
[GROMACS environment variables](https://manual.gromacs.org/2026.2/user-guide/environment-variables.html#envvar-GMX_GPU_ID).

## Native recovery and failure publication

The normalized default `gpus_per_node=1` is omitted from the recipe document so
old nodes-only checkpoint hashes remain identical. Non-default GPU counts are
part of the immutable recipe; changing the shape cannot silently resume another
recipe's native checkpoint. Coordinator-only `.cpt` publication and complete
gang retry remain the recovery mechanism.

Output inventory exhaustion is a terminal workflow failure, even if native
integration finished. `result.json` is still written with the exact inventory
error, bounded failure-log references, `inventory_complete=false` and explicit
scientific-output omission metadata. Files are not deleted or truncated to fit
the requested output budget. Last committed checkpoint generation is recorded
separately from a generation whose publication may have failed; remote-companion
versus local-only commitment is explicit. A bounded failure inventory must never
be displayed as successful scientific output.

## Required deployment acceptance

Focused automated tests cover request bounds/default equivalence, GPU binding,
transport labels, single-node launch without SSH, resource mismatch rejection,
native checkpoint recipe mismatch, and diagnostic publication on output-budget
failure. They do not replace real GPU, public path, recovery, artifact-integrity,
or contention acceptance. Those remain owned by the parent MPINAT campaign.
