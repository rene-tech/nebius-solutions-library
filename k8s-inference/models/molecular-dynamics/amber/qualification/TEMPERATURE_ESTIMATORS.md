# Exact PMEMD26 LFMiddle temperature provenance

This is a source inspection of the already-qualified private worker
`amber-worker@sha256:1ca8115d4b4f899a282e449e05c59c1e6a6897fbb3ab8f889965ec73677d11fc`,
not an engine change, numerical correction, or replacement of native output.
The source archive SHA256 is
`0478ccce892f3525e995e9c85458d552c6060b73dd28acd03c366e61ecf23a14`.
Licensed source remains private; the descriptions below are independent summaries.

The canonical H100 and L40S production logs report mean temperatures of
298.32824 K and 297.62712 K, respectively. These native values remain unchanged.
They are **not** computed from the same velocity estimator as the archived
`production-001.mdvel` frames.

## Exact implementation path

All source paths below are relative to `pmemd26_src/src/pmemd/src`.

1. `cuda/kMiddle.h:89` copies the current velocity into the previous-velocity
   array before the full force kick. `cuda/kForcesUpdate.cu:1220` then dispatches
   the constrained middle-scheme sequence: kick, velocity constraints,
   half coordinate update, Langevin thermostat, half coordinate update.
   `runmd.F90` subsequently applies coordinate and velocity constraints.
2. `runmd.F90:2977` calls the ordinary CUDA kinetic-energy kernel without a
   middle-scheme-specific estimator override. Its PME implementation at
   `cuda/kForcesUpdate.cu:1763` computes
   `EKE = c_ave / 8 * sum(mass * |v_current + v_previous|^2)`.
   It separately computes `EKPH = 1/2 * sum(mass * |v_current|^2)`.
   `cuda/gpu.cpp:12122` returns these distinct quantities to Fortran.
3. `runmd.F90:514` defines `c_ave = 1 + gamma_ln * dt / 2`, which is 1.001 for
   the frozen 1/ps friction and 0.002 ps step. Lines3852–3853 assign EKE to the
   reported kinetic energy; `runfiles.F90:750` computes the printed temperature
   as `EKE / (KB * DOF / 2)`.
4. For the fixture's positive `ntwv` and default `ionstepvelocities=0`,
   `runmd.F90:4684` downloads and writes the **current** velocity, not the average
   of current and previous velocities. NetCDF declares Angstrom/ps with the
   scale factor20.455; readers must honor it exactly once. To compare with
   internal native kinetic-energy units, use physical Angstrom/ps divided by
   20.455 before the mass-weighted square.
5. `prmtop_dat.F90:498` and502 set the removed COM degrees of freedom to zero
   for this middle/Langevin setup. `degcnt.F90:64` counts constrained degrees of
   freedom. The canonical topology has6598 atoms and6588 constrained bonds,
   giving `3*6598 - 6588 = 13206` DOF, not13203. It has no extra-point correction.

The Fortran printed-temperature path explicitly imports `gbl_constants_mod`
at `runmd.F90:57`. Its constant is `KB = 8.31441 / 4184`
(`gbl_constants.F90:26`), numerically matching the CUDA thermostat's
`KB_cuda = 0.00831441 / 4.184` (`cuda/kForcesUpdate.cu:486`). An initial source
note incorrectly followed the similarly named constant in the unrelated
`constants.F90` module. That alternative differs by about12.17 ppm and would
shift a300 K calculation by about0.00365 K; it does not explain the roughly1.8 K
estimator offset. The explicit Fortran module binding resolves the correction;
none of the native outputs or the velocity-averaging conclusion changed.

The [AMBER26 manual](https://ambermd.org/doc12/Amber26.pdf), section23.6,
printed pages443–444, describes the same leapfrog-middle propagation and
SHAKE/RATTLE constraint ordering, and the harmonic-limit momentum-distribution
property. That theoretical property alone is not an empirical acceptance test
of an anharmonic finite-step molecular trajectory.

## Interpretation and limits

Report both native printed temperature and independently mass-weighted saved-
velocity temperature with explicit estimator labels and uncertainty. Do not
silently replace the native298.32824 K measurement with a near-target number.
This source trace establishes why the observables differ; it does not prove
convergence or quantify the complete finite-step bias of either estimator.
Saved1 ps frames lack the adjacent2 fs previous-velocity array, so they cannot
exactly reconstruct each printed EKE using successive saved frames. A future
every-step paired-velocity diagnostic would be a separate bounded control.

## Private-source identity

| Inspected file | SHA256 |
| --- | --- |
| `runmd.F90` | `d3f7c49398aa14f580c34b55197f2078e2513a7ddf68f2ebd9133947f8b80803` |
| `runfiles.F90` | `276c2411b93ed630ddd60eea112df1bcca53025314ac6906b2a8bba2402c2f0a` |
| `prmtop_dat.F90` | `d3c6a4ce3716c471ab757b0d0777db3fb84cd1a196134911955b0bdfc652f353` |
| `degcnt.F90` | `6a230178df71f34250b96a90b06e91ddda00b5128605734065406a3bf7698615` |
| `constants.F90` | `e5fd9aa3ee4d7d2adf399ebf0feaa26a43686881887612fa36d448e351df1246` |
| `gbl_constants.F90` (actual imported KB) | `a8073e9f1951ebfb141809dbd85e92544ef063bb0cf8781ad2940506dc183745` |
| `cuda/kForcesUpdate.cu` | `4848bbf63f8ad64388e85bad1ab17d718120f43859a81101dab236b24d851707` |
| `cuda/kMiddle.h` | `ed5e74868a40d1bcceb0a4e5cfc67f2f1f2a0aa8653179e7943c655b360b0f09` |
| `cuda/gpu.cpp` | `2d2deb107ed8cf2e0afbb79d795b7cbec07e858d093ef6337de29af9ff05aed0` |
| Acquired `Amber26.pdf` | `7f12b0c947685899eac077e632b1c8b234238047ea7ceb9b9c6fd8452ec66778` |
