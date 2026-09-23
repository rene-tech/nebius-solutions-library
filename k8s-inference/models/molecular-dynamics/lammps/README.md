# Native LAMMPS workflows

Candidate under qualification; not a customer-ready release. This directory owns
the LAMMPS worker, native examples, and runtime evidence. Shared REST/MCP,
admission, artifact transport, and tenant storage are integrated by the parent.

The runtime executes ordered native input scripts from one immutable gzip-tar
bundle per job. Include data, include files, force fields/potentials, and complete
continuation scripts. Native LAMMPS syntax is intentionally preserved; this API
does not add a shell-command or arbitrary executable field. The workload runs in
the platform's isolated unprivileged container without storage credentials.

## Checkpoint contract

Invocation: `python3 -m fs2_lammps.worker --request request.json --job-id replica
--operation-id UUID --workspace WORKSPACE --checkpoint-mode companion`.

Workspace inputs are `input.tar.gz` and `request.json`; scientific files live in
`data/`. The worker uses the existing streaming GROMACS file helpers. The companion
owns remote credentials and publishes the full stopped workspace by immutable
file hash, committing the manifest last. It restores `.fs2/lammps-state.json`,
then writes `.fs2/restore-complete.json` with `status: ready`. The worker writes
`.fs2/checkpoint-ready.json` containing `{state, files}` and waits for an exact
generation `.fs2/checkpoint-ack.json` with `status: committed`. Transport failures
use `.fs2/transport-error.json`. The final generation commits before `result.json`.

State and result bind operation, job, normalized request and pinned engine identity.
State schema: `fs2-serve.nebius.ai/lammps-checkpoint/v1`. Result schema:
`fs2-serve.nebius.ai/lammps-workflow-result/v1`.

A native binary restart does not reconstruct fixes, computes, variables, neighbor
settings, long-range electrostatics, potential coefficients or output definitions.
Continuation therefore requires an explicit full script and the entire workspace.
For opted-in segmented dynamics, scripts use `timer timeout`, `run TARGET upto`,
`write_restart` and a final integer progress file. Reserved variables supplied by
the worker are `fs2_segment_seconds`, `fs2_segment`, and `fs2_restart`. Segment
output names must preserve prior parts or use deliberate native append semantics.
The worker independently reads the native restart timestep before committing.

Other scripts checkpoint at completed stage boundaries. If the active stage is
lost, a replacement reruns it from the last committed workspace. This is at-least-
once stage execution, not exactly-once external effects. Abrupt cancellation keeps
the last committed generation; it does not invent a final native checkpoint.
Persistent CUDA process snapshots are a separate, currently unqualified mode.

## References

- [LAMMPS restart content and portability](https://docs.lammps.org/read_restart.html)
- [LAMMPS native timer timeout](https://docs.lammps.org/timer.html)
- [NVIDIA stable_22Jul2025 container](https://catalog.ngc.nvidia.com/orgs/nvidia/-/containers/lammps/stable_22Jul2025/tags)
