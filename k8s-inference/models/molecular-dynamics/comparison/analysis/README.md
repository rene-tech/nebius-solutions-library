# Canonical four-engine trajectory analysis

Status on 2026-09-23: 49 explicitly synthetic unit tests pass. Actual complete
GROMACS, NAMD and AMBER production files have passed common analysis, and their
three real clips are rendered and encoding/visually checked. LAMMPS production
is still running, so the fourth clip and synchronized 2×2 remain pending.
Generated unit fixtures are **not scientific simulation or acceptance evidence**.

This CPU-only subtree owns analysis and visualization, not force-field
conversion, GPU runs, or the combined customer-shaped release gate. The parent
comparison README and native stage/equivalence receipts remain authoritative.

## Exact installed tools

Inspected on 2026-09-23: Python 3.12.3; MDAnalysis 2.10.0, NumPy 1.26.4,
SciPy 1.16.3, Matplotlib 3.10.7, Pillow 12.3.0; FFmpeg/ffprobe 6.1.1-3ubuntu5
with libx264. Blender, VMD, PyMOL, POV-Ray, PyVista, VTK and pyedr are absent.
No licensed renderer was downloaded. Root owns the isolated environment:

```bash
PY=/home/tux/.venvs/fs2-four-engine-20260923/bin/python
ANALYSIS=/absolute/worktree/k8s-inference/models/molecular-dynamics/comparison/analysis
"$PY" "$ANALYSIS/compare.py" --inventory
"$PY" -m unittest discover -s "$ANALYSIS/tests" -v
"$PY" "$ANALYSIS/compare.py" --spec /evidence/real-runs.json --output /evidence/analysis-01
"$PY" "$ANALYSIS/render.py" /evidence/analysis-01 --output /evidence/videos-01
```

For an explicitly partial result, `render.py --engines gromacs namd amber`
renders only those passed real analyses, labels the receipt incomplete, and
does **not** fabricate a missing-engine panel or four-way comparison.

Current actual evidence is `/home/tux/fs2-alanine-analysis-20260923`:
`real-analysis-02` contains the three-engine validation/statistics/phi–psi plots;
`real-clips-01` contains the actual three MP4s and PNG previews. Each clip has
1,000 real frames, 720×720 H.264/yuv420p, 40 fps and 25 s. All previews were
visually inspected. Raw native trajectories are preserved unchanged.

Read [the static energy/force report](STATIC-REPORT-20260923.md) for actual
four-engine single-point values, Coulomb/mesh diagnostics, C6 versus C6+C12 tail
conventions and unclosed numerical differences. Read
[the temperature report](TEMPERATURE-REPORT-20260923.md) for native reported
versus AMBER saved-current-velocity observables, DOF and correlated block
uncertainty. The native low AMBER reported means are retained, not replaced.

The output directories must be new. Failed analysis/render receipts are retained;
they are not overwritten by later attempts. Raw inputs are read-only and hashed
before and after analysis. GROMACS XDR uses the low-level read-only reader to
avoid creating hidden frame-index files beside raw trajectories.

## Input contract

`example-spec.json` documents the schema but contains no usable trajectory paths.
Use absolute paths. Each run explicitly declares the native production origin in
both steps and ps; derive these from the native input/log, never from the desired
result. AMBER NetCDF stores native time but not necessarily a step: its step is
reported as **derived**, not independently observed. NAMD DCD time/steps use the
actual AKMA `delta`, `istart`, and `nsavc`; no timestep override is allowed.
LAMMPS dump time is explicitly the native integer step times the recorded 2 fs
protocol. Dump positions may be Cartesian, unwrapped or scaled; atom IDs must
be exactly 1..N and are sorted, not trusted in row order.

For closed LAMMPS continuation segments, `trajectory`, `production_log`, and
`thermo.path` may each be an explicitly ordered list of distinct native files.
Only a duplicate first frame at a segment boundary may be omitted, after checking
all periodic atom positions and the cell within 1e-7 Å; legitimate cell-image
rewrapping is recorded. An arbitrary repeated/missing frame still fails. Native
thermo boundary observations retain both the preceding closed-step value and
the next segment's initialization value; only the former contributes to the
summary. Trajectory/thermo boundary steps and the full 1 ps output schedule must
agree. Production loop durations are summed only when their native step counts
sum to 500,000. No raw segment is rewritten or joined into fabricated frames.

Native topology/conversion evidence must establish `canonical_to_native` (either
`"identity"` or a complete zero-based permutation). The mapping alone is not
force-field-equivalence evidence. Include all relevant source/configuration,
conversion, single-point, stage-validation and operation receipts under
`provenance_files` for hashes; keep parent gate verdicts separate from this tool.
The tool rechecks all hashed canonical master files and expected composition.

Supported production files:

| Engine | Native coordinates | Thermodynamics |
|---|---|---|
| GROMACS | XTC or TRR | `gmx energy` XVG with Temperature, Pressure, Density legends; optional Potential |
| NAMD | DCD | native `ETITLE:` / `ENERGY:` production log |
| AMBER | periodic NetCDF with stored time | native mdout, excluding repeated averages footer |
| LAMMPS | periodic orthogonal/restricted-triclinic custom dump | native thermo table, `units real` |

For GROMACS, export the native EDR using the exact engine and preserve both EDR
and labeled XVG plus the export command in provenance. No unlabeled columns are
guessed. GROMACS density kg/m3 becomes g/cm3; NAMD/AMBER energies kcal/mol become
kJ/mol; LAMMPS pressure atm becomes bar and thermo `Time` fs becomes ps. Density
is also calculated independently from total topology mass and each native cell.
Missing native performance remains null. Native loop/footer performance is never
replaced by queue, image pull, preparation, artifact transfer or end-to-end time.

If AMBER explicitly warns that its MC-barostat pressure was not calculated,
`PRESS=0` is retained as `uncomputed_pressure_placeholder_bar`, **not** a physical
pressure measurement. Mean pressure is unavailable. A later actual native virial
or replay artifact may be joined with `pressure_observations`: CSV columns must
be exactly `production_time_ps,pressure_bar` at every production frame time;
the object also requires `path`, the original `trajectory_sha256`, immutable
`engine_image`, a real `method` description, and nonempty `provenance_files`.
The trajectory hash, complete time coverage, finite observations and all source
hashes are checked. An available native pressure series cannot be overwritten.
The parent must separately validate the virial/kinetic-pressure method; this
join does not manufacture missing observations or substitute target pressure.

## Scientific and geometric checks

The protocol must be the canonical ff14SB/TIP3P, 500,000 steps at 2 fs, NPT,
frames every 500 steps. Accept exactly 1,000 frames at 1..1000 ps or 1,001 with
the native zero frame. Check every frame's atom count, all coordinates, periodic
cell, time and available stored step; reject missing/duplicate/extra frames.
The common analysis/render samples are exactly 1..1000 ps. No resampling,
interpolation, smoothing, duplicate-frame filling or synthetic replacement.

The canonical peptide's bond graph reconstructs a whole molecule by periodic
minimum images, then its heavy-atom centroid is removed. A proper (det=+1)
Kabsch rotation fits the same ten heavy atoms to one master reference for all
engines. Water oxygens use their nearest periodic images about the same centroid
before that rotation. Reduced triclinic cells are supported; non-reduced cells
are rejected rather than silently miswrapped. Raw trajectories are not rewritten.

ALA phi = ACE:C–ALA:N–ALA:CA–ALA:C; psi = ALA:N–ALA:CA–ALA:C–NME:N.
Dihedrals are evaluated on the whole peptide, independent of rigid alignment.
Plots use common angular bins/limits. Scatter time series avoid false straight
lines across the -180/180-degree branch cut. Distribution counts are descriptive;
**1 ns does not establish equilibrium or convergence**, and ordinary SD is not
an independent-sample uncertainty estimate. Initial canonical-coordinate energy
must come from the parent's single-point gate, never the first production frame.
Native stage durations and force-field equivalence remain independent required
parent gates; a readable trajectory does not establish either.

## Rendering contract

Four 720×720 labeled H.264/yuv420p MP4s and a synchronized 1440×1440 2×2 video.
Every actual 1 ps sample is shown once at 40 fps: 25 s for 1 ns. Identical fixed
orthographic camera (elevation 18°, azimuth -55°), 28 Å view width, master
alignment, element-colored peptide sticks and translucent water oxygen points.
Water has an explicitly labeled, identical 13 Å display radius to keep the
peptide legible. This is a **display crop only**, not solvent deletion or a
change to the trajectory/statistics. No camera auto-fitting per engine or frame.
FFprobe checks encoded counts, dimensions, framerate and duration. PNG midpoint
previews enable actual visual inspection. Every output is hashed in a receipt.

## Primary reader references

- [MDAnalysis 2.10 DCD and its native time/cell conventions](https://docs.mdanalysis.org/2.10.0/documentation_pages/coordinates/DCD.html)
- [AMBER NetCDF reader](https://docs.mdanalysis.org/2.10.0/documentation_pages/coordinates/TRJ.html)
- [GROMACS XDR formats](https://docs.mdanalysis.org/2.10.0/documentation_pages/coordinates/XTC.html)
- [LAMMPS dump cell and coordinate conventions](https://docs.lammps.org/dump.html)

No scientific publication/convergence/customer-ready claim is made by this tool.
