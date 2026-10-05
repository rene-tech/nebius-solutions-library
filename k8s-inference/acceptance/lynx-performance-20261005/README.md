# Exact-input MD performance qualification — 2026-10-05

This task benchmarks an operator-approved private copy of the actual Lynx
185486-atom CHARMM membrane TPR. The input and all molecular outputs stay outside
Git. Immutable TPR SHA256:
`e2ee73571f0dd9855709d2a957e41e5ad52316b3f1d2b1808e65476f4bd8ef10`.

The running customer continuation is never stopped or changed. The private
source copy was made read-only from its exact TPR; QA uploads and execution use
the existing `system/qa` identity and its existing bucket. No customer key,
customer output prefix, admission-policy change or pool override is used.

## Protocol and claim boundaries

- The original 2fs timestep, 310K protocol, force field, constraints, PME accuracy,
  output cadence and initial coordinates/velocities are retained. Only
  `convert-tpr -nsteps` creates an explicitly finite benchmark copy. Repeats
  restart the same state: they measure timing variability, not independent
  molecular ensembles.
- Screen 50000 steps (100ps); confirm promising candidates with three 500000-step
  (1ns) repeats, reaching the original trajectory-output interval. No forced counter reset:
  native counters include tuning and are not a pure GPU-compute clock.
- Single-GPU control: eight requested vCPUs, native auto offload. Change one
  factor at a time: bonded task placement, thread count, explicit affinity,
  neighbour-list construction. A list-frequency screen must retain the positive
  original Verlet-buffer tolerance and native automatic buffer-adjustment proof.
  Never change physical cutoffs or energy/trajectory output frequencies for speed.
- MPI comparisons explicitly use CPU update: this TPR's nonconsecutive update
  groups do not support domain-decomposed GPU update. CPU/GPU PME fixed controls
  disable PME tuning and remain separate from auto-tuned controls. GPU PME uses
  one PME rank at multi-rank shapes; the current runtime has no multi-GPU FFT.
- Test 1/2/4/8 local ranks and 2x8 if the exact free-node check passes. Cross-node
  transport is host-staged TCP; these nodes have no allocated RDMA fabric.
  More GPUs is not assumed to reduce time or cost.
- Native Pod probes test only the pinned engine/resource recipe. REST and raw
  MCP terminal success, downloaded hash-verified native output, replay identity,
  frozen plan and durable allocation evidence are separate gates. Raw MCP is not
  an actual-agent/LibreChat qualification.

## Helpers

`recipes.py` prepares deterministic private bundles and bounded public request
parameters. `native_probe.py` reuses the retained native Pod supervisor's fresh
capacity check, immutable image resolution, actual UID ownership and exact Pod
cleanup; adds Lynx-specific finite energy/coordinate/repeat continuity checks.
`observe_native.py` samples only this task's labelled native Pods, never the
customer Pod. `run_public.py` uses the prior durable campaign/transport verifier
without its policy-changing path. One owned API operation at a time, optionally
beside one explicitly coordinated peer; the two-slot QA limit remains unchanged.

`native_cpu_probe.py` reuses the retained native CLI harness for an explicitly
requested 8/16/32-CPU Pod with one GPU, fresh CUDA cache plus three warm runs.
This is an operator-only envelope experiment, not a new public resource shape;
all public recipes retain the existing eight-CPU-per-rank envelope.
`capture_admission.py` captures only the selected system operation's live Pod
and Kueue resources, checking per-Pod GPU requests and total gang reservation
against its immutable plan. Run while objects still exist; missing evidence is
not a zero reservation. `validate_public.py` reuses the same finite-output
validator over already downloaded, rehashed public artifacts without copying
trajectories or making another API call.

For long segmented recipes, `trjcat` combines every retained trajectory part
before `check`; the original 1ns output cadence and all original parts remain
unchanged. Both commands are measured analysis work, not integration.

`collect_report.py` creates one fresh, explicitly selected terminal snapshot
using the existing MPINAT `Ledger` and `cost_report` implementation. It copies
small case metadata and a selected complete observer-file prefix at a fixed
cutoff, rehashes original output references, and indexes no other QA operation.
The approved recovery helper accepts the additional exact internal prefix
`runs/fs2-lynx-performance-20261005-`; its old default and QA identity guard
remain unchanged. Recover terminal cases first, without copying trajectories:

```bash
components/control-plane/.venv/bin/python acceptance/gromacs-mpinat-20261003/recover_bucket.py \
  --qa-env "$LYNX_QA_ENV" --cohort "$LYNX_TERMINAL_COHORT" \
  --output "$LYNX_RECOVERED_COHORT" \
  --workspace-prefix runs/fs2-lynx-performance-20261005-
components/control-plane/.venv/bin/python acceptance/lynx-performance-20261005/collect_report.py \
  --selection "$LYNX_EXPLICIT_SELECTION" --telemetry "$LYNX_OBSERVER" \
  --references acceptance/lynx-performance-20261005/cost_references.json \
  --output "$LYNX_NEW_REPORT_DIRECTORY"
```

The private selection has `public` entries containing absolute `cohort` and
`recovered` paths plus `interface` (`REST` or `MCP`). Optional `native` entries
contain `receipt`, `fixture` and protocol/background `notes`; native probes are
not charged as public operations. Existing snapshots are never overwritten.
Only verified, single-attempt checkpoint histories with exact remote commit,
recipe, input and native step continuity can fill durable-step measurements.
Multiple-attempt lineage stays unknown until explicitly proved.

The bounded PostgreSQL sidecar queries only the selected internal operation
UUIDs. It keeps reconciled scheduler GPU-seconds separate from observation
bounds and applies the existing dated per-GPU allocation-share price exactly
once, including multi-node shapes. Missing attempts or mixed unknown prices
remain null. Server accepted-to-completed throughput excludes input upload and
client artifact download; the latter is unknown unless separately timed. No
generic top-level reserved-GPU zero is interpreted as zero cost.

`summarize_samples.py` reports observed per-device utilization/power/memory
distributions and per-container CPU counter deltas from that same frozen
observer prefix. Irregular sample means include preparation and analysis;
they are not a utilization integral or native-only time. Counter resets and
unavailable readings remain explicit, and aggregate cgroup throttling is not
converted to a lost-work percentage. Retained S3 object bytes, input bytes and
SDK-verified artifact bytes remain distinct from unknown total transferred
checkpoint/export bytes.

Example preparation (private paths intentionally supplied by the operator):

```bash
components/control-plane/.venv/bin/python acceptance/lynx-performance-20261005/recipes.py \
  --tpr "$LYNX_PRIVATE_INPUT" --output "$LYNX_PRIVATE_FIXTURE" \
  --steps 500000 --repetitions 3 --bonded gpu
```

Private evidence root:
`/home/tux/secure-handoff/fs2-lynx-performance-20261005`.
Native workers are initially pinned to the existing single digest `f633539e…`
and MPI digest `938cc612…`; actual manifest/image IDs are in each receipt.
API release identity may change only through the parent release owner; never
roll back a reader that understands the longer continuation plans.

Report native and operation-delivered ns/day separately, actual GPU allocation
seconds, failed allocation, CPU throttling, sampled utilization, transfer bytes
and known phase clocks. Missing checkpoint/export phase clocks remain null;
do not subtract presumed compute from wall time and invent a phase duration.
Three repeat energy/coordinate checks are not ensemble convergence evidence.

## Primary references checked 2026-10-05

[GROMACS performance guidance](https://manual.gromacs.org/2026.2/user-guide/mdrun-performance.html)
supports measuring bonded offload on CPU-constrained/lipid-heavy systems,
explicit affinity and neighbour-list tuning while keeping Verlet accuracy.
[Native mdrun options](https://manual.gromacs.org/2026.2/onlinehelp/gmx-mdrun.html)
document the exact placement/affinity controls. The installed engine's own
version/help and actual dispatch logs remain the compatibility authority.
[Nebius pricing](https://nebius.com/prices) lists H100 on-demand $4.50/GPU-hour
effective October 1, 2026, and L40S starting prices that vary by CPU/RAM preset.
Use the actual node's full preset allocation share in final costs; these are
dated estimates, not an invoice, reservation consumption or pure GPU time.
