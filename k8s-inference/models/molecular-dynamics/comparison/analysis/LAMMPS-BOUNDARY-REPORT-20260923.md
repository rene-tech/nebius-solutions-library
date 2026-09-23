# Native LAMMPS restart boundary: retained failure and resolution

The first complete hosted fixture-04 run (operation
`46ea947b-fecc-4036-9c87-42df6886f1a6`) initially failed the generic 1e-7 Å
duplicate-coordinate check at step 396,000. The original failed receipt
`hosted-primary-validation-01.json` and both native dumps are preserved.
No simulation input, binary, checkpoint, coordinate or velocity was changed.

The exact build's [SHAKE setup](https://github.com/lammps/lammps/blob/c7ae612a9497437412cb787b78769570f48653dd/src/RIGID/fix_shake.cpp#L454)
calls coordinate correction. Its [KOKKOS implementation](https://github.com/lammps/lammps/blob/c7ae612a9497437412cb787b78769570f48653dd/src/KOKKOS/fix_shake_kokkos.cpp#L1768)
temporarily zeroes forces and velocities, calculates a position-only constraint
correction, then restores the original velocities. This occurs at run setup,
including native restart. Source files and the exact worker image are hash-bound
in `hosted-primary-boundary-diagnostic-02.json`.

An independent mass-weighted constraint solve on all 2,198 SHAKE clusters,
including all 6,588 constrained distances, explains the measured change:

| Boundary measurement | Value |
|---|---:|
| Largest atom displacement | 8.943697e-6 Å |
| Largest coordinate-component displacement | 8.503893e-6 Å |
| Largest difference from independent SHAKE solution | 1.383360e-13 Å |
| Largest cluster center-of-mass displacement component | 9.603686e-14 Å |
| Cell, velocities, and unconstrained atoms | Exactly unchanged in native output |
| Largest constraint-distance residual before setup | 1.267989e-5 Å |
| Largest constraint-distance residual after setup | 1.257883e-13 Å |

The evidence supports native constraint reprojection, not missing trajectory
time or loss of the recorded coordinates/velocities. It does **not** establish
bitwise continuation: native Langevin RNG state is not serialized by this recipe.
The new process reuses the recorded production seed 20260925. This is a native
file-checkpoint continuation, not a CUDA process snapshot or fresh-Pod test.

Both thermodynamic rows remain visible. Temperature, kinetic energy, volume and
density are unchanged, but native pressure is 54.945582 versus 1.369192 atm and
potential energy differs by -8.12653e-5 kcal/mol. Opposing real/reciprocal
electrostatic component changes accompany native PPPM setup, whose printed
G-vector changes from 0.31804663 to 0.31814598 Å^-1; the requested 64³ mesh,
order 4, 10 Å cutoff and 1e-5 tolerance are unchanged. Initialization virial is
not assumed equal to the preceding closed-step virial.

## Narrow analysis rule

Generic duplicate handling remains strict. This one explicit policy requires
the exact worker image, source revision, both trajectory-file hashes, topology
hash, diagnostic hash and boundary step. At regeneration it repeats the
independent constraint solve and checks unchanged cell/origin and velocities,
unchanged unconstrained atoms, preserved cluster centers, small displacement,
and agreement with the position-only solution within 1e-9 Å. Before/after
constraint residual limits are 1e-4/1e-8 Å. It cannot silently accept another
image, another boundary or another trajectory.

The preceding closed-step observation supplies the single sample at 592 ps
production-relative time. The following initialization frame and **every** native
thermodynamic field are retained in boundary receipts; no raw frame is repaired,
interpolated, fabricated or overwritten. Full validation independently checks
all stage durations, complete 1 ps schedules, finite atoms/cells, and all
constrained distances through the final production step 600,000.

Eight explicitly synthetic regression tests cover the projection, nonmutation,
small rigid translation, constraint-preserving rotation, unconstrained atom
motion, velocity/cell/origin changes, wrong step and changed image/evidence.
They are unit checks, not native scientific evidence. The original strict failure
is not deleted or retrospectively relabeled as a pass.
