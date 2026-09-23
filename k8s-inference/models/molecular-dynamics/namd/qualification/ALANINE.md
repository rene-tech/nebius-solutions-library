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
  cross-engine comparison. The public
  [native configuration logic](https://www.ks.uiuc.edu/Research/namd/doxygen/SimParameters_8C_source.html)
  selects force tables for unswitched interactions or non-unit 1–4 scaling,
  among other conditions, consistent with both required settings here.
- Native reports 2,192 H–H bonds, matching exactly the canonical AMBER rigid
  TIP3P water representation. This is not an accidental solute bond warning;
  the [native author's explanation](https://www.ks.uiuc.edu/Research/namd/mailing_list/namd-l.2003-2004/0629.html)
  also distinguishes water H–H connectivity from unsupported 10–12 potentials.

The prior fixture typo failed before force evaluation and is retained separately
at `campaign-alanine-singlepoint-r5-failed-01`. No dynamics or cross-engine
equivalence is claimed by this single-point record.

## Complete exact-r5 native dynamics

The unchanged `alanine-canonical-r3` input completed all four stages on the
fresh r5 H100 worker: minimization to step 5,000, 100 ps NVT, 100 ps NPT and
1 ns NPT production. All 76 native files (108,010,096 bytes) and all 12 original
input members passed inventory/hash checks. The three trajectories contain
100/100/1,000 finite frames. Production is exactly step 105,500 through 605,000
at 500-step intervals: absolute 211–1,210 ps, production-relative 1–1,000 ps.

Raw evidence: `campaign-alanine-dynamics-r5-01/rep-1/data/alanine/` under the
NAMD evidence root. Full native ETITLE/ENERGY/TIMING logs are preserved.
`alanine-validation.json` SHA256
`a794c38ac9440f18ad5e4fc210dfb81ef0e1dca20c14633de151585f394b98c5`
and result SHA256
`92772dbb4a21ab3d449653ef75838b701b920a126dd46fc57f1cd6cf28ea278e`
bind these outputs. Exact-image receipt `runtime-receipt-r5-alanine-canonical.json`
SHA256 `92b2d44c68a08b7aff42a2a54825d71a2daa51d1e01c0c458f886f60ef294b98`
is passed with `customer_ready:false`.

Production mean temperature is 300.144 K and mean density 0.98323 g/mL.
The native group-pressure mean is −1.471 bar; atomic pressure is separately
20.606 bar. Group pressure is the actual barostat control variable, not an
interchangeable alias for the atomic virial. These finite-run means do not
establish pressure convergence or cross-engine ensemble equivalence.

Native logs confirm all-atom BBK Langevin (including hydrogens) at 1/ps,
group-based 1 bar piston, all hydrogen rigid bonds, SETTLE water and 6,588
rigid bonds (6,576 water constraints plus 12 solute hydrogen bonds), analytical
LJ corrections to both energy and pressure, and actual production PME grid 44³.
The documented three warning categories are unchanged and retained in every
stage. Native steady-tail production throughput is 416.589 ns/day; whole
production-process throughput is 414.980 ns/day over 208.203 s. Neither includes
cloud queue, image pull or artifact delivery, and neither is MPS aggregate.

For hosted artifact validation, run `validate_alanine.py --fixture ... --campaign
... --request <client-receipt>/request-transport.json --output ...`. The actual
submitted request hash must match the result recipe; only output destination
and prefix may differ from the immutable fixture. Physics, scripts, steps and
seeds remain compared. `alanine_runtime_receipt.py` binds native validation to
the captured Pod/image; it is not a substitute for a hosted-client receipt.
