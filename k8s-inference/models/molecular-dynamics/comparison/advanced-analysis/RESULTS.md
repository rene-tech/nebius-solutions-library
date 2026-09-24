# Alanine dipeptide: free energies, equilibration and enhanced sampling

2026-09-24. Original system: one ACE–ALA–NME molecule and 2,192 waters,
6,598 atoms, Amber ff14SB and the unchanged Amber TIP3P parameters. The four
original 1 ns production trajectories and their 100 ps NVT / 100 ps NPT
preparation remain immutable. Numerical results below use native coordinates
and logs, never the smoothed presentation videos.

Bucket root:
`s3://renes-bucket/four-engine-alanine-20260923/advanced-analysis-20260924/`.
The delivered `README.md` indexes plots, full reports, raw archives, exact
inputs/seeds, source and checksums.

## Original 1 ns basin populations

Fractions with conditional 95% circular moving-block bootstrap intervals,
50,000 replicates and common 200 ps blocks. Separate β and PPII values,
rectangular basin definitions, boundary/grid/block-size sensitivity and all
frame-level φ/ψ values are in `ramachandran-analysis.tar.gz`.

| Engine | β/PPII combined | α-R | α-L | Effective ψ samples in 1 ns |
|---|---:|---:|---:|---:|
| GROMACS | 0.770 [0.488, 0.968] | 0.230 [0.032, 0.512] | No visits; uncertainty unresolved | 9.63 |
| NAMD | 0.646 [0.498, 0.797] | 0.354 [0.203, 0.502] | No visits; uncertainty unresolved | 17.92 |
| Amber | 0.584 [0.390, 0.772] | 0.415 [0.226, 0.609] | No visits; uncertainty unresolved | 16.30 |
| LAMMPS | 0.521 [0.218, 0.825] | 0.477 [0.172, 0.781] | No visits; uncertainty unresolved | 5.44 |

The observed basin differences are not resolved by the retained conditional
pairwise bootstrap comparisons. **This is inconclusive, not evidence of
engine equivalence.** Five observed blocks per trajectory and estimated
α-R effective sample counts of only about 5–16 provide weak uncertainty.
The five-correlation-time block criterion would require at least 514 ps,
which these records cannot supply while retaining enough blocks.

For visited α-R populations, an optimistic plug-in stationary estimate for
a 95% population half-width of 0.05 is approximately 32 / 22 / 26 / 79 ns
total for GROMACS / NAMD / Amber / LAMMPS. These are planning scales, not
guaranteed convergence times; independent seeds and re-estimation are needed.
No α-L physical sampling time can be inferred from zero visits. For context,
an assumed 1% rare population needs roughly 9,508 *independent* samples for
20% relative precision at nominal 95% confidence; its unknown correlation
time prevents converting that into nanoseconds from these trajectories.

The common-grid 2×2 surfaces use 10° bins, 300 K and
`ΔF = −RT ln(P_bin/P_max)` in kJ/mol. Unvisited bins are gray/missing,
not zero-energy states or measured infinite barriers. No pseudocounts or
smoothing are used; the color scale is identical across engines.

## Equilibration and ensemble observables

Every engine has native full-stage series and stage-wise running averages
for potential, temperature, pressure, mass/box density and fitted peptide
backbone RMSD. Initialization rows and original time conventions are retained;
production statistics use the 1,000 positive-time frames. Circular correlation
uses sine/cosine trace covariance, with `τ_int = g Δt/2` and `N_eff = N/g`.

| Engine | Density (g/cm³) | Native T (K) | Native P (bar) | Potential (kJ/mol) | φ / ψ τ_int (ps) |
|---|---:|---:|---:|---:|---:|
| GROMACS | 0.984843 | 300.016 | 5.72 | −87821.76 | 4.73 / 51.91 |
| NAMD | 0.983338 | 300.185 | −8.62 | −87649.49 | 2.72 / 27.90 |
| Amber | 0.984548 | 298.328 | −12.37 | −87818.39 | 3.28 / 30.67 |
| LAMMPS | 0.984620 | 299.823 | −1.50 | −87662.38 | 4.01 / 91.92 |

- All NVT boxes start near 0.74646 g/cm³. NPT densification is a genuine
  transient; its full 100 ps mean is not treated as an equilibrium estimate.
- Most production bulk series have no detected first-/second-half change,
  but this does not certify equilibration. NAMD potential and LAMMPS backbone
  RMSD have detected changes under the conditional, unadjusted diagnostic.
  Slow peptide conformational equilibrium is not established for any engine.
- All production pressure confidence intervals include 1 bar. Native group,
  molecular and atomic virial conventions differ; instantaneous estimates
  are not interchangeable. Amber's NVT `PRESS=0` entries are placeholders:
  the original run did not calculate that pressure, so the plot shows it as
  unavailable rather than inventing measurements.
- Amber's printed temperature is a midpoint kinetic estimator. A separately
  labeled estimate from its saved current velocities is 300.181 K. Neither
  is silently substituted for the other.
- Pointwise conditional density differences remain for GROMACS−NAMD
  (+0.001505; 95% interval +0.000541 to +0.002438 g/cm³) and
  LAMMPS−NAMD (+0.001282; +0.000703 to +0.001886). These are not a
  multiple-comparison-adjusted claim or a diagnosis of an engine defect.
- GROMACS/Amber versus NAMD/LAMMPS mean potentials differ by about
  156–172 kJ/mol. The retained canonical-coordinate static range is only
  about 4.71 kJ/mol, so static convention offsets alone do not explain it.
  Sampling and finite-step integration/constraint/barostat effects have not
  been separated. Successful execution is not a force-field equivalence test.

The densities are broadly in the expected original-TIP3P range; 0.985 is not
a universal exact target. A [systematic water-model study](https://doi.org/10.1021/acs.jcim.1c00794)
reports 0.980 ± 0.006 g/cm³ for original TIP3P under its 298.15 K / 1 atm
pure-water protocol (± is sample standard deviation). That is not a matched
300 K / 1 bar finite-peptide calculation. The [Ewald-reparameterized TIP3P
study](https://pubmed.ncbi.nlm.nih.gov/15549884/) concerns a different potential.
The actual water geometry/LJ parameters, cutoff/tail treatment, solute mass
fraction and reference limitations are documented in the full report.

## Native umbrella campaign

All 24 requested native simulations have completed: centers −180, −165, …,
+165°, harmonic φ restraint 200 kJ mol⁻¹ rad⁻², 100 ps NVT + 100 ps NPT +
2 ns production each. This is 48 ns production plus 4.8 ns equilibration;
±180° is not duplicated. Each window has its own recorded seeds. ψ is an
unbiased recorded coordinate, not a hidden second restraint.

All windows passed native completion, time/frame cadence, topology, geometry,
constraint, chirality and coordinate-versus-recorded φ/ψ checks: 24 million
production steps, 48,000 positive-time coordinate frames and 480,000 pull
observations. The maximum constraint residual was 2.03×10⁻⁵ Å. Native
production performance was 612–781 ns/day for these small-system windows;
the differing inputs/placements are not a matched H100-versus-L40S comparison.

Native periodic `gmx wham` used 180 bins of 2°, the exact corresponding TPRs,
300 K and periodic harmonic biases. Both 100 ps and 200 ps temporal-block
analyses completed all 200 bootstrap draws without failure. All 180 bins had
support and remained finite in every draw. The original 1 ns overlay has only
69/180 occupied bins; its 111 missing bins remain blank. This expanded φ
coverage—not a declaration of equilibrium—is what enhanced sampling adds.

| Diagnostic | Observed result |
|---|---|
| Full-cohort φ PMF minimum | −71° |
| PMF maximum-minus-minimum span | 55.800 kJ/mol; conditional on sampled orthogonal states |
| Window overlap graph | Connected, including the ±180° seam |
| Seam overlap | 0.32175 |
| Weakest adjacent overlap | 0.02750 between +120° and +135° windows |
| First-/second-half PMF difference | 0.4149 kJ/mol RMS; 1.1001 kJ/mol maximum |
| 100 ps bootstrap 95% interval width | 0.7938 kJ/mol median; 1.1660 maximum |
| 200 ps sensitivity 95% interval width | 0.7361 kJ/mol median; 1.2281 maximum |

Intervals are pointwise, fixed-reference percentile intervals, not simultaneous
confidence bands or uncertainty for unseen states. Their finite-draw widths
need not increase monotonically with block length. The two analyses have
identical native point estimates; only temporal resampling differs.

**Global convergence remains unestablished.** The largest ψ correlation-time
estimate is 130.17 ps; 13 windows fail the five-τ block criterion at 100 ps
and six still fail it at 200 ps. First-/second-half ψ histogram total variation
reaches 0.5387, and window 12 has no observed crossing of the diagnostic
ψ half-circle. Those crossings are not metastable transition counts.
Narrow bootstrap bands cannot repair unvisited ψ states or initialization
memory. The positive-φ feature is therefore not a proven equilibrium α-L
population. Independent initial conformers/seeded replicas and targeted
orthogonal sampling would be a next study; no additional simulation was
silently added to this requested 24×2 ns campaign.

The independent check revalidated all 400 bootstrap profiles and their
quantiles, actual TPR/pull data, native output inventories and replayed
resampling hashes. Five of 480,000 observations exactly on two bin edges
follow native floating-point bin assignment differently from the auxiliary
NumPy overlap histogram. This is documented; no observations or support
were lost, and the native histogram/WHAM estimator remains authoritative.

## Execution caveat

Windows 22 and 23 initially reserved an unavailable H100 pool. An operator
requeued only their unstarted queue reservations; the original attempts are
retained and both second attempts succeeded on healthy H100 capacity. This
was not spontaneous GPU preemption, a native simulation failure or a change
to inputs, quotas, node groups or customer limits. The intervention is part
of the retained evidence, and this campaign is not a claim of unattended
platform-wide readiness.
