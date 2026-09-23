# GROMACS on Scientific AI

Status: implementation and GPU qualification in progress. **Not customer-released.**
An onboarding profile is deployed for hosted qualification; the source template
under `activation/` remains deliberately unrouted. See [qualification evidence](qualification/RESULTS-20260923.md)
for what has actually run and which acceptance paths remain open.

This is a general molecular-dynamics App, not a customer-specific pipeline.
The official NVIDIA artifact is an optimized **NGC HPC container**, not an HTTP
NIM microservice. We retain NVIDIA's engine and add the existing Scientific AI
durable-job, storage and typed MCP interfaces around it.

## Scope and architecture

One immutable gzip-tar input bundle contains the coordinates, topology/includes,
MDP/TPR, index groups and optional native checkpoints. A typed request describes
independent jobs, each containing ordered native GROMACS commands. Commands take
explicit argv and stdin selections; no shell or arbitrary program is accepted.
Scientific parameters remain in the supplied MDP/TPR. We never silently change
force fields, timestep, seeds, ensemble or trajectory cadence for performance.

The adapter compiles into the existing PostgreSQL-backed scientific-batch engine:

`REST / typed MCP → durable Operation → existing Kueue admission → single-GPU Job`

Each job receives its own workspace. The native runner performs preparation,
minimization, MD and analysis. A CPU artifact companion handles streamed I/O,
native checkpoint publication, customer-bucket export and result collection.
Replicas and independent lambda windows can queue independently; a single job
can also run several windows and their analysis sequentially on one GPU.
Coupled replica exchange needs a separate execution shape (Priority 2).

GROMACS is not a docking pose search engine. Docking workflows compose an existing
platform docking App with explicitly parameterized ligand/protein inputs for MD.
`pdb2gmx` is not a universal ligand parameterizer; unsupported residues require
appropriate topology parameters, charge/protonation decisions and validation.

## Initial settings

| Setting | Default | Customer control |
| --- | --- | --- |
| Execution | One GPU, one thread-MPI rank, eight OpenMP threads | 1–8 threads; explicit native offload flags |
| GPU work placement | Native automatic selection | `-nb`, `-pme`, `-bonded`, `-update` |
| Local native checkpoint | Five minutes | `checkpoint_minutes`, 0.1–60 minutes |
| Coherent remote segment | Five minutes | `segment_minutes`, 0.1–60 minutes |
| Per-job wall budget | Six hours | `max_wall_seconds`, up to 72 hours; queue limits also apply |
| Files/workspace budget | 4 GiB | `max_output_bytes`, up to 48 GiB within a 64 GiB scratch shape |
| Output destination | Submitting user's assigned customer bucket, plus platform artifacts | `output_destination`, `output_prefix` |
| Customer output retention | Until customer deletion or their bucket lifecycle | Customer-managed; no silent platform deletion |
| Scientific output cadence | Supplied MDP/TPR, unchanged | `nstxout-compressed`, `nstenergy`, `nstlog`, etc. |

These are operational defaults, **not universal scientific standards**. A useful
trajectory sampling interval depends on the phenomenon and analysis. The byte
budget is not a bucket-quota increase. Input archive, temporary files and logs
also consume scratch. Large single-object transfer qualification remains open.
In particular, the current platform-artifact path uses single PUT uploads, not
multipart uploads. Do not promise individual files over 5 GiB just because the
aggregate workspace budget permits them. The customer-bucket copy does use
multipart uploads; both paths must be qualified before offering larger traces.

## Native recovery and output integrity

For finite MD, `mdrun` stops cleanly at the configured wall-time segment boundary.
The runner inventories closed native files and publishes a generation marker.
The companion uploads changed files, reuses unchanged content-addressed objects,
then writes the generation manifest last. Only after acknowledgement does the
engine resume with its `.cpt` and `-noappend`. Native neighbor-search boundaries
can delay a stop; this is not a hard millisecond checkpoint deadline.

Restart restores the latest committed generation of the **same operation and
replica**, including previous trajectory parts. Incomplete uploads do not become
recoverable generations. This is native GROMACS recovery, **not a CUDA process
snapshot**. A cancellation may leave the previous committed generation; the
platform currently invalidates upload authority when cancellation is requested.
Do not promise a final post-cancellation checkpoint before that path is qualified.

`-noappend` numbers coordinate outputs as well as trajectories. The runner keeps
all native parts and also copies the latest final coordinate to the normal
`-c`/`-deffnm` path, allowing the next explicit `grompp` step to use it. Explicit
`{"files":"md.part*.xtc"}` arguments resolve contained files without shell globbing.
Use `trjcat` and `eneconv` deliberately; do not concatenate binary trajectories.

Customer storage uses the original submitting user's tenant/user bucket policy.
Only the trusted companion obtains that user's bucket-scoped credentials, in
memory, through its active attempt capability. Credentials never enter the
simulation container, request schema, result or workspace. S3Transfer handles
customer-export multipart uploads and bounded retries. The structure is:

```text
<output_prefix>/<operation-id>/<job-id>/
  objects/<sha256>                         # immutable native file bytes
  attempt-001/checkpoint-00000001.json     # original names, hashes, sizes, state
```

There is no mutable cross-attempt `latest` pointer that a stale worker can replace.
Platform artifacts remain the authoritative recovery index; customer manifests
make every committed export reconstructible independently. Export errors are
failures, never silently reported as successful persistence.

## Runtime identity and extension boundaries

Pinned upstream: `nvcr.io/nvidia/gromacs@sha256:0e52e3ae971453898956379952b9ea606f5400cbdb4d439773ecbae4d8f5ad59`.
Its tag is `v2026.2`, but the binary reports **2026.2-dev**, CUDA 13, mixed precision,
thread-MPI. The regional registry is configurable via `NVIDIA_GROMACS_IMAGE`;
do not copy an eu-north1/project-specific mirror into another region's deployment.

H100 and L40S have been exercised on driver 580.173.02. Other GPUs must meet the
exact container's CUDA/driver and compiled-architecture requirements; no blanket
claim is made that every NVIDIA GPU has been tested.

| Capability | Exact NVIDIA build / implementation status |
| --- | --- |
| Preparation, MD, analysis, native checkpoint | Implemented; GPU fixtures tested |
| Free energy | Seven-window ethanol tutorial and BAR ran on L40S; hosted acceptance pending |
| Colvars | Compiled in; enhanced-sampling acceptance is Priority 2 |
| PLUMED | Compiled in; kernel availability and workflow acceptance are Priority 2 |
| CP2K QM/MM | Not compiled into this image; separate build required |
| Torch NNPot | Not compiled into this image; separate build required |
| Multi-node MPI | This image is thread-MPI, not external MPI; separate build required |
| CUDA/CRIU snapshot acceleration | Unqualified; native `.cpt` is the default recovery path |

Future multi-node work should reuse the platform's JobSet/Kueue gang scheduling,
an external-MPI GPU-aware build, compatible MPI/UCX/RDMA interfaces, and explicit
network/topology eligibility. Benchmark strong scaling on large systems before
selecting more nodes. MPS can improve aggregate independent-simulation throughput;
MIG and MPS must be measured per hardware/system, not inferred from an A100 blog.

## Development and qualification

- `runtime/fs2_gromacs`: canonical schema, streaming files and native engine runner.
- `build_contracts.py`: regenerates identical REST/MCP schema projections and the
  unrouted candidate profile.
- `activation/workload-profile.json`: candidate only, not an availability claim.
- `qualification/`: reproducible public fixtures and task-owned GPU probes.
- `components/control-plane/.../adapters/gromacs.py`: existing batch integration.
- `gromacs_checkpoints.py`, `gromacs_storage.py`: companion recovery/export.

The GROMACS runtime tests do not qualify hosted REST/MCP, customer bucket I/O,
queue bursts, preempted-node recovery or LibreChat. Those are separate acceptance
cases, and the candidate must not be marketed as available until they pass.

## Primary references

- [NVIDIA GROMACS container](https://catalog.ngc.nvidia.com/orgs/nvidia/containers/gromacs)
- [Versioned simulation/restart guidance](https://manual.gromacs.org/2026.2/user-guide/managing-simulations.html)
- [Native performance and offload guide](https://manual.gromacs.org/2026.2/user-guide/mdrun-performance.html)
- [Official free-energy tutorial](https://tutorials.gromacs.org/free-energy-of-solvation.html)
- [Heterogeneous parallelization](https://www.gromacs.org/topic/heterogeneous_parallelization.html)
- [NVIDIA MPS/MIG experiments](https://developer.nvidia.com/blog/maximizing-gromacs-throughput-with-multiple-simulations-per-gpu-using-mps-and-mig/)
