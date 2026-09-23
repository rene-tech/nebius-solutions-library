# Canonical LAMMPS ff14SB/TIP3P fixture

Status: adapter parameters/geometry and 8 synthetic unit tests pass; **real native
probe and full dynamics are pending**. No simulation is claimed from generated
inputs or algebraic tests. Shared platform/release and hosted submissions remain
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
  output context. SHAKE and Langevin fix state are not saved by native restart;
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
[Langevin and restart semantics](https://docs.lammps.org/fix_langevin.html).
