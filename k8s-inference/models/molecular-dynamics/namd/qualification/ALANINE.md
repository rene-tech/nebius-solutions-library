# Canonical alanine comparison

`make_alanine_fixture.py` consumes the shared, immutable ACE–ALA–NME
ff14SB/TIP3P master. It copies the AMBER `prmtop` and `rst7` bytes directly;
there is no topology or coordinate conversion. Its input audit is not, by itself,
proof that the native parser or force implementation is equivalent.

The audit checks master manifest hashes, atom counts, finite mass/charge/LJ
data, explicit exclusions and active proper-dihedral 1–4 pairs. Suppressed and
improper terms may intentionally have zero SCEE/SCNB entries. Only active 1–4
terms establish the uniform mapping: SCEE 1.2 becomes `oneFourScaling 1/1.2`,
SCNB 2 becomes `scnb 2`, with `readexclusions on` and `exclude scaled1-4`.
This does not qualify arbitrary AMBER force fields or nonuniform scaling.
AMBER rigid-water connectivity can differ from another engine's angle count;
compare actual constraints and exclusions, not counts alone.

The separate `singlepoint/` request runs both tail-on and a clearly labelled
tail-off diagnostic at the original coordinates, with zero velocities,
constraints disabled (no geometry projection), explicit 64³ order-4 PME grid
and tolerance 1e-6. Inspect
decomposed native energies, grid, exclusions and warnings before dynamics.
Tail-off is never substituted for the requested production protocol.

Dynamics use unswitched/unshifted 10 Å LJ with `LJcorrection on`, PME tolerance
1e-5, all forces every 2 fs, hydrogen-bond constraints and rigid TIP3P water
at tolerance 1e-6, and 300 K all-atom Langevin damping 1/ps. NPT uses the native
isotropic Langevin piston at **1.0 bar**, period 100 fs and decay 50 fs;
`useGroupPressure yes` and `COMmotion no` are recorded choices, not claims of
cross-engine thermostat/barostat identity. Native PME uses order 4 and 1 Å
maximum requested grid spacing; actual grid dimensions come from the log.

Stages are 5,000 minimization iterations, 50,000 NVT steps (100 ps), 50,000 NPT
steps (100 ps), then 500,000 NPT production steps (1 ns). Native Maxwell
velocities are initialized at 300 K after minimization; seeds are respectively
20260923/20260924/20260925 for the three dynamic stages. Independent native RNG
algorithms need not produce identical velocities across engines.

Each dynamic stage has one native process, without internal wrapper-induced
RNG restarts. NAMD writes native periodic restart files every 5,000 steps; the
wrapper's acknowledged recovery boundaries for this fixture are complete
stages. Native restart files do not serialize the stochastic RNG stream.
Production begins at absolute step 105,000 and ends at 605,000; analysis uses
production-relative time. Coordinate output every 500 steps gives 1 ps spacing
and 1,000 production frames. Scientific convergence is not asserted.

The generated provenance records the input archive and master parameter hashes.
Retain the actual native log, all result inventory hashes, input-parity audit,
complete finite trajectory/cell checks, stage timing, temperature/pressure and
density analysis. Native qualification is distinct from ordinary hosted-client
qualification and from GPU process snapshots.

Primary native references:

- [AMBER inputs and scaling](https://www.ks.uiuc.edu/Research/namd/3.0/ug/node13.html)
- [Nonbonded and LJ correction](https://www.ks.uiuc.edu/Research/namd/3.0.2/ug/node25.html)
- [Pressure control and units](https://www-s.ks.uiuc.edu/Research/namd/3.0/ug/node39.html)
- [Langevin hydrogen coupling](https://www-s.ks.uiuc.edu/Research/namd/3.0/ug/node38.html)

## First exact-r5 native decomposition

Corrected input `alanine-canonical-r3` SHA256
`1411c421eb29ff0d7ce79983f0051c16bd1390d5bf0a3a92f0677d31e647fbe3`
ran both diagnostics successfully on worker `30df4021…5f14e`, H100 and driver
580.173.02. `campaign-alanine-singlepoint-r5-02/alanine-validation.json`
SHA256 `fbc90eb5256f47c19dd5109266204cbcd71dd0968dc135a3d1a62c6594b82697`
binds the complete inventories, immutable input members and native logs.
All coordinates remained within 7.11e-15 Å of the original master. The native
run0 kinetic energy is zero; saved velocities have at most 8.7e-14 native-unit
round-off, explicitly measured against a 1e-10 diagnostic tolerance, not claimed
bitwise zero. Both checkpoints remain at timestep zero.

Tail-on energies (kcal/mol): bond 0.12634754, angle 0.36199813, dihedral
9.64400692, improper 0, electrostatic -20652.46094296, VDW 2214.80743341,
potential -18427.52115695. Native tail-on minus tail-off VDW is -68.63371423
kcal/mol and atomic pressure is -108.16508628 bar. A 6.33e-6 kcal/mol
electrostatic difference between the two separate GPU processes is retained;
the diagnostic does not assert bitwise reduction reproducibility.

Native logs independently report 6,598 atoms, 6,597 bonds, 36 angles, 67
dihedrals and 6,674 exclusions, all 45 LJ pair entries, SCNB 2, scaled 1–4
electrostatics, the actual 64³ order-4 PME grid, and tail energy/pressure on.
The log represents AMBER improper terms in the dihedral total; a zero separate
IMPRP column is not evidence that those terms disappeared.

All native warnings remain retained and require fixture-specific interpretation:

- GPU-resident disables lone-pair support. This audited master has no massless
  extra sites; this is not qualification of lone-pair workflows.
- Native GPU force tables are selected for this configuration. `GPUForceTable
  on` was already explicit; no different cutoff, switching or topology was
  substituted. Numerical force/energy equivalence still depends on the
  cross-engine comparison.
- Native reports 2,192 H–H bonds, matching exactly the canonical AMBER rigid
  TIP3P water representation. This is not an accidental solute bond warning.

The prior fixture typo failed before force evaluation and is retained separately
at `campaign-alanine-singlepoint-r5-failed-01`. No dynamics or cross-engine
equivalence is claimed by this single-point record.
