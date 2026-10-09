# Frozen-native equilibration diagnostic verification

The scientific analysis is bound to source commit
`c5a3384c154c299a56c60f18bc7d5d479443853a`. Its final output is
`/home/tux/fs2-alanine-analysis-20260924/equilibration/run-03`.
Passing means that the available-data analysis and its checks completed, not
that equilibrium or cross-engine statistical equivalence has been established.

## Independent output checks

The separate read-only verifier does not rerun a native engine or rewrite a
trajectory. It checks original file hashes, stage schedules, cumulative means,
missing AMBER NVT pressure, original production observables, circular statistics
from the independently implemented Ramachandran analysis, and PNG integrity.
Its output path must be new and outside the immutable delivery.

```bash
python tests/verify_equilibration.py \
  --analysis /home/tux/fs2-alanine-analysis-20260924/equilibration/run-03 \
  --delivery /home/tux/fs2-alanine-comparison-20260923/delivery-02 \
  --ramachandran /home/tux/fs2-alanine-analysis-20260924/ramachandran/analysis-02 \
  --output /path/to/new-verification.json
```

Run this from `comparison/advanced-analysis/` with the documented analysis
environment. The actual invocation used the existing, unmodified
`/home/tux/.venvs/fs2-four-engine-20260923/bin/python`.

The retained successful check is
`/home/tux/fs2-alanine-analysis-20260924/equilibration/verification-03.json`:

- 142 input/output file sizes and SHA-256 values match.
- 25,100 nonzero scalar observations and their per-stage cumulative means match.
- Each engine has 100 NVT, 100 NPT and 1,000 production nonzero samples at 1 ps.
- All 4,000 production angle and density observations match the frozen analysis;
  periodic angle differences are at most `5.7e-14` degrees.
- All eight circular statistical inefficiency, integrated correlation time and
  effective sample count results match the independent worker within `1e-10`.
- Native production thermodynamic observations are unchanged. AMBER's derived
  current-velocity mean matches its earlier source-bound diagnostic; the native
  printed temperature remains a separate quantity.
- All seven PNG files decode and have the expected nontrivial dimensions.

The 22 method tests passed on 2026-09-24. They cover circular wrapping and
rotation, biased trace covariance, constant/anticorrelated/nonfinite series,
time units, reproducible block resampling, insufficient half-window blocks,
native missing-field semantics, schedule validation, source binding, periodic
geometry and rigid-transform RMSD. Synthetic fixtures are method tests only.

```bash
python -m unittest discover -s tests -p test_equilibration.py -v
```

Visual inspection separately covered the four full-stage panels, production
comparison, circular autocorrelation and block-length sensitivity. Final AMBER
and circular panels were rechecked after layout padding was added: missing NVT
pressure remains an explicit gap, stages do not share a running mean, native
and current-velocity temperatures are distinct, and axes/legends are legible.

## Exact receipt hashes

| File | SHA-256 |
|---|---|
| `run-03/receipt.json` | `62553178402619b69928e0019156608b52c847284124d8fd67d11b31f85806a0` |
| `run-03/summary.json` | `7b0a450170283ea2059a5d5130228cc7a1bb228d5e2fd64daf35f7f1c409b057` |
| `run-03/REPORT.md` | `94458572fa141ca9f734e1dbcaeadebf09d2292d43857ca6b3aaeabf4b13e1c0` |
| `verification-03.json` | `4420279e4cd76413b0c60b833bc550e9579c2325ba95f9904ac78c255d7c89dd` |

The failed first extraction and preliminary second pass remain in `run-01` and
`run-02`. The first failure was an analysis binding error: NAMD's managed wrapper
sources the actual native stage control, rather than duplicating its pressure
directive. The correction verifies and hashes both files. Native inputs and
outputs were not changed.

## Scientific limits retained

All contrast intervals are conditional, pointwise/unadjusted, not
multiplicity-adjusted or formal equivalence tests. Several density and potential
mean contrasts are resolved, whereas slow conformational equilibrium remains
unresolved. The dynamic potential grouping is larger than the static-coordinate
energy range; its cause was not fitted away or uniquely diagnosed. No exact
AMBER NVT pressure replay is available. Literature density is contextual, not a
`0.985 g/cm³` pass threshold. No simulation, GPU allocation, cloud operation,
deployment, bucket write or frozen-delivery edit was performed in this task.
