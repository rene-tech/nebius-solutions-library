# Original four-engine Ramachandran statistics

`ramachandran.py` analyzes only the frozen original 1 ns production trajectories
in `delivery-02`. It does not read the new dense clips or umbrella campaign,
submit native work, touch cloud storage, or modify the source delivery.

The script verifies the entire delivery manifest, then streams all actual native
frames through its frozen strict readers. The source-bound LAMMPS closed-segment
SHAKE policy remains enforced. Phi/psi are recomputed from whole peptide
coordinates, cross-checked against the original frame CSV and independently
against MDAnalysis dihedral calculations. Initial records are retained, but all
statistics use exactly the 1,000 positive 1 ps samples per engine without
post-hoc burn-in removal. No angle or coordinate interpolation is performed.

## Frozen statistical conventions

- Angles wrap to `[-180,180)`. All rectangles are half-open.
- Extended β/PPII: φ < 0, and ψ ≥ 60° or ψ < −150°. β has φ < −90°; PPII
  has φ ≥ −90°. αR has φ < 0 and −120° ≤ ψ < 60°. αL has φ ≥ 0 and
  −60° ≤ ψ < 120°. Residual is the complement. Combined β/PPII is reported
  explicitly as an aggregate, not an additional exclusive category.
- These are fixed geometric regions, not learned metastable states. Nine
  boundary variants use β/PPII splits −100/−90/−80° and extended/αR splits
  50/60/70°. They are sensitivity analyses, not a search for significance.
- Shared 10° histogram edges and 300 K use `F=-RT ln(P_bin)`, no pseudocount or
  smoothing. Each observed minimum is subtracted for display; one colour scale
  covers all engines. Unvisited bins stay masked, not finite high barriers.
- Circular ACF uses trace covariance of centered `(cos θ,sin θ)`; ordinary
  angle differences across ±180° never enter the ACF. Separate sine/cosine and
  basin indicator diagnostics are retained. Autocovariance has denominator N.
- Geyer paired sums `(ρ0+ρ1),(ρ2+ρ3),...` stop before the first nonpositive pair
  and are monotonized. `g=max(1,-1+2Σpair)`, `τ_int=g Δt/2`, `Neff=N/g`.
  Estimation is capped at N/2 lag; the full ACF is retained diagnostically.
  Constant observables have null correlation estimates. This convention is
  coordinated with the parallel equilibration-diagnostics worker.
- Nonoverlapping block means and circular moving-block bootstrap use
  5/10/20/50/100/200 ps. Default 50,000 replicates and seed `20260924`; independent
  engine/block seed streams are saved. The common primary block is the first of
  20/50/100/200 ps at least five times the largest estimated τ, otherwise 200 ps
  explicitly marked inadequate. Few-block limitations remain visible.
- Constant/unvisited indicators never receive `[0,0]` or `[1,1]` confidence
  intervals. They provide no measured relaxation time or physical sampling
  requirement. Hypothetical rare-state planning is separated from inference.
- Basin probability-ratio free energies relative to β/PPII retain zero-count
  bootstrap draws as unbounded or undefined limits. They are never discarded
  to make an interval finite. Few-visit/effective-sample warnings are explicit;
  fewer than three sampled runs do not support time extrapolation here.
- All 36 pair/basin comparisons have nominal and Bonferroni-family intervals.
  Inclusion of zero is not equivalence; any exclusion remains conditional on
  stationary single-trajectory bootstrap assumptions. One run per engine does
  not identify between-replica variation or engine bias. Sparse histogram
  JS/TV distances at four grid widths are descriptive, not hypothesis tests.

Reversible stationary-chain theory motivates the Geyer diagnostic, but the
finite MD sequence is not thereby proven stationary/reversible/mixed. Likewise,
bootstrap resampling cannot discover unvisited states. For visited basins,
stationary Bernoulli CLT planning scales use `z² p(1-p)/ε²` effective samples,
then the observed g; these are conditional, potentially optimistic projections.
Zero-visit αL is never assigned an empirical equilibrium upper bound using
1,000 correlated frames. Independent replicas and the separate umbrella
campaign need their own convergence/overlap/orthogonal-mixing checks.

## Offline execution

Use the exact final client
`cr.eu-north1.nebius.cloud/e00akg9ndpx77eaexh/lc@sha256:81a2f3b54a98299933d487ccca4e9eb3fbea3130257a5a818a83940127429a4d`,
whose isolated `/opt/md-analysis/bin/python` provides numpy/scipy/MDAnalysis/
matplotlib. Run Docker without network, GPUs or credentials; mount the original
delivery read-only at `/delivery`, this source directory read-only at `/source`,
and a new output parent at `/output`. Use the actual host UID/GID.

```sh
/opt/md-analysis/bin/python /source/test_ramachandran.py
/opt/md-analysis/bin/python /source/ramachandran.py \
  --delivery /delivery --output /output/analysis-02 \
  --seed 20260924 --bootstrap-replicates 50000
/opt/md-analysis/bin/python /source/test_ramachandran.py \
  --verify-output /output/analysis-02 /output/output-verification-02.json
```

Do not reuse an existing output directory. Failures create `failure.json` while
retaining all partial output; a correction must use a new output path. The
receipt records every output SHA256, native input hashes, reader/script source,
method settings, environment and commands. Plots and numerical data are both
retained for independent inspection. No scientific convergence claim follows
from a `status:passed` analysis/validation receipt.

The separate saved-output verification checks every inventoried size/SHA256,
native sample counts and times, per-frame basin counts, histogram normalization,
zero masks, thermodynamic units, bootstrap array shape/composition, and explicit
frame-by-frame resampling arithmetic independent of the optimized block sums.
It writes only a new external receipt. See [the actual results](RAMACHANDRAN_RESULTS.md)
for the final evidence identities and scientific limitations.

## Primary references checked online

1. [Geyer, Practical Markov Chain Monte Carlo (1992)](https://www2.stat.duke.edu/homeweb/scs/Courses/Stat376/Papers/GeyerStatSci1992.pdf),
   with the [author's exact initial-sequence definitions](https://www.stat.umn.edu/geyer/mcmc/library/mcmc/html/initseq.html).
2. [Grossfield and Zuckerman (2009)](https://pmc.ncbi.nlm.nih.gov/articles/PMC2865156/):
   correlated sampling uncertainty and the limitation of unvisited states.
3. [Politis and Romano, circular-block resampling technical report](https://statistics.stanford.edu/technical-reports/circular-block-resampling-procedure-stationary-data):
   stationary-series block resampling.
4. [Holm (1979)](https://www.ime.usp.br/~abe/lista/pdf4R8xPVzCnX.pdf):
   familywise caution and the classical Bonferroni union bound. This script uses
   Bonferroni intervals, not a Holm p-value procedure.
5. [Drozdov, Grossfield and Pappu (2004)](https://www2.stat.duke.edu/~scs/SimGroup/PappuSolventPPII.pdf):
   representative conformer nomenclature only. Its OPLS/TIP5P populations are
   not used as targets for the present ff14SB/TIP3P system.
