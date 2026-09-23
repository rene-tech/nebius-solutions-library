# Native temperature observables and finite-trajectory uncertainty

H100 AMBER is the primary canonical comparison. L40S is a sensitivity control,
not a new requested production length or an independent-seed convergence study.
All values below use the actual complete 1 ns production, 1,000 samples at 1 ps.
Native reported temperatures are preserved, including the lower AMBER means.

## Measured observables

Intervals are approximate 95% Student-t intervals of twenty nonoverlapping 50 ps
block means. They require sufficiently independent blocks and stationarity;
they are not simultaneous acceptance bounds or proof of convergence.

| Run and temperature observable | Mean K | 50 ps block SEM K | Approximate interval K |
|---|---:|---:|---|
| GROMACS L40S native reported | 300.190 | 0.165 | 299.844–300.536 |
| NAMD H100 native reported | 300.144 | 0.117 | 299.899–300.390 |
| AMBER H100 native reported | **298.328** | 0.159 | **297.995–298.662** |
| AMBER H100 independently mass-weighted saved current velocities | 300.181 | 0.159 | 299.848–300.514 |
| AMBER L40S native reported, sensitivity control | **297.627** | 0.142 | **297.330–297.924** |
| AMBER L40S saved current velocities, sensitivity control | 299.489 | 0.138 | 299.200–299.779 |

The primary AMBER native reported mean is 1.672 K below target, outside its
within-run block interval. It must not be silently accepted merely because an
arbitrary broad temperature bound includes it. The saved-velocity kinetic
observable differs and is approximately target-consistent for H100. It is not
used to overwrite the native log or the main thermodynamics table.

L40S still has a 0.511 K deficit in the saved-velocity observable; its first and
second half means are 299.766 and 299.213 K. H100's corresponding means are
300.115 and 300.248 K. This finite-run sensitivity is retained, not declared
resolved or used to extend sampling beyond the requested 1 ns. No requirement
that every approximate interval contain 300 K is imposed after seeing the data.

## Why the native estimators differ

The exact private PMEMD26 source trace is documented separately in
[`amber/qualification/TEMPERATURE_ESTIMATORS.md`](../../amber/qualification/TEMPERATURE_ESTIMATORS.md),
corrected commit `f1a7f64826f95a482064c017ec29c10285604986`. The actual CUDA PME kernel
computes two quantities:

`reported EKE = c_ave/8 × Σ m |v_current + v_previous|²`

`current-velocity KE = 1/2 × Σ m |v_current|²`

Here `c_ave = 1 + gamma*dt/2 = 1.001`. The middle integrator saves the previous
velocity before its kick/thermostat/constraint sequence. The native printed
temperature uses EKE; positive `ntwv` with the actual default output convention
writes the current velocities. Thus the archived 1 ps velocity frames and the
printed log do not evaluate the same kinetic observable. Successive saved
frames are 1 ps apart, not the required adjacent 2 fs values: they cannot
reconstruct each printed midpoint estimator exactly.

For H100, current-velocity KE averages 3938.806951 kcal/mol, compared with native
printed EKtot 3914.491287 kcal/mol. Their framewise correlation is 0.993977, but
the mean difference is 24.315663 kcal/mol, or approximately 1.85 K. This is a
source-backed observable distinction, not evidence fabricated from target T.

The source-selected DOF is `3×6598−6588 = 13206`; this Langevin/middle configuration
does not remove three COM DOF. The diagnostic uses the **actually imported**
`gbl_constants_mod` constant `KB = 8.31441/4184 = 0.00198719168260 kcal/(mol K)`.
It numerically matches the CUDA thermostat's `0.00831441/4.184` constant.
The initial diagnostic followed a similarly named value in unrelated
`constants.F90`; this is corrected, not silently ignored. It shifted calculated
temperatures by only about 0.00365 K and cannot explain the 1.85 K discrepancy.
With the correct binding, printed EKtot/TEMP imply 13,205.997 DOF on average,
consistent with 13,206 within printed-temperature precision. NetCDF's declared
20.455 velocity scale factor is honored once; physical Å/ps velocities are
then divided by 20.455 for native internal kinetic units. Raw files are unchanged.

An explicitly different COM-removed estimator using 13,203 DOF gives 300.183159 K
for H100 and 299.490477 K for L40S, changes of only about 0.001–0.002 K. COM
bookkeeping therefore does not explain the observed estimator offset.

The [AMBER26 manual](https://ambermd.org/doc12/Amber26.pdf), §23.6, describes
leapfrog-middle momentum sampling and constrained integration. Its harmonic-limit
property does not prove exact ensemble sampling for this finite-step, anharmonic
system. No thermostat-failure or exact-ensemble claim follows from the diagnostic
alone. Parent scientific acceptance must distinguish operational completion,
observable conventions and the limits of this deliberately short comparison.

## Autocorrelation and sensitivity

The diagnostic calculates the unbiased lag covariance, integrates the initial
positive ACF through its first nonpositive lag, and reports the statistical
inefficiency. It also reports 10, 20, 50, 100 and 200 ps block means, SEM and
approximate intervals. Only five 200 ps blocks are available, so that estimate
is particularly weak. ACF window selection is a transparent diagnostic, not an
independent-replicate uncertainty calculation.

| Observable | Initial-positive-window effective sample estimate | ACF-adjusted SEM K | 100 ps block interval K |
|---|---:|---:|---|
| GROMACS native | 760.5 | 0.133 | 299.882–300.499 |
| NAMD native | 610.1 | 0.148 | 299.859–300.430 |
| AMBER H100 native | 718.9 | 0.142 | 297.964–298.693 |
| AMBER H100 saved current velocities | 507.6 | 0.169 | 299.838–300.525 |
| AMBER L40S native | 634.5 | 0.149 | 297.316–297.939 |
| AMBER L40S saved current velocities | 636.4 | 0.150 | 299.191–299.788 |

## Reproduction

`temperature_diagnostic.py` rechecks the passed analysis output hashes, matches
the AMBER topology hash, validates all 1,000 native step/time pairs and velocity
frames, checks units/scale/finite values, and hashes the unchanged native inputs.
The source-review note is bound by hash rather than redistributing licensed code.

Actual receipt:
`/home/tux/fs2-alanine-analysis-20260923/temperature-diagnostic-03.json`, SHA256
`4e71e6f2d1210a5d931f4b0315ec905e50ede520df9482268ada88420d659575`.
It includes both temperatures, native pressure diagnostics, all block means,
ACFs, kinetic energies, DOF/constants and input/source provenance. Earlier
diagnostics01/02 are retained with the initial constant-reference error; they
are superseded by03, not relabeled. No new MD steps were generated.
