# Canonical-coordinate native energy and force diagnostic

This report concerns the actual unchanged 6,598-atom ff14SB/TIP3P
ACE–ALA–NME master, not minimized or production coordinates. Native single-point
calculations disabled constraints, optimization and integration. Bonded terms
agree closely; nonbonded numerical conventions produce small but measurable
differences. **This is not a claim of identical four-engine energies, complete
four-engine force parity, trajectory identity or thermodynamic convergence.**

The parent topology audit, native run receipts and exact runtime identities remain
separate requirements. This parser does not replace the force-field audit.

## Actual native energies

All entries below are kcal/mol. Torsion includes proper and improper terms;
VDW and electrostatic columns include their corresponding 1–4 terms.

| Engine | Tail | Bond | Angle | Torsion | VDW including tail | Electrostatic | Potential |
|---|---|---:|---:|---:|---:|---:|---:|
| GROMACS | on | 0.126310 | 0.362002 | 9.644002 | 2214.548495 | -20652.750876 | -18428.068609 |
| LAMMPS | on | 0.126356 | 0.361994 | 9.643999 | 2214.566491 | -20652.365410 | -18427.666570 |
| NAMD | on | 0.126348 | 0.361998 | 9.644007 | 2214.807433 | -20652.460943 | -18427.521157 |
| AMBER | on | 0.1264 | 0.3620 | 9.6440 | 2214.5442 | -20651.6191 | -18426.9425 |
| GROMACS | off | 0.126310 | 0.362002 | 9.644002 | 2283.203914 | -20652.750876 | -18359.414212 |
| LAMMPS | off | 0.126356 | 0.361994 | 9.643999 | 2283.200328 | -20652.365410 | -18359.032733 |
| NAMD | off | 0.126348 | 0.361998 | 9.644007 | 2283.441148 | -20652.460949 | -18358.887449 |
| AMBER | off | 0.1264 | 0.3620 | 9.6440 | 2283.2005 | -20651.6191 | -18358.2862 |

Each bonded column spans less than 0.0001 kcal/mol. The tail-on total-potential
range is 1.126109 kcal/mol. That remaining difference must not be hidden by
rounding the total, tuning charges or changing force-field parameters.

AMBER's total here is explicitly the sum of its four-decimal native component
fields; its separately printed minimization total has lower precision. GROMACS
mixed-precision total accumulation does not reproduce the sum of printed terms
exactly; the approximately 0.001 kcal/mol closure differences are retained.
LAMMPS `E_vdwl` already includes `E_tail`, so the tail is not added twice.
Native GROMACS kJ/mol values are divided by 4.184. Raw decompositions and all
conversion/closure details are in the machine-readable receipt.

## Coulomb convention and mesh diagnostic

The inspected source-level Coulomb coefficients in kcal·Å/(mol·e²) are:

| Engine | Coefficient | Primary evidence and limit |
|---|---:|---|
| AMBER | 332.05221729 | Exact private PMEMD26 `constants.F90`, standard charge scale 18.2223 squared; not the separate pGM constant |
| LAMMPS | 332.06371 | [Pinned 22 Jul 2025 implementation, real units](https://raw.githubusercontent.com/lammps/lammps/c7ae612a9497437412cb787b78769570f48653dd/src/update.cpp) |
| GROMACS | 332.063713299 | [Pinned upstream units implementation](https://raw.githubusercontent.com/gromacs/gromacs/da9e013175bae98b31b34384f6b4864ff29f65a5/api/legacy/include/gromacs/math/units.h), derived from its SI constants; exact NGC 2026.2-dev binary-to-revision binding not proven |
| NAMD | 332.0636 | [Official implementation documentation](https://www.ks.uiuc.edu/Research/namd/doxygen/common_8h_source.html); vendor 3.0.2 binary identity is known, but exact source-to-binary binding is not proven |

The diagnostic predicts a constant-only shift as
`AMBER_EEL * (C_engine/C_AMBER - 1)`. This is an explanatory arithmetic
normalization, **not** an engine rerun with modified charges/constants.

| Engine minus AMBER, tail off | Observed EEL difference | Constant-only predicted shift | Remaining difference |
|---|---:|---:|---:|
| GROMACS 64³/order 4 | -1.131776 | -0.714982 | -0.416794 |
| LAMMPS 64³/order 4 | -0.746310 | -0.714776 | -0.031533 |
| NAMD 64³/order 4 | -0.841849 | -0.707935 | -0.133914 |

The authorized CPU-only GROMACS run0 control used the **same immutable 14ff…
baseline image**, four CPU threads and copied inputs. Only the three grid sizes
and PME order changed. Original files were rehashed unchanged; all commands,
native outputs, TRR forces and failures/successes remain archived.

| GROMACS diagnostic | Electrostatic kcal/mol | Constant-normalized residual versus unchanged AMBER 64³ |
|---|---:|---:|
| Original 64³/order 4 | -20652.750876 | -0.416794 |
| 96³/order 6 | -20652.463380 | -0.129299 |
| 128³/order 6 | -20652.462097 | -0.128015 |

The 96³→128³ change is 0.001284 kcal/mol. Increasing this one engine's mesh/order
explains approximately 0.289 kcal/mol of the original residual, but does not
eliminate it. AMBER/NAMD/LAMMPS were not all independently extrapolated to zero
mesh/table error. Equal requested grids/tolerances are not equal error estimates:
the native Ewald coefficients, mixed precision, NAMD GPU tables, LAMMPS Coulomb
tables and AMBER interpolation differ. No universal error bound or final
four-engine numerical-equivalence tolerance is inferred from this one control.

## Dispersion-tail conventions: C6 is not C6+C12

The canonical native VDW on-minus-off differences are:

| Engine | Observed tail Δ, kcal/mol | Independently traced convention | Canonical coefficient formula prediction |
|---|---:|---|---:|
| AMBER | -68.656300 | Bulk type populations, attractive C6 only | -68.656241 |
| GROMACS EnerPres | -68.655419 | Distinct nonexcluded-pair mean, attractive C6 only | -68.655393 |
| LAMMPS | -68.633836 | Bulk type populations, attractive C6 and repulsive C12 | -68.633837 |
| NAMD | -68.633714 | Native on/off observation; exact formula not independently traced here | not asserted |

`dispersion_diagnostic.py` evaluates the unchanged master prmtop coefficient
matrices at the actual 88,170.555590 Å³ cell and 10 Å cutoff. There are 6,674
non-self excluded pairs. The bulk attractive correction is -68.656240637 and
the repulsive contribution is **+0.022403366 kcal/mol**. This accounts for most
of the approximately 0.022 kcal/mol difference between the first and second
convention groups. Small finite-pair averaging and native print/precision
differences remain. These are potential convention differences, not data
conversion errors to be corrected by changing the input force field.

The [GROMACS implementation](https://raw.githubusercontent.com/gromacs/gromacs/da9e013175bae98b31b34384f6b4864ff29f65a5/src/gromacs/mdlib/dispersioncorrection.cpp)
subtracts exclusions before averaging over distinct pairs; EnerPres includes
the attractive term, while the full-interaction variant also includes repulsion.
The [LAMMPS implementation](https://raw.githubusercontent.com/lammps/lammps/c7ae612a9497437412cb787b78769570f48653dd/src/KSPACE/pair_lj_cut_coul_long.cpp)
uses type populations and integrates both inverse powers. Exact private PMEMD26
`pme_force.F90:5869` computes its standard `vdwmeth=1` correction from C6 alone.
Private source was inspected but is not redistributed.

## Available atom-force comparison

GROMACS and LAMMPS supplied all 6,598 force vectors, 19,794 components, at
matching canonical coordinates. Maximum coordinate discrepancy is
7.11e-15 Å after explicit units conversion and ID sorting. GROMACS force-only
TRR has no coordinate record, so the separately hash-bound native input GRO
provides the coordinates; absent coordinates are never filled with zeros.
GROMACS native kJ/(mol·nm) forces are divided by 41.84 to kcal/(mol·Å).

| Metric, GROMACS minus LAMMPS | Value |
|---|---:|
| Component RMS difference | 0.001320881 kcal/(mol·Å) |
| Relative component RMS, LAMMPS reference | 1.082912e-4 |
| Maximum absolute component difference | 0.006447356 kcal/(mol·Å) |
| Maximum atom-vector difference norm | 0.007653937 kcal/(mol·Å) |

Tail-on/off controls give the same force comparison, as expected for these
spatially constant energy corrections at a fixed cell. NAMD and AMBER complete
force vectors were not supplied: **four-engine force parity remains unproven**.

## Reproduction and evidence identity

Evidence root: `/home/tux/fs2-alanine-analysis-20260923`.

| Receipt | SHA256 |
|---|---|
| `static-analysis-03/receipt.json` | `c5bf342afd46a339251dec6f0244a3fec83eb80809946b56f4ec87107c8c2945` |
| `gromacs-convergence-01/receipt.json` | `ff2bebcb1940ad18f75166794d07bce668ec69eebc14e3bfa1a227d3b662e575` |

The first receipt lists every native energy/force input path and hash. The second
binds the exact baseline image, source input hashes, full command arrays and all
generated native outputs. `dispersion-diagnostic-01.json` binds its unchanged
master and static receipt. Failed earlier parser attempts are retained separately.
Source snapshots are in `static-sources-01`:

- GROMACS units header: `19fc3fde45b864b6377f35cf953a71854f35308c5136b7a08b085f2d153ab267`.
- LAMMPS real-units source: `362978558ca39ccb12d78ba6a59b1cbac339af545732c2bfc4af75ba01319f59`.
- NAMD official common-header documentation: `b0e5be8ee02b75b8f1ee0d58128a1a64ca9471abd71c26a06644d0551d092ac4`.
- Private AMBER constants: `e5fd9aa3ee4d7d2adf399ebf0feaa26a43686881887612fa36d448e351df1246`.
- Private AMBER force source: `543c18dd82091ee186a8522e55c48c2d895193cc0d44587e1ea8f013fa0c9054`.

`static_compare.py --help`, `convergence_gromacs.py --help` and
`dispersion_diagnostic.py --help` expose the reproducible, new-output-only entry
points. All measured values derive from retained native output, not synthetic
unit fixtures or a substituted molecular system.
