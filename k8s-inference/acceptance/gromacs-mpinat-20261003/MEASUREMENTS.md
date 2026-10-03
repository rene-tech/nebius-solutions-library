# MPINAT campaign measurement contract

The authoritative operation/attempt state remains in the platform's PostgreSQL
API. This campaign retains raw API documents, native logs, committed checkpoint
manifests and checksums, Kubernetes observations and sampled counters alongside
it. Evidence is keyed by operation ID, stage/shard (replica/window), attempt ID,
native command/segment and phase. Never aggregate away a failed attempt.

## Units and progress

- Particle count comes from the native TPR/log; timestep is in ps. Preserve the
  original TPR hash and the finite-length derived TPR hash separately.
- Requested steps are the requested interval, not native log lines. Executed
  steps include uncommitted work. Where a fatal error reports a step, retain it
  as an observed reached-step boundary, not a successful completion certificate.
- Durably completed MD steps require a valid native checkpoint at that step,
  referenced by a committed generation; successful scientific delivery additionally
  requires native/artifact validation. Workflow command counts are not MD steps.
- Keep resumed intervals `(start_step, end_step]` for every attempt. Useful
  progress is their union **within the same logical replica/window**. Summing
  overlapping retry intervals would double-count. Intentional timing repetitions
  have separate IDs and are not independent scientific samples.
- Native ns/day and ms/step describe simulation throughput. Allocation-to-release
  GPU seconds describe resource occupancy; neither substitutes for the other.

## Required raw evidence

| Group | Retained sources |
| --- | --- |
| Protocol | Original TPR, native input-parameter log/MDP, all commands: force field identity if recoverable, constraints, cutoffs, PME order/grid/tuning, integrator, dt, FE mode/lambda vectors, seeds, output frequencies |
| Shape | Pod/attempt IDs, exact image IDs, requested/limited CPU and RAM, actual GPU UUID/SKU/count, nodes, MPI ranks/threads, device and CPU topology |
| Lifecycle | Public operation events and timestamps; Pod creation/binding/container start/exit/deletion observations; native command finish time and monotonic duration |
| Phases | Staging, native initialization, simulation, checkpointing, analysis and export as distinct spans where available; never silently classify unresolved gaps as simulation |
| Efficiency | Native performance records, 10 s GPU utilization/memory/power samples, cgroup CPU time and memory, metrics-server samples |
| Recovery | Every attempt/failure/preemption; native checkpoint generation and step range, upload/validation results, resumed/repeated work |
| I/O | Input and artifact byte counts and hashes, checkpoint objects and inventory, retained object bytes, measured upload/download spans |
| Interface | REST request/receipt/polling vs actual agent/installed-skill/MCP trace; exact client image, instructions hash, tool timings and all operation IDs |

## Measurement quality

Every derived field must carry a source and quality: `measured`, `derived`,
`sampled`, `bounded`, `unknown` or `not_applicable`. Unknown numeric values are
null, never zero. GPU utilization is sampled, not an exact duty-cycle integral.
Pod binding is a scheduler allocation boundary, not the CUDA first-use instant;
deletion polling bounds resource release but does not establish its exact time.
Reported native command wall time includes native initialization. Separating it
requires explicit native timestamps; do not infer it from the first utilization
sample. Overlapping checkpoint upload/export spans must not be summed twice.

Native checkpoints (portable application state) are not CUDA process snapshots.
A content-hash-verified diagnostic file is not validated scientific output.
TPRs preserve executable force-field parameters but may omit a trustworthy
human-readable force-field name: keep identity unknown rather than guessing.

## Baseline correction and scope

REST-A used the upstream `-resethway` example and 12/16 cases failed because
PME tuning was still active at step 5000. Preserve that cohort. Corrected runs
remove only the forced timer reset and keep the physics and tuning enabled.
Their timing **includes warmup**, not post-tuning steady-state throughput.
Do not mix these timing definitions in one average. Longer, safely timed
steady-state runs are a separate cohort.

`observe.py` only reads system-tenant GROMACS/GROMACS-MPI Pods. Raw sampling
started after some early REST-A jobs, so missing early samples remain unknown.
`recover_bucket.py` verifies existing checkpoint/native files without mutating
the bucket or rotating credentials. No customer key or Pod is used.
