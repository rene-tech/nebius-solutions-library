# Canonical LAMMPS performance investigation

Bounded diagnostic lane, not a production protocol change. The exact worker is
`lammps-worker@sha256:e4e21f952285134be263c9ea3f1f06ce461fdca2409622b568b186b12f7f199c`.
Use one explicitly released task-owned GPU; do not overlap the existing full
canonical trajectory or change image, driver, node policy, capacity or quotas.

The matched starting checkpoint is canonical fixture-03 `probe.restart` at
step 2,000, SHA256 `0458091095da737f80e38befb329ddf4a2549933767b359e58b6a00d9842ea5d`.
Original coefficient, nonbonded, thermodynamic and constraint includes are copied
byte-for-byte. All controls retain ff14SB/TIP3P, 10 Å unswitched LJ plus tail,
PPPM tolerance 1e-5, explicit 64³ mesh/order 4, 2 fs, identical hydrogen/water
constraints, Langevin seed/damping and the same isotropic NPH barostat.
The unchanged full canonical baseline continues separately.

Initial evidence from `benchmark-02` is 14.6881 s per 1,000 NPT steps on H100
(11.765 ns/day). CUDA+Serial rejects t4/t8; all-CPU serial took 75.473 s.
Mixed CPU PPPM attempts failed at the first dynamic step and are not candidates.
Native asynchronous section timers cannot locate the large Other bucket alone.
KISS is the GPU FFT implementation, not evidence of CPU fallback.

The first control changes only a **zero-tilt cell's representation** to orthogonal,
before defining Kspace. Nonzero tilt explicitly fails this diagnostic precondition.
Initial all-atom coordinates, physical forces and nine energy components must
match the baseline, and complete NPT outputs must pass finite/constraint checks.
The script emits run0 physical forces in an independent process without
stochastic/constraint fixes. A separate fresh restart preserves the NPH barostat
restart records, performs 200 warmup steps, then measures 1,000 steps. Three
separately restarted measurements are run per case.
The timed continuation uses documented `run ... pre no`, with no intervening
state/configuration change. The earlier default-reinitialization attempt is
retained: both representations completed warmup but acquired pathological
pressure at a second default run setup and failed before its first step.
This does not qualify arbitrary multi-run NPH/SHAKE scripts.
This is a short screen, not an independent ensemble or sustained production proof.

Source-backed hypothesis: in the pinned implementation, NPH recomputes Kspace
setup on every volume change. Triclinic Green-function setup parallelizes only
the z-grid, looping x/y inside each work item; the orthogonal path flattens the
grid. The canonical box has exactly zero tilt. The matched measurements below
and CUDA trace support this explanation on the exact image and input.

## Recorded evidence

All raw evidence is under
`/home/tux/fs2-alanine-lammps-performance-20260923/`. Exact Pod provenance is
`pod.json`; `environment-corrected.json` records H100 80GB, driver 580.173.02,
GPU UUID `GPU-e8da8985-f0cc-43f6-9a6e-78f4ababcd10`, native library versions,
tool availability and read-only profiling policy observations. No other GPU
workload overlapped this bounded Pod.

`screen-03` completed three matched repeats per representation. Single-rank
native Loop medians are 11.7714 ns/day (triclinic, range 11.7622–11.7935) and
28.4171 ns/day (orthogonal, range 28.1145–28.4959), a 2.414× short-screen ratio.
Run0 preserves all 6,598 coordinates exactly, maximum force difference is
1.244e-11 kcal/mol/Å, and nine energy differences are at most 1.022e-14 kcal/mol.
All native frames and 6,588 hydrogen/water distance constraints passed a
1e-4 Å gate. Validation SHA256:
`a95054cba02726690a09b8a42a42264a2f732dda8c199c5edb7af804ae30ef8b`.
These 200-warmup/1,000-measured-step cases are not sustained full production.

`restart-check-01` separately reads an already-orthogonal native restart, with
and without repeated `change_box all ortho`. Both native processes succeed;
coordinates and all nine energies are unchanged, and maximum force difference
is 1.137e-13 kcal/mol/Å. `restart_check.py --validate-only` verifies every native
file hash and all atom identities before emitting a numerical receipt.

The final fixture04 archive SHA256 is
`cb0ab63bd6311acf030288db41544d7ea3988e47b7155da2dd62f18808162b53`.
`fixture04-probe-local` ran the exact worker's original/adapted run0, minimization,
1,000 NVT and 1,000 NPT steps. All four stages and five frames passed Curie's
independent `validate_probe.py`: all nine energy terms equal, maximum force
difference 1.066e-13 kcal/mol/Å and maximum constraint error 4.644e-5 Å.
Result SHA256: `8e1dce07956856a2d2461687d326b49ea47cf44750181e0192bdce387e9d12dd`.
Scientific validation SHA256:
`dd0dfeb63807d51f228841b85408ade8d369bf077db82ae780c7b15c591cd1c0`.
The fixture/full-run owner received this proof; this lane did not change or
interrupt the earlier canonical full trajectory.

### CUDA attribution, not a throughput benchmark

`profile-02` captures CUDA/NVTX with Nsight Systems 2025.6.3 and no CPU sampling.
The existing host tool's target binaries were copied into ephemeral `/work`;
there was no image rebuild, package install or driver-policy change. Raw QDSTRM
was imported with the matching host tool, then exported to SQLite/CSV. The
capture includes setup plus 100 warmup and 200 measured native steps.

The triclinic Green-function kernel has 301 calls, mean 8,407.807 µs, totaling
2.530750 s and 69.3% of summed GPU kernel time. The corresponding orthogonal
kernel has 301 calls, mean 17.625 µs, totaling 0.005305 s. GPU KISS FFT has
3,612 calls in both, mean 159.522 versus 159.396 µs. Thus the trace identifies
a changed Green-function path, not a CPU FFT fallback. Kernel percentages are
summed GPU execution durations, not whole-process wall-time percentages.
`kernel-attribution.json` SHA256:
`f66d32c2b20e4270efee1a258f0bd4ecb340f328925e1155b74b34424739689c`.
Profiled native outputs passed the same numerical/constraint checks; the
validator deliberately emits no comparable unprofiled throughput summary.

### Retained failures and exploratory rank controls

- `screen-01-failed-library`: direct diagnostic command omitted the architecture-specific
  shared-library path and exited 127 before native forces. The benchmark driver
  was corrected to match the released worker's environment; image unchanged.
- `screen-02-failed-reinitialization`: both box representations failed at the
  second default run setup after finite warmup. This is retained separately from
  the documented continuous `pre no` control and the original one-run stages.
- `profile-01-failed-tool-layout`: copied tool directory had the wrong installation basename and
  refused launch; no native profiled dynamics. `profile-02` corrects tool layout.
- `fixture04-probe-missing-companion`: a diagnostic invocation used the default companion mode
  without a transport companion. It was interrupted before native execution;
  the separate `fixture04-probe-local` explicitly selected local checkpoint mode.
- `mpi2-01`: two ranks on the same single GPU without MPS passed native outputs
  and the original single-rank force/energy oracle, but were much slower: 2.731
  and 3.768 ns/day for the triclinic/orthogonal one-repeat screens. MPI reports
  memory-binding and CUDA host-registration warnings, which are retained. This
  does not establish optimized MPI/MPS performance or a new distributed App
  feature; rank-dependent Langevin streams also preclude bitwise trajectories.
- `mpi4-01`: four ranks without MPS also passed the original one-rank numerical
  oracle and finite/constraint checks, but a bounded 100-warmup/200-measured-step
  screen was still slower: 1.280/1.484 ns/day. No further rank tuning, MPS service,
  new image or distributed API was introduced. These rejected one-repeat screens
  are not presented as statistically confirmed performance results.

Performance promotion uses the validated one-rank orthogonal representation.
Do not reduce mesh, tolerance, constraints, precision or force-field fidelity.

After the representation fix, GPU KISS FFT accounts for 52.5% of summed kernel
time in this small-system trace (0.575737 s of this capture). A matched same-source
GPU KISS-versus-cuFFT build control and small-system PPPM overhead analysis are
scoped future investigations, not evidence that cuFFT will improve end-to-end
performance. No alternate image was run or promoted in this lane. The current
validated image already uses GPU KISS FFT; the target mesh/order/accuracy remain
unchanged. The release owner ended further tuning after the bounded controls.

For reproducibility, copied Nsight CLI SHA256 is
`a363929734eda02be778ae03b296e21bf8b236d45b373c4866dcc38751cf60ac`,
matching host QDSTRM importer SHA256 is
`6282d44836e35c23b1548c359bd42f045dd65d5ebd2af9f4902a76d6501aa340`.

The independent worker archive audit reuses the existing shared file inventory,
native request normalization/recipe contract and immutable input-bundle reader.
`verify_worker_archive.py` does not duplicate the scientific adapter validator.
The captured profiles and rejected attempts remain independently hashed; no
earlier failure is relabeled or overwritten.

Primary sources:

- [Pinned NPH implementation](https://github.com/lammps/lammps/blob/c7ae612a9497437412cb787b78769570f48653dd/src/KOKKOS/fix_nh_kokkos.cpp)
- [Pinned PPPM implementation](https://github.com/lammps/lammps/blob/c7ae612a9497437412cb787b78769570f48653dd/src/KOKKOS/pppm_kokkos.cpp)
- [LAMMPS Kokkos guidance](https://docs.lammps.org/Speed_kokkos.html)
- [Native run continuation semantics](https://docs.lammps.org/run.html)

Unprofiled native Loop timing is the performance measure. CUDA launch-blocking
and Nsight runs are separate diagnostics, never reported as comparable throughput.
No precision reduction, smaller mesh or changed force field is authorized.
