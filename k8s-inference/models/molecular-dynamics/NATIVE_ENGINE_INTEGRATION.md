# Native MD App integration

Updated 2026-09-23. This describes implementation and release mechanics, not a
claim that every engine or scientific workflow is publicly qualified.

## One platform, engine-specific science

LAMMPS and NAMD reuse the existing scientific-batch admission, PostgreSQL run
state, Kueue placement, companion, artifact transport and customer-bucket export.
They do not introduce another queue, authentication system or storage credential.
The submitting key identifies the customer bucket; request fields cannot select
a different customer's bucket.

`scientific_batch/native_workflows.py` is the fixed registry joining model IDs,
parameter schemas, collectors, runtime packages and checkpoint media types.
`adapters/native_md.py` contains shared bundle/stage/result handling. Engine
packages own their scientific normalization, native commands and continuation.
The retained GROMACS adapter and existing internal storage route remain valid.
Other registered engines use `/internal/scientific-workloads/native/storage`.

Only closed, manifest-verified generations are uploaded and acknowledged. A
worker failure can replay work since the last committed generation; an open
trajectory is not a durable checkpoint. Final result collection requires the
correct operation/job/recipe, completed steps and exact file inventory. Native
checkpoints are not CUDA process snapshots and do not establish convergence.

Independent jobs each request one GPU; the shared queue bounds simultaneous
execution. This is not multi-node execution of a single LAMMPS/NAMD simulation.
Users retain control of force fields, units, ensembles, seeds, timesteps and
output cadence in their native files. The platform must not change scientific
settings merely to obtain a passing test or better throughput.

## Packaging and contracts

The backend image includes the lightweight `fs2_lammps` and `fs2_namd` contracts,
not their GPU binaries. Worker images derive from pinned NVIDIA NGC HPC images.
These distributions are not presumed to provide an NVIDIA NIM HTTP service.

Generate the public parameter schema and unrouted candidate profile from the
worker's actual contract using the control-plane virtual environment:

```sh
.venv/bin/python ../../models/molecular-dynamics/build_native_contracts.py --model lammps
.venv/bin/python ../../models/molecular-dynamics/build_native_contracts.py --model namd
```

Use `--check` in validation. Regenerate after changing an engine package. The
generated profile is deliberately `candidate-unqualified` and is not added to
the routed execution map just by generating files or registering a compiler.

## Evidence-bound release

1. Capture the current Helm values, scheduling configuration and backend
   deployment with the existing `gromacs/qualification/capture_live.py` helper.
   Store its private values outside Git.
2. Complete substantive repeated engine tests on the **exact worker digest**.
   Keep failed cases and superseded-image results separately. Validate native
   restart state, every promised output and complete trajectory cadence.
3. Supply an engine receipt with `model_id`, immutable `runtime_image`,
   `recorded_at`, `customer_ready: false`, and `tests`. Each successful test
   identifies `case`, `pool`, `gpu_name`, `driver`, `status`, `input_sha256`,
   `result_sha256`, `validation_sha256` and raw evidence paths. The composer
   enables only pools represented by successful tests; it does not extrapolate
   H100 evidence to another GPU. Pool identifiers must match the scheduling map.
4. Run `prepare_native_release.py --baseline ... --runtime-receipt ... --output ...`.
   Repeat the receipt option for both engines. It preserves other Apps, quotas,
   snapshot bundles and qualification baselines, and generates additive changes.
   `--publish-catalog` additionally checks the repository execution map against
   the captured baseline. It is not a cluster write or customer acceptance test.
5. Build the backend from committed source using its normal image build helper.
   Recheck the live baseline, then use the existing guarded Helm release helper.
   Drain affected operations before changing a previously published runtime's
   identity; terminal provenance currently depends on that profile identity.
6. Exercise ordinary-key typed MCP, real file upload, queueing, result retrieval,
   customer-bucket restoration, cancellation/retry, bounded mixed batches and
   the actual LibreChat skills. Retain two clean cohorts on unchanged images.
   Only this evidence can support the customer-ready decision.

## Known limits and pending work

- AMBER's agreement is operator-confirmed. No public AMBER NIM was identified
  in the accessible NVIDIA `hpc`/`nvidia` catalogs. Its licensed installer or
  private image reference and version are still required. AmberTools is not a
  substitute for licensed GPU PMEMD. A transport descriptor is not an AMBER App.
- NVIDIA's pinned LAMMPS build exposes KOKKOS CUDA plus Serial, not the separate
  GPU package. A native binary restart alone does not recreate all fixes,
  computes, variables or output definitions; continuation needs complete input.
- NAMD 3.0.2 is the selected NGC binary, not proof of later upstream fixes.
  GBIS and Colvars spinAngle are not qualified on this binary. Sustained tests
  found lost explicit metadynamics hills across an ungridded restart; that path
  remains failed until a native state round-trip and repeated trajectories pass.
- Persistent fresh-worker GPU snapshot restore remains unqualified for these
  new engines. Native restart success must not set a GPU-snapshot readiness flag.
- Files over 5 GiB and single-simulation multi-node execution are not qualified
  for these new Apps. Workspace/output budgets do not increase customer quotas.

## Integration validation to date

On the integration source, the scientific/GROMACS/native-MD non-PostgreSQL test
cohort passed **1,087 tests**, with 16 skipped and 46 deselected (2026-09-23).
Database-backed tests were not part of that run. Terraform validation and format
checks passed; Helm lint passed with the captured complete values and the
`fs2-system` namespace. These are offline integration checks, not production or
customer-science acceptance.
