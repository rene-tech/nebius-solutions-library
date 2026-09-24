# Original 1 ns alanine trajectories: measured results

Analysis completed on 2026-09-24, offline and CPU-only. The original frozen
`delivery-02` was mounted read-only; all 548 files (1,525,211,038 bytes) passed
its manifest checks. No native simulation, cloud write, GPU allocation or change
to the original delivery was performed. These results do not include the dense
20 ps continuations or the separate umbrella campaign.

All four original trajectories contributed exactly 1,000 positive 1 ps samples.
Phi/psi were recomputed from native coordinates, agreeing with the frozen
analysis to 5.69e-14 degrees and with an independent MDAnalysis calculation to
less than 0.000431 degrees. The original source/file/step-bound LAMMPS SHAKE
boundary policy was revalidated without a new tolerance waiver or trajectory
repair. Full values and provenance are in the output receipt.

## Populations and uncertainty

Fixed geometric basin definitions and all methods are in
[RAMACHANDRAN.md](RAMACHANDRAN.md). Fractions, not percentages:

| Basin | GROMACS | NAMD | AMBER | LAMMPS |
| --- | ---: | ---: | ---: | ---: |
| β | 0.219 | 0.183 | 0.163 | 0.139 |
| PPII | 0.551 | 0.463 | 0.421 | 0.382 |
| αR | 0.230 | 0.354 | 0.415 | 0.477 |
| αL | 0 | 0 | 0 | 0 |
| Residual | 0 | 0 | 0.001 | 0.002 |
| Combined β/PPII | 0.770 | 0.646 | 0.584 | 0.521 |

The zero αL counts have **no estimated confidence interval, relaxation time or
required physical sampling time**. No inference of zero equilibrium probability
or correlated-frame rule-of-three upper bound is made. Residual observations
are too few for a physical sampling forecast.

| αR diagnostic | GROMACS | NAMD | AMBER | LAMMPS |
| --- | ---: | ---: | ---: | ---: |
| Sampled runs in basin | 4 | 4 | 9 | 5 |
| Estimated τ_int (ps) | 58.67 | 31.35 | 34.18 | 102.54 |
| Estimated effective samples | 8.52 | 15.95 | 14.63 | 4.88 |
| First-half / second-half fraction | .064 / .396 | .224 / .484 | .354 / .476 | .112 / .842 |
| Conditional 95% interval, 200 ps blocks | [.032, .512] | [.203, .502] | [.226, .609] | [.172, .781] |
| Conditional total ns for ±0.05 population half-width | 31.93 | 22.03 | 25.50 | 78.61 |

Sampled runs are not independent transition counts. All effective-sample
estimates are weak, and the substantial half-run changes make stationarity
questionable. The predeclared block rule requires at least 514.37 ps; the
largest tested length, 200 ps, leaves only five nonoverlapping blocks and does
not satisfy that rule. Bootstrap intervals are conditional diagnostics, not
demonstrations of convergence. Sampling projections use the measured p and g
in a stationary Bernoulli approximation; they are optimistic planning scales,
not sufficient-run guarantees or recommendations to stop at those lengths.

At 200 ps, none of the 36 predeclared pair/basin comparisons excludes zero,
nominally or with Bonferroni family correction. This is not evidence of engine
equivalence. At 5 and 10 ps the family-adjusted exclusion counts are five and
two; at 20/50/100/200 ps they are zero. Selecting a short block after seeing
that result would exaggerate certainty. One trajectory per engine cannot
separate engine differences from stochastic undersampling. Independent replicas
and the separate enhanced-sampling campaign require their own checks.

The independently implemented circular trace τ/Neff values agree with the
parallel all-stage diagnostics. In engine order, φ τ is
4.73315/2.71712/3.27616/4.00589 ps and ψ τ is
51.90646/27.89744/30.66988/91.91666 ps. The slower basin-indicator diagnostics
above are not replaced by the faster φ coordinate correlation.

## Free-energy maps

Common 10° bins, 300 K, and `F=-RT ln(P_bin)` use no smoothing or pseudocount.
All four plots share a 0–9.65608683 kJ/mol scale after subtracting each observed
minimum. Only 183/192/216/201 of 1,296 bins are visited, respectively; unvisited
bins are masked, not measured barriers. Basin-boundary and grid-width
sensitivity remain available. The common maps, population intervals and
block-size sensitivity plots were visually inspected for labels, masking,
shared scales and agreement with the numerical outputs.

## Reproduction and evidence

Final output directory:
`/home/tux/fs2-alanine-analysis-20260924/ramachandran/analysis-02`.
Its `REPORT.md` is the full human-readable report; `receipt.json` inventories
37 output files, native images/inputs, code hashes, methods and environment.
All CSV data, 50,000-replicate bootstrap arrays, sensitivity outputs and figures
are retained. The earlier `analysis-01` remains as history; `analysis-02`
adds the explicit N/2 lag cap and clearer rare-state limitations. No native
failure is concealed by this analysis revision.

| Evidence | SHA256 |
| --- | --- |
| Final analysis receipt | `74ff17e034a5f05f26dc19c0951d12cf46b19df8cabbf41456200ac484fff11e` |
| Separate `output-verification-02.json` | `a4133c246279db8a9ce0d87f22b5f16b43d7476ad56c6c06c44e5378c86cf82c` |
| Native delivery manifest | `e9df4cee5404587cbed77387e7abb52636720be37e10412e75e8d6718e40e01e` |
| Analysis script | `f7e5d28c80021bd75e58b1c7ab20404f2f4d1f41ef0dc36d1239c5e83e13c0d8` |
| Unit/saved-output verification source | `fa95f47ae5672bb2b632c2d635dd46f27dc76c29e4b26a2610f9b3b5d62b05dc` |

Analysis source commits: `526856e7d0d915ac3b8566984a51b7fe733d4395` and
`34cb870351cd2af9094391afbd303e5f97d8d0a1`. The final verifier and this document
are a later additive commit; they do not change the frozen analysis output.

The exact client is
`cr.eu-north1.nebius.cloud/e00akg9ndpx77eaexh/lc@sha256:81a2f3b54a98299933d487ccca4e9eb3fbea3130257a5a818a83940127429a4d`.
It used Python 3.11.2, numpy 1.26.4, scipy 1.16.3, MDAnalysis 2.10.0 and
matplotlib 3.10.7. The command below ran with no network, GPU or credentials:

```sh
docker run --rm --network none --cpus 2 --memory 4g --user 1002:1002 \
  --env MPLCONFIGDIR=/tmp/fs2-rama-mpl \
  --entrypoint /opt/md-analysis/bin/python \
  --mount type=bind,src=/home/tux/worktrees/fs2-namd-grid64-20260923/k8s-inference/models/molecular-dynamics/comparison/advanced-analysis,dst=/source,readonly \
  --mount type=bind,src=/home/tux/fs2-alanine-comparison-20260923/delivery-02,dst=/delivery,readonly \
  --mount type=bind,src=/home/tux/fs2-alanine-analysis-20260924/ramachandran,dst=/output \
  cr.eu-north1.nebius.cloud/e00akg9ndpx77eaexh/lc@sha256:81a2f3b54a98299933d487ccca4e9eb3fbea3130257a5a818a83940127429a4d \
  /source/ramachandran.py --delivery /delivery --output /output/analysis-02 \
  --seed 20260924 --bootstrap-replicates 50000
```

The same image passed all **26 unit tests** with
`/opt/md-analysis/bin/python /source/test_ramachandran.py`. The separate
`--verify-output` mode passed every output hash/size and independent numerical
check; its receipt is outside `analysis-02`. All owned CPU processes finished.
Parent integration/upload is separate from this local analysis acceptance.
