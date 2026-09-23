# Canonical LAMMPS ff14SB/TIP3P fixture

Status: adapter parameters/geometry, 9 synthetic unit tests and the **real native
probe pass**. Full canonical dynamics is running, not yet accepted. No simulation
is claimed from generated inputs or algebraic tests. Shared platform/release and hosted submissions remain
parent-owned. Exact intended worker:

`cr.eu-north1.nebius.cloud/e00akg9ndpx77eaexh/fs2-platform/lammps-worker@sha256:e4e21f952285134be263c9ea3f1f06ce461fdca2409622b568b186b12f7f199c`

## Explicit native constraint adapter

Canonical Amber topology has 2,192 water triangles: two OH harmonic bonds
(0.9572 Å) and one HH harmonic bond (1.5136 Å), with no water angle term.
InterMol assigns a separate coefficient type to every bonded record. LAMMPS
SHAKE supports small star clusters and constrained angles, not a triangle of
three directly selected bonds. The adapter:

1. Deduplicates numerically identical coefficient types, retaining every original
   interaction record and parameter, including all 2,192 HH harmonic bonds.
2. Adds one exactly zero-K harmonic HOH angle per water. The target is derived
   from the master distances: `2 asin(HH/(2 OH)) = 104.49060265846039°`.
   It is deliberately **not** replaced by a hardcoded nominal TIP3P angle.
3. Selects both OH bonds and that HOH angle for SHAKE, thereby enforcing the
   original HH distance geometrically. Peptide hydrogen bonds are also selected.

The original data are retained separately. Checks compare full interaction
multisets, not dictionaries that hide duplicates; atoms, coordinates, charge and
mass records are unchanged. New angle potential and its gradient are identically
zero for arbitrary geometry. A complete coefficient-ID map and SHAKE atom/type
map are recorded. Missing HH bonds, duplicate bonds, overlapping waters or
non-isosceles water targets fail closed.

## Generate and test

```bash
PY=/home/tux/.venvs/fs2-four-engine-20260923/bin/python
LAMMPS=/absolute/worktree/k8s-inference/models/molecular-dynamics/comparison/lammps
"$PY" -m unittest discover -s "$LAMMPS/tests" -v
"$PY" "$LAMMPS/prepare_fixture.py" \
  --master /home/tux/fs2-alanine-comparison-20260923/master-01 \
  --converted /home/tux/fs2-alanine-comparison-20260923/conversion-02 \
  --output /new/fixture-directory
"$PY" "$LAMMPS/validate_probe.py" /downloaded/probe/data \
  --output /new/native-probe-receipt.json
"$PY" "$LAMMPS/validate_production.py" /downloaded/full-workspace \
  --output /new/native-production-receipt.json
```

The generator verifies every source manifest hash before adapting; output must
be new. Bundle tar/gzip metadata is deterministic. `probe-request.json` and
`production-request.json` are native typed LAMMPS request parameters, ready for
the parent to submit with `input.tar.gz` through the ordinary hosted path.

## Gates before production

The bounded probe executes original and adapted native `run 0` at identical
canonical coordinates (PPPM 1e-6, mesh64³/order4), writing decomposed energies
and all native atom forces. It then minimizes, runs 1,000 NVT steps and 1,000
NPT steps using the adapter, and writes coordinates, velocities and restart.
Validator checks matching inputs; ≤1e-5 kcal/mol energy component change and
≤1e-5 kcal/mol/Å maximum force change; all water OH/HH and peptide H-bond
distances within1e-4 Å after dynamics; five expected frames and final step2,000.
These are probe tolerances, not equilibrium/convergence validation.

Parent's separate force-field conversion and four-engine decomposed single-point
gate must pass too. The parent topology audit had demonstrated negative-control
gaps; those findings are retained in `../analysis/TOPOLOGY-REVIEW-20260923.md`.
A passed algebra test or one native probe cannot replace those gates.

## Full requested stages and recovery

- Native CG minimization: at most5,000iterations/50,000forceevaluations, full
  original flexible bonded potential. SHAKE is not approximated by an added
  minimization restraint. Explicitly reset native step0 afterward.
- NVT50,000steps (100ps), then NPT50,000steps (100ps), then NPT500,000steps (1ns).
- All dynamics:2fs, Langevin300K and damp1,000fs (friction1/ps), hydrogen-bond
  SHAKE tolerance1e-6, 10Å unswitched/unshifted LJ cutoff with tail correction,
  PPPM1e-5 fixed64³/order4; neighbor every1/checkyes with2Å skin.
- Isotropic NPH MTK integration plus separate Langevin thermostat, Pdamp2ps,
  barostat chain3 and ptemp300K. Pressure1bar=0.9869232667160128atm. The native
  Langevin uniform noise and zero-total-random-force setting are explicit
  algorithm differences, not a claim of identical stochastic trajectories.
- Seeds NVT20260923/NPT20260924/production20260925. Same numeric seeds do not
  imply the same random streams across engines.
- ID-sorted coordinates and velocities every500steps (1ps). Production origin
  step100,000/time200ps, final600,000/time1200ps; common comparison1..1000ps.
- Native timer at500-step boundaries, segment-specific dump names, closed
  restart/progress after each segment. Worker independently reads restart step.
  Resume scripts recreate pair, PPPM, integrator, thermostat, constraints and
  output context. They replay every exact bond/angle/dihedral coefficient from
  the adapted data, because hybrid styles do not store substyle coefficients
  in binary restart files. SHAKE and Langevin fix state are not saved by native restart;
  Langevin RNG restarts, so this is **statistical, not bitwise continuation**.
  The initial1800s segment target favors one uninterrupted production segment;
  preserve any actual segment boundaries for honest common analysis.

Native throughput is separate from hosted queue, preparation, upload/download
and checkpoint I/O. Raw output/restart/force/log files must be retained and
hash-bound to exact operations before calling this fixture qualified.

The production validator checks actual worker completion and native restart
steps, native loop step counts, all stage frame schedules and finite coordinates,
every constrained distance at every saved post-initial frame, and final closed
progress. Multiple closed native segments are counted only after matching their
duplicated boundary coordinates/cell; those duplicates are recorded, never
silently used as extra physical samples. Common-render segment assembly remains
an additional explicitly provenance-bound step if a production run is segmented.

Primary references: [LAMMPS SHAKE](https://docs.lammps.org/fix_shake.html),
[native NPH/MTK](https://docs.lammps.org/fix_nh.html),
[Langevin and restart semantics](https://docs.lammps.org/fix_langevin.html),
[hybrid bond restart](https://docs.lammps.org/bond_hybrid.html),
[hybrid angle restart](https://docs.lammps.org/angle_hybrid.html), and
[hybrid dihedral restart](https://docs.lammps.org/dihedral_hybrid.html).

## Retained native failure and narrow correction

The first hosted probe (`cec80ced-079a-4577-96ef-a26189492302`) failed at
`shake-probe`. Its inner native log did not survive hosted cleanup. Exact-image
reproduction in a retained one-H100 Pod completed both static stages and native
minimization, then showed `All bond coeffs are not set` after `read_restart`.
Native SHAKE had already found all 2,192 frozen water-angle clusters. The failed
workspace is retained at `/home/tux/fs2-alanine-lammps-20260923/native-probe-01`.
The correction adds exact coefficient replay after each restart; no coefficients,
force-field terms, targets, seeds, step counts or constraint selections change.
Fixture-01 remains immutable. Fixture-02 is a separate evidence generation.

Fixture-02 then completed 1,000 NVT steps but native NPH initialization required
SHAKE before the box-changing fix. Fixture-03 declares Langevin, SHAKE, then
NVE/NPH, and creates initial velocities after constraint DOF registration.
The complete real probe passed on the exact image/H100: all nine decomposed
energy changes were zero, maximum 6,598-atom force change was
1.99e-13 kcal/mol/Å, final step2,000 and five frames were present, and maximum
post-initial constrained distance error was4.76e-5 Å (NPT). Receipt and all raw
files: `/home/tux/fs2-alanine-lammps-20260923/native-probe-03`.

The same-state,1,000-step NPT screen retained GPU t1 (14.6881s native loop).
CUDA+Serial rejects Kokkos t4/t8; full CPU serial was75.473s. CPU PPPM and
Kokkos-host PPPM attempts failed at the first dynamics step and are not selected.
The zero-tilt orthogonal representation attempt stopped at input setup, before
dynamics. Failed attempts and commands are preserved in benchmark-01/02/03;
benchmark-01 was a launcher-library-path error, not an engine measurement.
Asynchronous GPU timing categories do not identify a bottleneck; total loop
times remain the bounded performance measurement. No physics was tuned.

Full fixture-03 bundle SHA256:
`e04c642ea12237ea2840732dba86019536b61457c6e15eadf769b308559626c8`.
One task H100 Pod `fs2-lammps-r20260923-alanine-probe` on
`computeinstance-e00bwrmx5x05qn4bc8` executes the full protocol into
`/home/tux/fs2-alanine-lammps-20260923/native-production-03`; this is native
qualification, not yet a completed hosted/customer result.
# Optional zero-tilt orthogonal representation

`prepare_fixture.py --orthogonal-proof PASSED_SCREEN.json` opts into the exact
geometry-preserving orthogonal-box recipe. It requires three passed, unprofiled
native repetitions for both representations and zero xy/xz/yz source tilts.
Every initial/restart script places `change_box all ortho` immediately after the
read, before PPPM, fixes or dumps. The native command also rejects nonzero tilt.
No box length, coordinate, interaction, cutoff, mesh, timestep or stage changes.
The original data and native performance proof are retained in the bundle.

The 2026-09-23 exact-image short screen reported medians 11.7714 versus28.4171
ns/day (three1000-step measured NPT windows after200 continuous warmup steps),
with identical coordinates, maximum force difference1.244e-11 kcal/mol/Å and
energy-term difference1.03e-14 kcal/mol. These are **short-screen results, not
full-production performance**. Raw results are under
`/home/tux/fs2-alanine-lammps-performance-20260923/screen-03`; the passed receipt
SHA256 is `a95054cba02726690a09b8a42a42264a2f732dda8c199c5edb7af804ae30ef8b`.
The already-orthogonal restart run0 check also passed. Fixture04's complete
adapter/minimize/SHAKE probe and long public run remain separate required gates;
the original fixture03 full baseline is retained and continues unchanged.
