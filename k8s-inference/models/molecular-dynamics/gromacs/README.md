# GROMACS on Scientific AI

Status: the single-GPU backend is deployed and has passed hosted MD, free-energy,
six-job batch, Colvars, PLUMED, interrupted-worker recovery and cancellation tests. **This is not
a blanket qualification of every GROMACS workflow.** The updated workbench image
is deployed as Rene's replacement client and passed a real browser-agent Colvars
workflow with independently verified native outputs. The source
template under `activation/` remains deliberately unrouted. See
[initial qualification](qualification/RESULTS-20260923.md) and
[advanced/MPI evidence](qualification/P2-20260923.md) for exact tested
artifacts, release scope and remaining unqualified execution shapes.

The separate [native SM89 qualification](qualification/SM89-20260923.md) records
the L40S source-build candidate, architecture control and matched-input evidence.
It does not replace the released runtime or establish a global offload default.

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
| Per-job wall budget | Fourteen days | `max_wall_seconds`, 60–1,209,600 seconds; explicit smaller budgets and tenant admission still apply |
| Files/workspace budget | 4 GiB | `max_output_bytes`, up to 48 GiB within a 64 GiB scratch shape |
| Output destination | Submitting user's assigned customer bucket, plus platform artifacts | `output_destination`, `output_prefix` |
| Customer output retention | Until customer deletion or their bucket lifecycle | Customer-managed; no silent platform deletion |
| Scientific output cadence | Supplied MDP/TPR, unchanged | `nstxout-compressed`, `nstenergy`, `nstlog`, etc. |

The fourteen-day default applies to **newly admitted jobs**, including explicit
continuations. It does not change a running operation's frozen native budget.
The existing Lynx continuation retains its original seven-day budget; if needed,
continue its last committed checkpoint as a new operation rather than patching
the active Job. A configured fourteen-day limit is not a completed fourteen-day
soak test. See [the continuation API and exact release gates](CONTINUATION.md).

These are operational defaults, **not universal scientific standards**. A useful
trajectory sampling interval depends on the phenomenon and analysis. The byte
budget is not a bucket-quota increase. Input archive, temporary files and logs
also consume scratch. A 144,113,652-byte native trajectory has passed the hosted
download and customer-bucket paths, including multipart export. Larger individual
objects have not yet been qualified.
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

For a terminal job with a committed checkpoint, use the REST `:resume` endpoint
or typed `resume_gromacs_workflow` tool described in [Continuation](CONTINUATION.md).
An explicit continuation gets a new operation and budget; infrastructure retries
share the original budget. Changing today's default does not extend an already
running worker's frozen budget. The fourteen-day limit is not a fourteen-day
soak-test claim.

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
| Free energy | Seven-window ethanol tutorial and BAR passed the hosted MCP/client/bucket path; no convergence claim |
| Colvars | Compiled in; 200 ps radius-of-gyration metadynamics, restart and analysis qualified |
| PLUMED | Pinned 2.10 runtime kernel added; 1 ns hosted metadynamics survived Pod eviction with complete bias history |
| CP2K QM/MM | Not compiled into this image; separate build required |
| Torch NNPot | Separate LibTorch build passes 18 upstream H100 tests; not a customer-qualified App |
| Multi-node MPI | Separate `gromacs-mpi` App and external-MPI image; retained TCP hosted recovery/export tests and a newer native-qualified 2×8 H100 InfiniBand path have different release scopes; see below |
| CUDA/CRIU snapshot acceleration | Same-process GPU suspend/resume measured, not persistent/new-Pod restore; native `.cpt` is the default |

The separate MPI App reuses JobSet/Kueue gang scheduling and an external-MPI
GPU-aware build. Its older cross-node shapes use host-staged TCP; those retained
measurements do not describe the new InfiniBand deployment. One-node shapes use
the separate local UCX path. The additive `multi-node-8gpu-rdma` shape uses
operator-selected `ucx-rdma` with exact GPU-cluster and RDMA-resource eligibility,
not an unvalidated customer transport flag. Native qualification has passed;
**its exact-release public REST/MCP delivery and recovery gate remains pending**
in the [InfiniBand evidence](../../../acceptance/lynx-performance-20261005/RDMA.md).
Benchmark strong scaling before selecting more nodes. MPS can improve aggregate
independent-simulation throughput, not necessarily one trajectory's latency;
MIG and MPS must be measured per hardware/system, not inferred from an A100 blog.

## GPU placement and the full-node InfiniBand path

Pool names below describe the retained H100-region deployment, not universal
names or a guarantee of immediately free GPUs. The operator controls qualified
pool preferences; clients request a declared shape and inspect the admitted
pool/resources in operation status. They do not set a Kubernetes node name,
provider pool, transport or RDMA-device count in a public request.

| Requested work | Retained pool / evidence | Important boundary |
| --- | --- | --- |
| One GPU, independent `gromacs` job | L40S in `l40s-4x`; H100 in `h100-ondemand-1x` or a compatible full-node pool | The matched membrane workload favors the tested L40S recipe below; this is not a universal GPU ranking. |
| `gromacs-mpi`, `nodes: 1`, `gpus_per_node: 8` | One `h100-reserved-8x` node | Eight ranks advance one simulation. Native 3×1 ns median: 318.53 ns/day; not eight independent runs. |
| `gromacs-mpi`, `nodes: 2`, `gpus_per_node: 8` | Two compatible `h100-reserved-8x` nodes | Once the RDMA profile is published, the operator resolves this to `multi-node-8gpu-rdma`; an older profile resolves the same request to its legacy TCP shape. Inspect the frozen plan, not GPU count alone. |

The existing `h100-ondemand-1x` group has eight one-GPU hosts; its scheduling
metadata now matches that already-provisioned maximum and live Kueue quota.
Multi-node shapes require enough **distinct eligible hosts**, not just enough
aggregate GPUs. The current one-host L40S four-GPU pool cannot satisfy a
two-node gang. Configured maximum host count bounds feasibility; a temporarily
empty autoscaled pool may still admit work within its declared maximum.

For the InfiniBand path, Terraform's explicit `managed_rdma_pools` opt-in joins
the existing full-node pool to its exact provider GPU-cluster identity and
allocator-only RDMA deployment. It does not install a second GPU driver/OFED
owner. The two-Pod frozen shape requires eight GPUs and one eight-HCA bundle
(`rdma.fs2.nebius/hca`) **per Pod**, with the matching cluster label. Kueue must
account for both resource types together. Both Pods must use the compatible
reader/worker release and the pinned qualified external-MPI image; old GPU-only
placement facts cannot qualify RDMA. See the evidence's
[publication order](../../../acceptance/lynx-performance-20261005/RDMA.md#publication-order)
before activating the additive shape. No silent TCP fallback is accepted for
a run labelled `ucx-rdma`.

For the same membrane input, three native 1 ns runs on sixteen RDMA H100s gave
350.90 ns/day median, versus 318.53 on eight H100s: about 10.2% higher native
trajectory rate for twice the GPU allocation. These configurations differ in
rank layout/runtime/transport and are not an isolated InfiniBand speedup test.
The dated October 5 on-demand scenario is $72/hour for both full nodes, **not**
the actual reservation invoice, and excludes storage/network/idle capacity.
The native result is not public accepted-to-completed throughput. Final public
delivery, checkpoint durability and accounting still need their own receipt;
do not substitute the native benchmark for that gate.

## Measured membrane-workload tuning

The [2026-10-05 exact-input results](../../../acceptance/lynx-performance-20261005/FINDINGS.md)
qualify a specific 185,486-atom CHARMM membrane workload, not a universal default.
With eight OpenMP threads on one L40S, the tested `mdrun` arguments are:

```json
["-s", "production.tpr", "-deffnm", "production", "-nb", "gpu",
 "-bonded", "gpu", "-pme", "auto", "-update", "auto", "-pin", "auto",
 "-nstlist", "200"]
```

Use these as the native command arguments within an ordinary GROMACS workflow,
with `threads: 8`; REST and typed MCP accept the same workflow contract. The
tested TPR had positive Verlet-buffer tolerance `0.005`, so GROMACS adjusted
the neighbor-list buffer for the changed interval. Do not apply list tuning to
an unrelated protocol without checking that condition and its native log.
Force field, timestep, physical cutoffs, PME accuracy and output cadence stay
in the customer's TPR. Unsupported bonded offload or update settings are not
silently substituted by the platform.

Three 1 ns repeats through each interface delivered 201.0 ns/day over REST and
196.6 ns/day over raw MCP, including server startup/checkpoint/export time.
These are timing repeats, not independent scientific ensembles or an LLM-agent
qualification. The linked report retains exact commands, images, failures,
GPU allocation costs and comparison boundaries. More GPUs are not automatically
faster: the pre-InfiniBand two-node result was slower than one L40S. The newer
[RDMA native results](../../../acceptance/lynx-performance-20261005/RDMA.md)
are recorded separately; their higher single-trajectory rate does not yet
qualify the end-to-end public path or a cost-efficiency recommendation.

The [MPS comparison](../../../acceptance/lynx-mps-20261005/README.md) measured
aggregate gains for multiple independent simulations sharing a whole, task-owned
GPU, at higher per-simulation latency. It did not enable a public fractional-GPU
or MPS scheduling mode. Existing customer operations remain unchanged.

## Development and qualification

- `runtime/fs2_gromacs`: canonical schema, streaming files and native engine runner.
- `build_contracts.py`: regenerates identical REST/MCP schema projections and the
  unrouted candidate profile.
- `activation/workload-profile.json`: candidate only, not an availability claim.
- `qualification/`: reproducible public fixtures and task-owned GPU probes.
- `components/control-plane/.../adapters/gromacs.py`: existing batch integration.
- `gromacs_checkpoints.py`, `gromacs_storage.py`: companion recovery/export.

Native runtime tests alone do not qualify the hosted customer experience. Hosted
evidence now includes the exact packaged LibreChat CLI calling typed MCP, byte-
verified customer-bucket output, a controlled own-Pod eviction and automatic
restore, cancellation and six-job bursts. This is not a physical-node-loss test,
a browser/LLM-agent interaction test, an uncached new-node cold-start benchmark,
or evidence for arbitrary customer scientific protocols.

The client hashes and uploads source files with bounded memory, validates exact
finalized metadata, downloads four native files concurrently and retries only
transient read failures. The saved operation/receipt is the resume boundary;
transport recovery never silently resubmits GPU work. Inputs above the gateway's
16 MiB inline threshold use its existing presigned Object Storage path.

The current execution shape reserves a GPU for the entire job, including CPU
preparation, analysis and artifact I/O. Report occupied GPU time separately from
native simulation time; platform lifecycle `active_compute` is not a DCGM busy-
time measurement. A CPU-only analysis/preparation shape and scheduler-owned MPS
ensemble are follow-up optimizations, not currently released capabilities.

## Primary references

- [NVIDIA GROMACS container](https://catalog.ngc.nvidia.com/orgs/nvidia/containers/gromacs)
- [Versioned simulation/restart guidance](https://manual.gromacs.org/2026.2/user-guide/managing-simulations.html)
- [Native performance and offload guide](https://manual.gromacs.org/2026.2/user-guide/mdrun-performance.html)
- [Official free-energy tutorial](https://tutorials.gromacs.org/free-energy-of-solvation.html)
- [Heterogeneous parallelization](https://www.gromacs.org/topic/heterogeneous_parallelization.html)
- [NVIDIA MPS/MIG experiments](https://developer.nvidia.com/blog/maximizing-gromacs-throughput-with-multiple-simulations-per-gpu-using-mps-and-mig/)
