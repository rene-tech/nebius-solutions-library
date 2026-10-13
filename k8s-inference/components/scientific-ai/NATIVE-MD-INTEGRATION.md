# Native MD adapter seam — integration handoff

13 October 2026. Work on `scientific-ai-serverless2-migration`.
The root integrator applied packaging and runner dispatch below and built the
wheel. The adapter has not been deployed, registered or GPU-qualified on S2.
Existing native engine source and GROMACS execution semantics are unchanged.

## Implemented interface

`NativeMDRun(engine="amber" | "namd" | "lammps", job_id=..., input_bundle=...,
parameters=...)` retains each engine's existing typed native request. It rejects
unknown fields, multiple jobs, an unrelated job ID, platform-artifact output and
non-bundle input. One `NativeMDApplication` executes that one job.

`native_engines.py` loads the original `fs2_amber`, `fs2_namd` and `fs2_lammps`
packages lazily. A small worker subclass calls the original native checkpoint
method, then publishes synchronously through the same `Artifacts`, `Snapshot`
and `Lifecycle` objects used by GROMACS. It does not copy command construction,
scientific validation, segmentation, execution loops or a scheduler.
`committed_generation` advances only after S2 acknowledges the pointer.

`NativeMDApplication` reuses the current GROMACS application's upload, snapshot,
fencing and execution/delivery flow. Its overrides use engine-specific state
filenames, native completion checks and verified inventories. Existing
`publish_marker` / `retained_workspace` also work with this run contract; a CPU
finalizer never calls the native engine. Results retain the original worker
schema/engine identity and add actual `execution_image`, `inventory_complete`
and `native_committed_generation` fields. A transient final checkpoint failure
can enter delivery only after all native validators and the complete inventory
have passed. Denied, interrupted or incomplete work cannot become success.

## Shared-file integration (applied by root)

Package the existing Python runtime libraries, not another copy in this component:

```diff
--- a/k8s-inference/components/scientific-ai/pyproject.toml
+++ b/k8s-inference/components/scientific-ai/pyproject.toml
@@
 [tool.hatch.build.targets.wheel.force-include]
 "../../models/molecular-dynamics/gromacs/runtime/fs2_gromacs" = "fs2_gromacs"
+"../../models/molecular-dynamics/amber/runtime/fs2_amber" = "fs2_amber"
+"../../models/molecular-dynamics/namd/runtime/fs2_namd" = "fs2_namd"
+"../../models/molecular-dynamics/lammps/runtime/fs2_lammps" = "fs2_lammps"
```

The current protected binding and process entrypoint can dispatch by the typed
private run. No executable, shell command or image is accepted from a request:

```diff
--- a/k8s-inference/components/scientific-ai/src/scientific_ai/runner.py
+++ b/k8s-inference/components/scientific-ai/src/scientific_ai/runner.py
@@
 from .lifecycle import FileLifecycle
+from .native_md import NativeMDApplication, NativeMDRun
@@
-    run: GromacsRun
+    run: GromacsRun | NativeMDRun
@@
-            app = GromacsApplication(
+            native = isinstance(binding.run, NativeMDRun)
+            application = NativeMDApplication if native else GromacsApplication
+            options = {} if native else {"gmx": "/opt/gromacs/bin/gmx"}
+            app = application(
@@
-                gmx="/opt/gromacs/bin/gmx",
+                **options,
```

These snippets are now applied; run the complete component suite after integration.
The existing signal forwarding, current-checkpoint lookup, retained-workspace
authorization, private input, projected identity and delivery-only entrypoint
remain the integration boundary. Build a wheel and inspect the installed
packages before constructing any candidate worker image.

The gateway owner must separately register operator-owned per-engine S2
execution/runtime profiles, use the engine's unchanged native normalization,
bind `NativeMDRun` privately, and expose only those allowed model IDs. Runtime
base images must supply the exact existing binaries and dependencies (including
NAMD's psfgen/Colvars helper and AMBER's licensed PMEMD/AmberTools environment).
Use the same existing artifact, authorization, S2 submission, status, cancel and
storage broker paths. This source addition alone does not publish REST/MCP Apps.

## Native semantics and explicit bounds

| Engine | Preserved stage/continuation contract | Bounds not expanded here |
| --- | --- | --- |
| AMBER | PMEMD, tleap, cpptraj, ParmEd, Antechamber, parmchk2 and MMPBSA native schema/validators; completed-stage full workspace checkpoints | Active stage replays from the prior committed workspace; no automatic partial restart or exact stochastic-state claim; no PMEMD MPI |
| NAMD | `prepare`/`native` completion boundaries; `dynamics` coordinates, velocities, XSC and matching Colvars state at native segments | Existing GBIS/spinAngle exclusions remain; no serialized RNG or bitwise continuation; no multi-node execution |
| LAMMPS | Native scripts; optional explicit continuation script, closed restart and independently read native restart timestep | Unsegmented active scripts replay; no invented continuation or GPU-package/MPI support |

All three accept the existing H100/L40S single-GPU shape boundary. The immutable
profile must separately carry operator-approved runtime images. CPU native
stages can run within that allocation; CPU-only scientific placement, other GPU
classes, multi-GPU/MPI and multi-job fan-out are not implemented by this seam.
No precision, native scientific parameters or native execution budgets change.
AMBER/LAMMPS retain their existing 5 GiB per-file checkpoint bound.

Portable native restore is separate from GPU process snapshots. Same-digest
H100-to-L40S restore is supported by the adapter and covered with synthetic
files, including a fresh workspace on another declared cluster. This is not a
live hardware/scientific qualification. Different runtime image digests are
rejected even when their compatibility labels match; qualifying and explicitly
enabling cross-image interchange is follow-up work. Snapshot mechanisms are
neither requested nor introduced.

## Verification and remaining gates

Executed from `k8s-inference/components/scientific-ai`:

```sh
uv run --no-sync ruff check src/scientific_ai/native_engines.py src/scientific_ai/native_md.py tests/test_native_md.py
uv run --no-sync pytest -q tests/test_native_md.py
uv run --no-sync pytest -q
uv run --no-sync pytest -q ../../models/molecular-dynamics/amber/tests/test_contracts.py ../../models/molecular-dynamics/amber/tests/test_worker.py ../../models/molecular-dynamics/amber/tests/test_advanced.py
uv run --no-sync pytest -q ../../models/molecular-dynamics/namd/tests/test_runtime.py ../../models/molecular-dynamics/namd/tests/test_colvars_state.py
uv run --no-sync pytest -q ../../models/molecular-dynamics/lammps/tests/test_runtime.py
```

Results: lint passed; 105 adapter tests, 28 AMBER regressions, 20 NAMD regressions
and 14 LAMMPS regressions passed. The complete component suite collected at the
time of the run passed 313 tests with one explicitly enabled native test skipped.
Other agents are editing the shared checkout, so rerun it after integration.
Tests use synthetic bundles and explicit
process/storage/lifecycle doubles. They exercise the actual workers' stage
ordering, output checks, NAMD vector/XSC parsing and LAMMPS restart-step parsing;
they do not execute scientific binaries or establish GPU performance/capacity.

Still required: qualify installed
wheel and exact engine images; register authorized internal profiles; exercise
each advertised native workflow on the isolated scientific-ai2 fleet; prove
storage isolation, real native recovery and finalization; integrate corresponding
REST/MCP/admin behavior. No live qualification is claimed for any AMBER, NAMD or
LAMMPS stage by this source-only task. No customer API, identity, bucket, cluster
or existing native-runtime file was changed. Root integrator owns commit and
publication on the active migration branch, never automatic integration to main.
