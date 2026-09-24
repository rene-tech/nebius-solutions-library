# Alanine free energies and full-stage diagnostics

Additive analyses of the frozen four-engine ACE–ALA–NME ff14SB/TIP3P
comparison, plus a separate native GROMACS umbrella campaign. Original
trajectories are never repaired, replaced by display interpolation, or silently
extended. Successful execution and statistical convergence are separate claims.

## Entry points

| Work | Code | Methods and measured evidence |
|---|---|---|
| Four-engine common-grid Ramachandran surfaces, basin populations and block uncertainty | `ramachandran.py` | [Methods](RAMACHANDRAN.md), [original 1 ns results](RAMACHANDRAN_RESULTS.md) |
| Native 100 ps NVT / 100 ps NPT / 1000 ps production time series, running means, density and circular correlations | `equilibration.py` | [Methods/reference values](EQUILIBRATION.md), [independent verification](EQUILIBRATION-VERIFICATION.md) |
| Native φ-umbrella inputs and exact-binary CV/energy check | `umbrella_campaign.py` | Protocol below |
| Ordinary isolated batch jobs; no infrastructure or quota edits | `umbrella_batches.py` | [Batch contract](UMBRELLA_BATCHES.md) |
| Native frame, topology, constraint, chirality and φ/ψ correspondence validation | `validate_umbrella_native.py` | [Validation scope](UMBRELLA-NATIVE-VALIDATION.md) |
| Exact native periodic WHAM, within-window temporal bootstrap and unbiased overlay | `umbrella_wham.py` | [Method, units, sources and reproducible commands](UMBRELLA_WHAM.md) |
| Complete-cohort binding and immutable bucket publication | `assemble_umbrella.py`, `publish_results.py` | Explicit file manifests, conditional writes and full-download SHA256 verification |

## Umbrella protocol

Use the exact frozen GROMACS topology: one capped alanine dipeptide plus 2192
TIP3P waters, 6598 atoms, original Amber ff14SB parameters and Amber 1–4 factors.
Source coordinates are the original equilibrated final GROMACS state.
Construct each starting φ by rigidly rotating the downstream bonded component
about N–CA; preserve bonds, chirality, ψ and solvent, then minimize natively.

- 24 distinct periodic centers: −180, −165, …, +165°. Do not duplicate ±180°.
- Fixed harmonic φ restraint 200 kJ mol⁻¹ rad⁻²; native reference/pullx angles
  are degrees. ψ is recorded as a second, strictly zero-force coordinate.
- 100 ps NVT, 100 ps NPT, then the full requested 2000 ps production per window.
  Equilibration is additional to—not part of—the 48 ns total production.
- 300 K, 1 bar in NPT, 2 fs, SD/Langevin relaxation 1 ps, native C-rescale barostat,
  the reference 64³/order-4 PME grid, unswitched 1 nm cutoffs and original
  dispersion corrections. Native LINCS 8/2 H-bond constraints and SETTLE.
- Instantaneous φ/ψ/force output every 0.1 ps; real coordinates every 1 ps.
  No trajectory interpolation, subsampling to a shorter run, or pseudocounts.
- NVT/NPT/production Langevin seeds are `202609240+3*window_index+stage_index`
  for stage indices 0/1/2. Initial velocity seed is `202609240+3*window_index`.
  All supplied and expanded native parameters remain in inputs, TPRs and logs.

The exact NVIDIA-derived worker image is
`gromacs@sha256:14ffdae0f0389c7771bae8791c56a5e21736ece630bd11dfb0f3b6a78f8cd643`
in the existing project registry. It reports 2026.2-dev. Its native dihedral
signs, degree/radian units and nonzero harmonic energy were checked using the
actual binary before any GPU submission. Synthetic native-WHAM tests remain
separate from actual alanine evidence. The exact customer SDK image and all
real operation IDs are retained in campaign receipts. GPU placement is through
the platform scheduler, not direct hand-created GPU Pods.

## Reproduce from retained native data

All deliverables go under
`s3://renes-bucket/four-engine-alanine-20260923/advanced-analysis-20260924/`.
The final bucket index explains archive extraction and names the manifest used
for actual WHAM. `original-four-engine-delivery.tar.gz` contains the unchanged
`delivery-02` directory, including its complete file-hash inventory.

```sh
python comparison/advanced-analysis/ramachandran.py \
  --delivery /data/delivery-02 --output /new/ramachandran \
  --seed 20260924 --bootstrap-replicates 50000
python comparison/advanced-analysis/equilibration.py \
  --delivery /data/delivery-02 --output /new/equilibration
python comparison/advanced-analysis/umbrella_wham.py analyze \
  --manifest /data/umbrella/windows.json --output /new/wham \
  --bins 180 --bootstrap 200 --block-ps 100 --seed 20260924
```

Native-WHAM commands and pinned analysis
dependencies are documented in the linked method files. Output paths must be
new; old scientific evidence is not overwritten. The native analysis runner
uses bounded CPU-only Docker, with no credentials, network or GPU reservation.
Submitting a new simulation is different: use the existing typed GROMACS
workflow/customer SDK with the retained immutable input bundles and your own
authorized API key. Keys and storage secrets are not distributed with results.

## Interpretation

The original 1 ns trajectories have few effective slow-coordinate samples and
no observed αL visits. A zero observed population is not a zero equilibrium
population or a zero-width error bar. Conditional bootstrap overlap is not
proof that engines are equivalent. Some bulk-density contrasts are resolved,
and native energy/temperature/pressure conventions remain explicitly distinct.
In particular, AMBER's original NVT pressure was not computed and cannot be
invented from its zero placeholders. Published TIP3P density depends on water
variant, temperature, cutoff treatment and system composition.

The umbrella PMF must pass actual overlap and input checks, but even then its
bootstrap uncertainty is conditional on visited states. Orthogonal ψ mixing
and first/second-half PMFs are reported; 48 ns aggregate sampling does not prove
convergence. Further simulation is a separate scientific decision, not an
automatic extension hidden in this campaign.
