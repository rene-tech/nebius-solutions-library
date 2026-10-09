# Acceptance against the requested four-engine query

This is the tracked acceptance matrix for the full server bundle
`/home/tux/fs2-alanine-comparison-20260923/delivery-02`. Paths below are relative
to that bundle, not this source directory. Raw trajectories are not stored in Git.

The requested molecular-dynamics experiment and deliverables passed. This is
not a claim that every feature of every engine, every GPU-process snapshot or
an unconstrained future scientific experiment has been qualified.

| Requested outcome | Actual evidence in this bundle |
|---|---|
| Inspect installed programs, converters, force fields and visualization tools | `validation/host-inventory.json`, `validation/amber-inventory.json`, `PREPARATION.md`, `REPRODUCE.md`, and the final-client inventory in `validation/final-client-portable.json`. Native engines run in pinned containers; absent host executables and absent Blender/VMD/PyMOL are disclosed. |
| One ACE–ALA–NME structure, ff14SB, explicit TIP3P, at least 1 nm clearance | `master/master-manifest.json`, canonical `system.prmtop`/`system.rst7`, original LEaP inputs and source-file hashes. All 6,598 atom indices are retained. No engine, force field or solvent was substituted. |
| Validate converted types, charges, bonded/LJ parameters, exclusions and 1–4 factors | `conversion/` and `validation/audit-03-final-source.json`; the audit re-reads the generated native topologies, compares bonded potential curves and has corrupted-parameter negative controls. SCEE=1.2 and SCNB=2.0 are preserved. |
| Minimize; 100 ps NVT; 100 ps NPT; 1 ns production | Complete `runs/<engine>/` outputs and engine-specific validation receipts. Two unchanged-input hosted cohorts per engine passed; the first final-qualified cohort supplies the comparison/visualization. |
| 300 K, 1 bar, 2 fs, hydrogen constraints, matching cutoffs/output | `REPRODUCE.md`, archived native inputs and full logs. Every dynamic stage uses the prescribed duration; production saves coordinates at 1 ps. Native integrator, barostat and pressure/temperature conventions are explicit. |
| PME, or LAMMPS PPPM | Native logs confirm the configured 64³ mesh and order 4. GROMACS PME autotuning is disabled to retain the matched 10 Å cutoffs and grid. |
| Seeds, commands, versions and settings | `case.json`, `inputs/<engine>/request.json`, archived inputs, native command/version metadata and complete logs. Seeds are 20260923/20260924/20260925; equal integers do not imply equal RNG streams. |
| Complete runnable inputs/scripts and raw files | `run_case.py`, `REPRODUCE.md`, `PREPARATION.md`, `inputs/`, `master/`, `conversion/`, and unaltered `runs/<engine>/data/`. Licensed image access and GPU/container requirements are explicit dependencies. |
| Particle count, charge, initial potential energy, temperature, pressure, density, performance | Full table in `RESULTS.md`; machine-readable statistics in `analysis/comparison.csv`, and initial canonical-coordinate energies in `diagnostics/static-analysis-03/energies.csv`. Units and measurement boundaries are stated. |
| Four short clips and synchronized labeled 2×2 video | `videos/{gromacs,namd,amber,lammps,four-engine-2x2}.mp4`; five verified 1,000-frame, 40 fps, 25 s clips. |
| PBC removal, centering, consistent peptide alignment/camera/representation and water | The portable renderer makes the peptide whole, centers and aligns it to one master reference, and uses fixed peptide sticks, transparent water points, camera and playback. Every rendered frame is an actual 1 ps sample. The common 13 Å water viewing radius is disclosed; full water remains in raw data. |
| Backbone φ/ψ over time and ensemble comparison | `analysis/phi-psi-timeseries.png`, `phi-psi-distributions.png`, `ramachandran.png`, per-engine numeric files and `analysis/INTERPRETATION.md`. No frame-by-frame equality or convergence is asserted. |
| Independently reproduce analysis and visualization | `validation/final-client-portable.json`: final client regenerated all five videos and 78,302 checked scientific values from raw files, with networking disabled and zero numeric differences. |
| Actually use the deployed platform and recover complete outputs | Original hosted operation receipts plus `validation/final-eight-operation-recovery-227.json`: 424 artifacts, 2,749,521,423 bytes, eight matching manifests, all freshly verified with the final client. |

## Material qualifications

Successful execution was not used as proof of force-field equivalence. Static
bonded energies agree to about 0.0001 kcal/mol, but native total energies span
1.126109 kcal/mol because of measured electrostatic/numerical/tail conventions.
Full-array force comparison was available only for GROMACS/LAMMPS. The detailed
reports preserve these limits rather than claiming identical Hamiltonian
implementation or four-engine force parity.

The two runs per engine repeat fixed input seeds to test release repeatability;
they are not independent ensemble replicas. One nanosecond is not a convergence
or free-energy result. Native checkpoint continuation is validated separately
from GPU-process snapshots; the latter remain limited as described in
`PLATFORM-DELIVERY.md` and `validation/GPU-SNAPSHOT-20260923.md`.

The LibreChat preview intentionally excludes raw trajectories because Rene's
existing 5 GB workspace was nearly full. This full bundle contains those files;
no existing customer data was deleted and no quota was raised.
