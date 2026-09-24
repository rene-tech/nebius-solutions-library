# Native dense continuation — NAMD and LAMMPS

Both hosted continuations passed on 24 September 2026. These are **new native
20 ps simulations**, not recovered frames, interpolation, or another 1 ns
campaign. They continue the frozen 6,598-atom ff14SB/TIP3P `delivery-02` final
checkpoints without changing its files, timestep, physical settings, deployed
images, quota, or resource configuration. Rendering and publishing are separate
parent-owned work.

| Engine | Original/final step | Positive native frames | Customer artifacts | Operation |
| --- | --- | --- | --- | --- |
| NAMD | 605000 → 615000 | 1000, every 10 steps / 20 fs | 30 | `f88b4e14-9ae0-4930-b314-23e59f8591a1` |
| LAMMPS | 600000 → 610000 | 1000, every 10 steps / 20 fs | 19 | `199127a9-c7be-4a57-b99c-c58c29499bce` |

Each operation used one H100 from the normally admitted `h100-ondemand-1x`
pool, sequentially. Native timestep is 2 fs. Both attempts succeeded and report
`resource_released:true`; final exact-operation Pod queries returned no Pods.
No additional native run was needed after either first attempt.

## Exact output and renderer handoff

Evidence root: `/home/tux/fs2-alanine-videos-20260924/dense`.
Each engine directory contains immutable `fixture/`, the exact customer
`customer-01/` downloads, and `materialized/dense-motion/`.

NAMD:

- Trajectory: `namd/materialized/dense-motion/data/alanine/dense.part000001.dcd`.
- Topology: `namd/materialized/dense-motion/data/alanine/system.prmtop`.
- Frozen native validator: `namd/validation-02.json`, SHA256
  `cdb421daee3eeb2122f2c5779f147958f29f74aef6769438bb5d0a63d590a585`.
- Renderer spec: `namd/validation-02-render-spec.json`. Native origin is
  **1210 ps / step 605000**, not the LAMMPS origin. DCD contains only positive
  samples, steps 605010 through 615000; native DCD header time roundoff is
  3.67e-6 ps at this absolute time.

LAMMPS:

- Trajectory: `lammps/materialized/dense-motion/data/dense.1.lammpstrj`.
- Topology: `lammps/materialized/dense-motion/data/system-shake.lmp`.
- Frozen native validator: `lammps/validation-01.json`, SHA256
  `26b12a89a99d267bb8f7e06a9278594f0d658e0b1447030265b5d2ffd80a8ede`.
- Renderer spec: `lammps/validation-01-render-spec.json`. Native origin is
  **1200 ps / step 600000**. The dump retains the native setup frame at step
  600000 plus 1000 positive samples, steps 600010 through 610000.

The renderer receives absolute trajectory paths, native origin time/step,
`canonical_to_native:"identity"`, native format, and the passed receipt path.
All 6,598 atom positions and cells are finite, and all 6,588 constrained
distances were inspected in every frame. Maximum errors are 1.4917e-5 Å for
NAMD and 2.9481e-5 Å for LAMMPS, below the unchanged 1e-4 Å validation threshold.
The last NAMD DCD frame also matches its final binary coordinate checkpoint.
Native thermo samples, loop endpoints, final checkpoint/progress, immutable
inputs, complete output inventories, exact request recipe and typed customer
artifact hashes were checked independently of movie readability.

## Native continuation semantics

NAMD restores coordinate, velocity and XSC cell/piston files together. Native
PME is explicitly 64³, order 4, tolerance 1e-5, 10 Å unswitched cutoff, unchanged
Langevin/piston/rigid-water settings. Seed 20260925 is retained, but its binary
restart files do not serialize the Langevin RNG. The log explicitly records
native COM-velocity removal `(0.0261056, -0.0229095, 0.0551967)`; it is not hidden
or treated as bitwise continuation. No temperature initialization or repeated
equilibration was introduced.

LAMMPS restores its binary restart and explicitly recreates the original
potential, PPPM, thermostat, constraints and integration fix IDs. The native log
confirms restored `nph/kk` state, all restart fix state reassigned, CUDA Kokkos,
64³ PPPM/order 4 and the unchanged neighbor checks. Its Langevin seed is retained
but its RNG resets. The existing GPU KISS FFT warning remains visible; there is
no new optimization/image change.

The **new step-600000 boundary** was checked independently. Its largest periodic
coordinate adjustment is 6.0032e-6 Å. A mass-weighted, fixed-old-direction SHAKE
calculation reconstructed the actual adjustment to 1.3013e-13 Å, with unchanged
velocities/cell/origin and zero displacement of unconstrained atoms. Raw
coordinates include legitimate periodic wrapping; the largest raw difference
is therefore approximately one cell length, not a physical jump. The proof
binds these new trajectory/topology hashes and pinned native source revision
`c7ae612a9497437412cb787b78769570f48653dd`. It does **not** reuse the old
step-396000 exception. Both raw records are retained; no trajectory is repaired.

Twenty correlated picoseconds do not establish equilibrium, convergence, or
cross-engine trajectory equality. Movies use real positive-time samples and
may apply rigid presentation alignment only.

## Image and device provenance

Customer client:
`lc@sha256:81a2f3b54a98299933d487ccca4e9eb3fbea3130257a5a818a83940127429a4d`.

- NAMD worker: `namd-worker@sha256:30df40215f7df047463cf8e37f1fb80457b73fbb7ae8f681b9cb40d980e5f14e`.
- LAMMPS worker: `lammps-worker@sha256:e4e21f952285134be263c9ea3f1f06ce461fdca2409622b568b186b12f7f199c`.

Full registry names, upstream engine identities, native commands, source hashes
and artifact SHA256 values are in `receipts/*-validation.json`, semantically
identical JSON copies of the frozen passed evidence (JSON number formatting
differs, so their file hashes are not the frozen render-input receipt hashes).
Both caller-scoped active model digests matched
before upload/admission.

NAMD had a live Pod/image capture and `nvidia-smi`: H100 80GB HBM3, driver
580.173.02, GPU UUID `GPU-d144da30-c688-aed8-5999-f9d44cee79d0`.
LAMMPS finished before direct Pod observation. Its retained **measured kubelet
and DCGM facts** independently identify the same GPU UUID, actual Pod UID and
node; those additive facts are in `receipts/lammps-identity-supplement.json`.
LAMMPS has no direct per-Pod imageID/driver capture. Do not infer its driver from
NAMD or overwrite the original receipt's capture limitation. The supplement
preserves this distinction.

Native timing is separate from lifecycle accounting: NAMD process wall time
5.106 s / final native estimate 385.366 ns/day; LAMMPS native loop 73.0713 s /
23.6481 ns/day (worker process wall 74.3333 s). Dense dump output adds I/O cost;
these are not controlled throughput benchmarks. Retained scheduler/device
intervals also include preparation and publication and are not native MD time.

## Reproduction and checks

`namd_lammps.py prepare --delivery <frozen-delivery> --output <new-fixture>
--engine namd|lammps` verifies the source delivery inventory, copies only the
needed native restart context, changes output cadence/endpoints, and builds a
deterministic archive. It refuses to write inside the frozen delivery.

`discover` checks the existing ordinary key fingerprint against its previous
customer receipt, typed schema/access and caller-published image digest. `submit`
uses the exact immutable client, stable idempotency identity and durable output
receipt. It neither changes resource policy nor creates credentials. Keep the
key private; do not put it in a command argument or repository. The failed
tentative owner comparison occurred before any API call; the original
four-engine ordinary owner was then used. One active operation at a time leaves
admission room for the other parallel MD worker and uploads.

Materialize using the shared `molecular-dynamics/materialize_customer_results.py`
with the actual submitted request. Then invoke `validate` under the exact
client's `/opt/md-analysis/bin/python`, `--network none`, no GPU/key, mounting:

- This helper directory read-only at `/validation-source`.
- The source `molecular-dynamics` directory read-only at `/md-source`.
- Frozen delivery read-only at `/delivery`.
- Each new engine evidence directory at its identical absolute host path.

Pass `--fixture`, `--workspace`, `--md-source /md-source`, `--master
/delivery/master`, `--receipt`, `--pod-evidence`, and a **new** `--output` path.
Use the actual local UID/GID. The image's isolated analysis environment uses
its already-installed API environment's schema packages; nothing is installed.
The validator binds its own source and the reused strict native readers/SHAKE
verifier in its receipt. Never overwrite a receipt already bound by rendering.

Tests executed:

```sh
python3 -m pytest -q test_namd_lammps.py
# 10 passed: frozen sources, native origins, immutable input, safe output,
# reproducible archives and caller-visible exact digest checks.

# Under the exact client's /opt/md-analysis/bin/python, offline:
python test_dense_semantics.py
# 13 passed: cadence, native origins/timestamps, missing/duplicate/nonfinite
# samples, grid mismatch, recipe identity, rehashed input tampering, hidden
# retry and duplicate inventory rejection.
```

Retained harness-only failures include the initial discovery mount assumption,
matching a repository-qualified image against a digest-only API field, and a
CPU validator package-import/UID error. Each was corrected without changing the
native fixture/result bytes or resubmitting a native simulation. The raw
preflight logs and `namd/validation-harness-error-01.json` remain in the evidence
directory; neither native operation failed.
