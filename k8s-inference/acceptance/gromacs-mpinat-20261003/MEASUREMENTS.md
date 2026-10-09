# MPINAT campaign measurement contract

Current results, deployment identity, blockers and resume instructions are in
[STATUS.md](STATUS.md). The measurement contract is broader than the currently
instrumented coverage; unknown fields are explicitly retained as such.

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

`ledger.py` builds `measurements.sqlite`, an ordinary indexed SQLite analytical
database with operation/attempt/command/checkpoint, lifecycle-event, allocation,
measurement, observation and source-digest tables. It is rebuildable from the
raw evidence; platform PostgreSQL remains authoritative. Keep the original files
with it. This is not an LLM-generated scorecard or a new production billing store.
Each retained native log version has a hash-addressed local copy. Checkpoint
generations are indexed separately instead of replacing them with the last one.

HTTP spans omit credentials, headers and URL query strings. `seconds_to_headers`
is exactly that interval, **not** download completion time. Bucket diagnostic
recovery separately times full downloads and verifies size/SHA-256. A failed
network call retains a start span even if no response arrives.

Example (private evidence paths):

```sh
python3 ledger.py --cohort /path/rest-b/cohort-1 --recovered /path/bucket-b \
  --telemetry /path/telemetry --database /path/measurements.sqlite
```

Still not directly instrumented in the original engine: exact GPU device-plugin
release, native initialization vs integration, and checkpoint/export subspans.
Their raw lifecycle/container/native observations are retained, but unresolved
fields remain explicitly unknown. Do not advertise complete exact phase timing
or billing reconciliation until those gaps are closed and recovery is exercised.

## Retained phase measurements now indexed

The offline parser/report adds these explicitly named fields without changing
the deployed runtime or relabelling lifecycle `active_compute`:

| Field | Evidence and limits |
| --- | --- |
| `init_container_process_seconds` | Kubernetes init-container `startedAt`/`finishedAt`, at its recorded timestamp resolution. Includes interpreter startup, downloading, validation and extraction; not transfer-only. |
| `native_mdrun_counter_wall_seconds` | One unambiguous GROMACS `Core t (s) / Wall t (s)` table in the hash-verified command log. Uses Wall t, not summed CPU core seconds or rank times. Native counter scope, **not pure GPU compute**. |
| `mpi_input_staging_seconds` / `mpi_input_staging_bytes` | Retained coordinator stager monotonic wall and sum of per-peer transferred bytes. The timer precedes mdrun; parallel peer durations are not added. An explicit empty peer list is measured zero bytes; absent instrumentation is unknown. |
| `analysis_command_seconds` | Monotonic process wall for retained `energy` and `eneconv` commands. Usually CPU work while GPUs remain reserved. |
| `post_stage_collector_tail_bounds` | Worker exit to collector termination when captured; otherwise last collector-running observation is a lower bound and first Pod disappearance is an upper bound. This excludes overlapping checkpoint/export before worker exit and is not total export time. |

`allocation_phases` and `command_phases` retain the per-Pod/per-command rows;
attempts and operations contain `phase_accounting` observed sums, known/unknown
record counts, modelled reserved GPU-seconds and dated allocation-share costs.
These are **not a complete, mutually exclusive timeline**. Native counter time
is nested inside mdrun process wall; do not sum both. No utilization or invoice
is inferred from GPU reservation. Unknown initialization/integration-only,
checkpoint and total-export durations remain null. Incomplete attempt telemetry
still prevents a complete operation occupancy cost.

Completed native result artifacts can fill gaps when checkpoint recovery has
not run yet. The parser rechecks result/log hashes and input-operation identity.
With multiple attempts, commands copied into the final result are not simply
assigned to the latest attempt. Missing attribution remains unknown.

For MPI, save case-local `frozen-plan.json` from the exact admitted plan:

```json
{"operation_id":"UUID","tenant_id":"system","model_id":"gromacs-mpi",
 "plan":{"stages":[{"stage_id":"workflow","mode":"gang-jobset"}]},
 "captured_at":"UTC timestamp","source":"postgresql-admitted-plan"}
```

The capture helper uses a bounded read-only PostgreSQL selection of task receipt
IDs; no credentials or whole durable state are needed. Only frozen
`mode=gang-jobset` permits mapping public attempt `shard_id=null` to the sole
native job ID. The original public shard stays in raw attempt evidence.
An independent job literally named `gang` remains literal. Missing/ambiguous
frozen mode is not guessed from the job name or GPU shape.

## Explicit cross-operation retry lineage

`cost_report.reviewed_retry_map` constructs only the reviewed REST-B→C MEM/RIB
and REST-B→C→matrix REST-D PEP chains. REST-B PEP was cancelled, not failed;
its actual state is retained. It streams hashes of the original TPR and bundle,
compares every request parameter except output namespace and output byte budget,
and creates twelve separate groups (`repeat-1/2/3` for four systems). It rejects
MPI shapes, changed protocols, missing inputs and duplicate operation identities.
Neither REST-A forced-reset experiments nor purposeful PME/shape controls are
merged. Intentional timing repeats remain separate logical work.

Apply the saved map explicitly with `cost_report.py --retry-map PATH`. A
before-start failure may have no native log forever. Only with this reverified
original-TPR/request proof can a logical group obtain static atom count, dt and
initial step from another retained repeat of those exact operations, all proved
to start from the same finite TPR. Donor IDs and filled fields are reported;
individual measured rows stay unknown. No executed steps, checkpoint interval,
native time or rate is transferred. Conflicting or wholly absent input metadata
still fails closed. Each operation's allocation cost is counted once across its
three logical repeats.

## Smallest remaining instrumentation hooks (not deployed)

1. Record bounded UTC start/end plus monotonic duration around worker setup,
   checkpoint inventory/ack waiting and final inventory; attach operation,
   attempt, native job, generation and an allowlisted phase name.
2. In the companion, separately time restore download/verification, native
   checkpoint publication, customer-object transfer and final manifest commit;
   retain byte counts and success/failure, never keys or signed URLs.
3. Correlate nested/parallel spans by generation and attempt; do not add peer
   staging or overlapping checkpoint/export spans as if serial. Distinguish
   coordinator and peer Pod reservation.
4. Test retries, partial failures, cancellation, zero-byte reuse and duplicate
   observation. Requalify the exact modified runtime before changing live images.

Until those hooks exist, subtracting native Wall t from process wall does not
measure initialization; assigning all post-worker time to export is also wrong.
