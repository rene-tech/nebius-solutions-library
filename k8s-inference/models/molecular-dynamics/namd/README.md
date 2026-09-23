# NVIDIA NAMD native workflows

Implementation candidate; customer qualification is incomplete. The wrapper
uses the existing Scientific AI durable-job, checkpoint and tenant-storage
interfaces. This NVIDIA artifact is an HPC container, not an HTTP NIM service.

The operator confirmed coverage of this NVIDIA distribution under their
agreement on 23 September 2026. This records operator confirmation, not an
independent contract review. No additional agreement was accepted. Preparation
uses `psfgen` already bundled in the selected container; VMD and separately gated
upstream binaries are not added.

## Exact runtime

Selected `nvcr.io/nvidia/namd:3.0.2`, amd64 manifest
`sha256:e1ebab672b968e0b287ba91c3dc19cdad9b693e8b59e74d7782cb753d6a7460f`.
The user-linked `hpc/namd` namespace's latest actual tag is 3.0.1. Both namespace
tag lists were resolved through the existing authorized cluster registry path;
the workstation's registry-root HTTP 403 does not establish missing entitlement.

The binary reports NAMD 3.0.2, multicore Charm++ 8.0.0, CUDA 12.9, built
8 December 2025. The included build recipe contains SM80, SM86, SM90, SM100,
SM103 and SM120 targets plus compute_120 PTX. This recipe is build metadata;
per-GPU runtime qualification remains required. H100 and L40S both use driver
580.173.02 in the retained qualification. The exact r4 worker passed three
400 ps ApoA1 NVE/NPT/ungridded-metadynamics repetitions per case on L40S;
additional matched controls remain in progress. These results do not imply
qualification of every mode, a hosted L40S path, or a peak GPU-only comparison.

No 3.0.3 correctness backport was found. This build predates the July 2026 fixes.
GBIS and Colvars spinAngle are rejected by effective-option compatibility guards.
The guards are not a sandbox for arbitrary Tcl and do not qualify all other
features. A fixed NVIDIA artifact is required before advertising affected
extended-Lagrangian/spinAngle or GBIS workflows. Multi-node execution is not
offered by this single-node build/profile.

## Workflow contract

One immutable gzip-tar bundle contains all native config/Tcl, topology,
coordinates, force-field/parameter and optional restart files. Each independent
job has ordered steps and an isolated workspace. The canonical JSON Schema is
`runtime/fs2_namd/contracts.py`; REST/MCP projections are parent-owned.

- `prepare`: bundled native `psfgen` executes a Tcl preparation script. The
  scientist supplies compatible topology and parameter files.
- `native`: a complete NAMD Tcl program, including minimization, staged
  equilibration, analysis callbacks or native run control. Explicit expected
  output filenames are required. Recovery is at completed workflow boundaries;
  arbitrary Tcl control-flow/RNG state is not serialized.
- `dynamics`: a configuration-only Tcl script plus finite `steps`. The wrapper
  runs `segment_steps`, closes the process, validates binary coordinate/velocity
  vectors and XSC timestep, and commits all files before continuing. GPU resident
  or offload mode is explicit and must agree with the native configuration.

Managed configurations omit `run`, `minimize`, `startup`, `numsteps`,
`benchmarkTime`, `outputName`, `restartName`, explicit DCD/XST filenames and
`firsttimestep`. The wrapper owns those controls. Output cadence, timestep,
ensemble, constraints, force fields, seeds and scientific parameters remain in
the supplied configuration. Use `if {!$fs2_restart}` for fresh `temperature` and
cell initialization. Restart configurations restore `.coor`, `.vel` and `.xsc`;
enabled Colvars also requires matching `.colvars.state`. An explicit
`initialize_colvars: true` introduces a new bias at a new stage from an unbiased
checkpoint, while later segments restore that bias state.

The shipped Colvars 2024-06-04 (patch 1) ungridded-metadynamics state writer
omits the `keepHills` state marker that its reader needs to retain old explicit
hills. The original r3 campaign detected native "success" with a lost bias
history and is retained as failed scientific qualification. Managed continuation
now preserves the original bytes and derives a version-scoped restart copy with
only that missing state marker added. It records both hashes and the bias names.
After native startup, `cv savetostring` captures the loaded state without changing
output prefixes; every explicit metadynamics hill's step, weight, center and width
is compared before dynamics may advance. No scientific configuration, grid
choice, force field or seed is substituted. Unknown repair formats fail with
the original inputs preserved. This is a serialization compatibility repair,
not an upstream engine patch or qualification of all Colvars methods.

Each managed segment writes `prefix.partNNNNNN.*`. All DCD, XST, log and bias
parts are retained. Final coordinate, velocity, cell and bias state aliases are
also copied to `prefix.*` for the next explicit stage. Trajectories are not
silently concatenated or replaced. The input inventory is hashed and rechecked
between stages and on restore; modifications require a new workflow.

Native restart is a scientific continuation, not bitwise preservation of every
stochastic thermostat state. No independent-replica RNG or persistent CUDA
snapshot claim follows from restoring one native checkpoint.
Effective native seeds and first timesteps are recorded for each process. The
wrapper does not silently rewrite or prohibit scientist-selected seeds. The
qualification inputs explicitly choose `314159 + $fs2_first_step` per managed
segment; this avoids intentionally reusing a fixed segment seed in those tests,
but is not serialization of the thermostat RNG. Performance repetitions use
the same schedule, not a claim of independent scientific replicas.

## Existing transport interface

`python3 -m fs2_namd.worker --request <json> --job-id <id> --operation-id <op>
--workspace <dir> --checkpoint-mode companion`

The worker shares the existing `input.tar.gz`, `data/`, `.fs2/` and `result.json`
layout. `.fs2/namd-state.json` uses `namd-checkpoint/v1`. The existing companion
handshake uses `restore-complete.json`, `checkpoint-ready.json`,
`checkpoint-ack.json` and `transport-error.json`. The recipe hash includes the
normalized request, job ID and pinned engine identity. No credentials enter the
simulation container. Publication stops execution until the same generation is
committed, including the final generation before a successful result.

Cancellation recovers the previous acknowledged generation; no final post-cancel
checkpoint is promised. Native Tcl is executable user code and requires the
platform's nonroot worker isolation, no service-account or storage credentials,
network isolation, bounded scratch and resource limits.

The aggregate workspace maximum is 48 GiB within the selected execution shape;
this does not increase tenant quota. Individual files above 5 GiB remain
unqualified through both artifact paths, consistent with the GROMACS contract.

## Build and qualification

Build from `k8s-inference/models/molecular-dynamics` with
`docker build -f namd/runtime/Containerfile ... .`. The regional base mirror can
be replaced with the same pinned upstream manifest via `NVIDIA_NAMD_IMAGE`.
The wrapper reuses the reviewed GROMACS streaming archive/inventory helpers.

Run local contract/format tests with `pytest -q namd/tests` from that directory.
`qualification/make_fixture.py` produces output-enabled public ApoA1/STMV
multi-stage inputs, recording original archive and derived bundle hashes.
An explicit `--ensemble colvars --colvars-grid` fixture is a separate gridded
radius-metadynamics protocol, with 0–20 Å bounds, 0.2 Å bins, `keepHills on` and
PMF output. It never replaces the original ungridded fixture or its failed r3
evidence. These controls follow the [native grid/state options](https://colvars.github.io/namd-3.0/colvars-refman-namd.html).
The qualifier compares every saved grid field/value across the native restart
and reads finite PMF samples, without claiming free-energy convergence.
`qualification/run_campaign.py` runs independent repetitions sequentially on one
GPU and retains all outcomes and 1-second telemetry. These are single-trajectory
measurements; no MPS daemon is started, and NVIDIA aggregate MPS results are a
different experiment. Short screens are development evidence only.

For downloaded hosted artifacts, retain each native `result.json` beside its
`data/` tree under `campaign/rep-N/`. Run the scientific reader with
`PYTHONPATH=namd/runtime:gromacs/runtime python3 namd/qualification/validate_campaign.py
--campaign <campaign> --output <validation.json>` from the molecular-dynamics
directory. It reads every production DCD frame, finite energy/cell/vector data
and this fixture's full explicit-hill history across segments.

Independently audit each downloaded result with `qualification/audit_outputs.py
--request <actual-workflow-parameters.json> --result <result.json> --data <data>
--input-bundle <input.tar.gz> --output <artifact-audit.json>`, using the same Python path. Pass the actual
submitted workflow parameters, including any changed output prefix, not the
surrounding REST envelope or a pre-submission example. The audit checks the
canonical recipe, exact completed stages, every inventoried file's size/SHA256,
segment boundaries/GPU mode, required outputs and final paired native timesteps.
The optional input-bundle comparison checks every original immutable member
against the downloaded files without extracting or executing the archive.
This is qualification tooling; it adds no runtime restriction on user physics.

`qualification/capture_hosted.py` records only the selected operation's filtered
Pod/image/resource identities and a read-only GPU query. It excludes environment,
arguments, annotations, secrets and customer payloads. `hosted_receipt.py` binds
the downloaded scientific and inventory audits; the released client and parent
release receipt remain responsible for authentication, transport and deployment
provenance. Use the exact submitted parameters, including a transport-only
`request-transport.json` when the client selects platform-managed artifacts.

`qualification/inspect_warnings.py` retains every warning line with log hashes
and a narrow PSF inventory. Classification does not suppress warnings or approve
a release. Explicit experimental options in the source benchmark inputs and
their limitations are documented in the qualification report.

Native worker tests do not establish REST/MCP, admin, tenant storage, customer
skills, interruption recovery or persistent fresh-worker GPU snapshots. Those
release gates remain separate and must be exercised on the integrated identity.

## Primary references

- [NVIDIA NAMD catalog](https://catalog.ngc.nvidia.com/orgs/nvidia/containers/namd)
- [User-selected HPC namespace](https://catalog.ngc.nvidia.com/orgs/hpc/containers/namd)
- [NAMD 3.0.3 correctness release notes](https://www.ks.uiuc.edu/Research/namd/3.0.3/announce.html)
- [Native Tcl/configuration and output commands](https://www.ks.uiuc.edu/Research/namd/3.0/ug/node9.html)
- [Colvars state and configuration](https://colvars.github.io/namd-3.0/colvars-refman-namd.html)
- [Public scientific benchmark systems](https://www.ks.uiuc.edu/Research/namd/benchmarks/)
