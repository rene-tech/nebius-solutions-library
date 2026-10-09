# Full-stage alanine diagnostics (2026-09-24)

This additive CPU-only analysis reads the frozen `delivery-02` native NVT, NPT
and production files. It does not rerun simulations, replay another engine's
virial, interpolate missing observations, or modify the original delivery.
Synthetic unit tests are method tests, not scientific evidence.

```sh
/home/tux/.venvs/fs2-four-engine-20260923/bin/python -m unittest discover \
  -s k8s-inference/models/molecular-dynamics/comparison/advanced-analysis/tests \
  -p test_equilibration.py

/home/tux/.venvs/fs2-four-engine-20260923/bin/python \
  k8s-inference/models/molecular-dynamics/comparison/advanced-analysis/equilibration.py \
  --delivery /home/tux/fs2-alanine-comparison-20260923/delivery-02 \
  --output /home/tux/fs2-alanine-analysis-20260924/equilibration/run-01
```

The input directory is relocatable; pass its new location to `--delivery`.
The script uses the sibling qualified `comparison/analysis/` readers, whose
hashes are included in the output receipt. The output must be a new directory
outside the delivery. Python/NumPy/SciPy/MDAnalysis/ParmEd/Matplotlib versions,
arguments, bootstrap seed, all consumed native files, exact worker images and
all output hashes are recorded. Consumed input hashes are checked again at exit.
No GPU, registry credentials, cloud API or internet access is needed to rerun.

## Observations, units and clocks

Native stage clocks differ: GROMACS resets each stage; NAMD includes the
minimizer's 5000-step counter before dynamics; AMBER resets its local step but
retains elapsed coordinate time; LAMMPS uses a continuous dynamics step counter.
Every native schedule must match 100/100/1000 ps at 1 ps output intervals.
AMBER NetCDF has time but no stored step: a derived step is not called native.
The shared plot clock is NVT 0–100, NPT 100–200 and production 200–1200 ps;
minimization is not physical dynamics time. Native initialization rows remain
visible, but each stage's statistics use only its nonzero 1 ps samples.

Potential energy is converted to kJ/mol, pressure to bar, density to g/cm³,
and lengths to Å. Native logged density and canonical-mass/native-cell density
are separate columns. NVT cell density is constant by construction, not proof
of a pressure-equilibrated liquid. A missing GROMACS NVT density column is not
invented. AMBER NVT prints only `PRESS=0` with `ntp=0` and no virial; these
placeholders are retained but excluded from pressure statistics. No proven
NVT replay exists in the frozen bundle. NVT pressure stationarity is therefore
not identifiable for AMBER from these original outputs.

The exact licensed PMEMD26 source was inspected read-only: `runmd.F90:674`
requests virials only for applicable pressure control or surface tension, and
the pressure calculation at lines 919–947 is inside `ntp > 0`. File SHA256 is
`d3f7c49398aa14f580c34b55197f2078e2513a7ddf68f2ebd9133947f8b80803`, matching
the previously delivered private-source provenance. No licensed code is
redistributed here. The AMBER26 manual's PMEMD unsupported-options list also
identifies `imin=5` trajectory analysis as unsupported. Neither the typed
worker's qualified steps nor the existing receipts establish an exact native
NVT virial replay; creating a new estimator is outside this read-only task.

Backbone RMSD uses ACE:C/O, ALA:N/CA/C/O and NME:N, making the complete peptide
whole through bonded periodic minimum images before a proper Kabsch fit of
those same seven atoms to the unchanged canonical master. This is not a
three-atom ALA-only fit that would largely hide dihedral changes. Phi/psi use
the canonical ACE:C–ALA:N–ALA:CA–ALA:C and ALA:N–ALA:CA–ALA:C–NME:N definitions.
No coordinates are averaged or written back. The exact, previously validated
LAMMPS restart policy is reused; both boundary observations remain in receipts.

AMBER native printed temperature and current-saved-velocity temperature are
different observables. The source-traced midpoint estimator and 13,206 DOF are
documented in delivered `diagnostics/TEMPERATURE_ESTIMATORS.md`. The latter
uses the native 20.455 velocity scale and `KB=8.31441/4184`; it never replaces
the former. Saved 1 ps velocities cannot reconstruct adjacent 2 fs midpoint
velocities. Native pressure conventions also differ; no atomic/group/molecular
virial identity is implied. Static Coulomb/mesh/dispersion energy offsets remain
in place and limit literal interpretation of cross-engine potential means.

## Correlation and conditional uncertainty

For an angle use `z=(cos(theta), sin(theta))`, subtract its vector mean, and
normalize the trace lag covariance by the trace variance. The lag covariance
uses denominator N. This is invariant to choosing a different angular origin
and avoids treating +179° and −179° as distant values. Report sine and cosine
components as well. Constant or unvisited observables have undefined mixing
time, not N independent observations.

Use Geyer's paired sequence `(rho0+rho1), (rho2+rho3), ...`, stop at the first
nonpositive pair or N/2 lag, and monotonize positive pairs. Define
`g=max(1,-1+2*sum(pairs))`, `tau_int=g*dt/2`, `Neff=N/g`. The explicit factor of
two and cap are shared with the separate Ramachandran worker. Geyer's theorem
assumes a stationary reversible chain; here the estimator is a diagnostic,
not a proof that finite-step MD meets those assumptions. See the author's
[initial-sequence documentation](https://www.stat.umn.edu/geyer/mcmc/library/mcmc/html/initseq.html)
and [covariance-estimation notes](https://www.stat.umn.edu/geyer/8054/notes/initseq.html).

Fixed-length circular moving-block bootstrap uses 20/50/100/200 ps sensitivity,
2000 replicates and seed 20260924. Blocks never cross stage boundaries.
For the primary conditional mean interval select the first available length
at least five estimated tau; if none is long enough, flag insufficiency rather
than certify stationarity. First-half/second-half contrasts resample each
half separately and are conditional on local stationarity. Five 200 ps blocks
per ns give weak uncertainty. Per-property unadjusted 95% diagnostic intervals
are not a family-wise hypothesis test, a formal equivalence test, or a chosen
burn-in criterion. A flat potential or density does not prove phi/psi mixing.

## TIP3P density: a contextual reference, not a threshold

Original TIP3P, CHARMM-modified TIP3P and Ewald-reparameterized TIP3P are not
interchangeable. The [native LAMMPS parameter comparison](https://docs.lammps.org/Howto_tip3p.html)
distinguishes their oxygen LJ parameters and the CHARMM hydrogen LJ terms.
The script verifies every master water's actual charges, LJ parameters and
rigid distances and reports the finite solute mass fraction. The Amber master
also uses its actual H–H distance, not a rounded literature angle substituted
into the topology.

The primary [2021 systematic water-model comparison](https://doi.org/10.1021/acs.jcim.1c00794)
reports original TIP3P density 0.980 ± 0.006 g/cm³ in its Table 6; the reported
± quantity is a sample standard deviation, not an acceptance band or uncertainty
of this peptide system's mean. Its [author preprint](https://chemrxiv.org/engage/api-gateway/chemrxiv/assets/orp/resource/item/60e2a4a6f7373f34eb451ccc/original/a-systematic-comparison-of-the-structural-and-dynamic-properties-of-commonly-used-water-models-for-molecular-dynamics-simulations.pdf)
provides the table. Its [author-hosted full text](https://www.researchgate.net/publication/353012953_A_systematic_comparison_of_the_structural_and_dynamic_properties_of_commonly_used_water_models_for_molecular_dynamics_simulations)
specifies 2000 waters, 298.15 K, 1 atm, PPPM, 1 fs steps and 20 ns NPT sampling.
This is not an exact 300 K/1 bar/10 Å finite-peptide reference. Its LJ cutoff/
tail convention has not been matched to this bundle; a protocol-matched
pure-water density was not measured here. Literature entries at 298.15 K
cannot silently be relabeled 300 K.

[Price and Brooks (2004)](https://pubmed.ncbi.nlm.nih.gov/15549884/) explicitly
reparameterized TIP3P for Ewald treatment and reported 0.997 g/mL at 25 °C and
1 atm for their new potential. That value must not be assigned to unchanged
original TIP3P. Their work also treats long-range LJ corrections separately,
showing why cutoff and dispersion conventions belong in a density comparison.

The actual master uses Amber's TIP3P variant: oxygen charge −0.834 e,
sigma 3.1507524 Å and epsilon 0.1520 kcal/mol, hydrogen charge +0.417 e and
zero hydrogen LJ. Oxygen mass is 16.0 Da. Its two O–H distances are 0.9572 Å
and H–H is 1.5136 Å, implying 104.4906°, not a substituted 104.52°. These small
parameter/geometry rounding differences from literature tables are explicit,
not a claim that a tabulated model is byte-identical. The 144.176 Da peptide is
0.363757% of the system mass and one of 2193 molecules (0.045600 mol%).

The present runs use these original-family Amber TIP3P parameters, unswitched 10 Å LJ,
64³/order-4 PME or PPPM, and analytical native dispersion corrections.
The retained static diagnostic establishes C6-only versus C6+C12/pair-count
tail differences; no density effect has been separately measured or fitted
away. The observable is the total density of one capped peptide plus 2192
waters in a finite periodic box, not bulk pure water. Solute partial volume,
finite-size effects, temperature convention, finite timestep, barostat and
sampling uncertainty preclude identifying 0.985 g/cm³ as a universal pass
value. Conversely, deviation from experimental real-water density alone does
not establish a code failure in a TIP3P simulation.

## Retained development evidence

The first CPU pass (`run-01`) stopped at a strict NAMD control check because
it inspected the managed wrapper rather than the native `.namd` sourced by
that wrapper. Both wrapper and sourced file are now hash-bound and the source
directive is checked. All three native inputs do specify `useGroupPressure yes`.
This was an analysis path-binding correction, not a native runtime or protocol
change. The original failed receipt remains outside the frozen delivery.
The complete preliminary `run-02` is retained; the final pass additionally
records fixed endpoint windows, pointwise CI scope and source-review details.
