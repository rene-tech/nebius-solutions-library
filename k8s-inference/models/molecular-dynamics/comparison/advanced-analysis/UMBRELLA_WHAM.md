# Periodic φ umbrella WHAM

This analysis uses **native `gmx wham`**, not a replacement WHAM solver. The
Python code audits inputs, prepares selected/resampled observations, runs the
upstream implementation, and reports uncertainty and sampling limitations.
It does not launch molecular dynamics, deploy services or write cloud storage.

## Native conventions and exact method validation

The pinned `gromacs@sha256:14ffdae0f0389c7771bae8791c56a5e21736ece630bd11dfb0f3b6a78f8cd643`
worker reports GROMACS **2026.2-dev**. The executable is
`/usr/local/gromacs/avx2_256/bin/gmx`. The published v2026.2 source is a semantic
reference, not a claim that an independently rebuilt release-tag executable is
identical to the NVIDIA binary. Native synthetic tests bind these conventions
to the actual image.

- Native dihedral centers and `pullx.xvg` values are **degrees**; the TPR's
  harmonic constant is **kJ mol⁻¹ rad⁻²**. The potential is
  `0.5 * k * radians(wrap(phi - center))**2`.
- Native WHAM internally converts the angular constant by `(π/180)²` before
  applying it to degree-valued displacements. Do not convert the observations
  to radians or apply that conversion twice.
- Use unique centers −180, −165, …, +165°. The ±180° endpoints are one state.
- Supply `-min -180 -max 180 -cycl`. The periodic nearest-image bias matters,
  not merely making the final plot look continuous. Do not use `-sym`.
- **Omit both `-auto` and `-noauto` when giving explicit bounds.** This binary
  rejects the explicitly set negative form as well. Bounds alone disable auto.
- Use `-it` with each matching original TPR and `-ix` with degree-valued pull
  observations. The φ umbrella must be the first coordinate. If unrestrained ψ
  is the second coordinate, give `-is` rows `1 0` and verify its native `k=0`.
- Use `-nolog` and normalize probabilities explicitly. In this implementation,
  native energy conversion does not turn zero-probability bins into infinity;
  plotting that output naively can disguise missing support. This wrapper
  leaves unsampled PMF bins as NaN/JSON null. There are no pseudocounts.

The synthetic command generates preprocessing-only native TPRs and deterministic
discrete-grid observations from known flat and asymmetric periodic PMFs. It
checks degree/radian conventions, the seam, explicit selection of an unbiased ψ
monitor, and repeatable native results from a fixed temporal resampling seed.
Noncyclic and degree-force-constant negative controls must fail the analytical
comparison. These are **method tests, not simulated alanine results**, and do
not measure continuum discretization error or physical equilibration.

The original single-coordinate synthetic check recovered the flat/asymmetric
PMFs within 0.000201/0.001153 kJ/mol, respectively; its noncyclic negative control
deviated by 109.446 kJ/mol. The earlier `-noauto` harness failure is retained,
not relabeled as a scientific run. Final receipts capture the current script
hash and exact native commands.

Sources:
[native WHAM reference](https://manual.gromacs.org/2026.2/onlinehelp/gmx-wham.html),
[native pull force-constant units](https://manual.gromacs.org/2026.2/user-guide/mdp-options.html#mdp-pull-coord1-k),
[v2026.2 WHAM implementation](https://github.com/gromacs/gromacs/blob/v2026.2/src/gromacs/gmxana/gmx_wham.cpp),
[Hub, de Groot and van der Spoel, 2010](https://doi.org/10.1021/ct100494z).

## Real-data contract

One manifest binds all 24 successful native windows and the original frozen
GROMACS 1 ns overlay. Paths are relative to the manifest or absolute. Optional
`tpr_sha256`, `pullx_sha256` and `frames_csv_sha256` fields are verified. All
inputs are hashed before and after analysis regardless.

```json
{
  "evidence_kind": "real-native-md",
  "temperature_k": 300,
  "windows": [
    {
      "id": "phi-minus180",
      "center_degrees": -180,
      "force_constant_kj_mol_rad2": 200,
      "status": "succeeded",
      "operation_id": "actual-hosted-operation-id",
      "tpr": "window-00/production.tpr",
      "pullx": "window-00/production-pullx.xvg",
      "production_start_ps": 0,
      "production_end_ps": 2000,
      "expected_dt_ps": 0.1
    }
  ],
  "unbiased": {
    "frames_csv": "/path/to/frozen/delivery-02/analysis/gromacs/frames.csv"
  }
}
```

The example has one illustrative row; execution requires **all 24** unique
centers, no replacement of failed windows. Actual production time origins may
differ, but each interval must span 2,000 ps. The analysis uses `(start,end]`,
excluding the extra time-zero sample, and checks every expected 0.1 ps sample.
Duplicate/nonmonotonic times or missing output fail instead of being silently
deduplicated or interpolated. Native pull output must be unaveraged and have
`pull-print-com`, `pull-print-ref-value`, `pull-print-components` all `no`:
columns are time/φ or time/φ/ψ. The exact TPR is audited for these settings, fixed
center and force constant, temperature, duration and output cadence. A biased ψ
cannot be silently dropped from a one-dimensional estimator.

## Reproduction

Use Python 3.12 and a local virtual environment with the pinned requirements.
The exact client image's `/opt/md-analysis/bin/python` also supplies analysis
libraries, but **does not contain a native GROMACS executable or Docker daemon**.
Library presence does not make this complete native analysis available there.
The default runner uses local, bounded CPU-only Docker containers with no network
and no GPU reservation. Alternatively give `--image '' --gmx /path/to/gmx` to use
a compatible local executable. New output directories are required; old evidence
is never overwritten.

```bash
python -m pytest -q test_umbrella_wham.py
python umbrella_wham.py validate-synthetic --output /new/path/synthetic
python umbrella_wham.py analyze --manifest /path/to/windows.json \
  --output /new/path/real-analysis \
  --bins 180 --bootstrap 200 --block-ps 100 --seed 20260924
```

The point estimate uses ordinary sample-weighted histogram WHAM, not an
autocorrelation-reweighted estimator. Each bootstrap independently resamples
contiguous chronological blocks **inside each window**, keeps every window's
native bias, and calls native WHAM again. Circular moving-block resampling wraps
the observed *time series*, preserving sample count; this is separate from the
angular boundary condition. The same row indices resample φ and ψ together.
The NumPy PCG64/SeedSequence seed and each block start are saved. Synthetic
time labels used only for native histogram ingestion do not replace the saved
physical sample times in provenance; native `-ac` is not used on those labels.

Native `-bs-method hist`/`b-hist` and `-histbs-block` resample/group **window
histograms**, not chronological time blocks. They are not substituted for the
requested block bootstrap. Native trajectory bootstrapping also differs from
resampling the actual observed blocks.

Intervals are pointwise 2.5–97.5 percentile intervals with a fixed reference bin
(default the bin containing −60°, centered −59° on the 2° grid), not independent
minimum-shifted profiles or simultaneous confidence bands. Failed draws remain
in the denominator. No interval is emitted for a bin missing in any requested
draw. Full, first-half and second-half native profiles are retained separately.

## What the results can and cannot establish

- Histogram overlap includes the +165/−180 seam, an observed-support graph, and
  raw per-window counts. Connected support is necessary, not sufficient.
- φ and unrestrained ψ use sine/cosine and half-circle-occupancy autocorrelation
  diagnostics. Ordinary linear-angle correlation across ±180 is avoided.
- First/second-half ψ histograms and crossing counts expose initialization
  memory. A constant occupancy has no estimable mixing time; it is not zero
  correlation or successful equilibration. Crossings can be rapid recrossings.
- A 100 ps block gives only about 20 blocks per 2 ns trajectory. Warnings flag
  blocks shorter than five estimated correlation times; those estimates can
  themselves be unreliable in short or metastably trapped records. Repeat with
  documented 50/100/200 ps blocks when assessing uncertainty sensitivity; do not
  choose a shorter block merely to obtain smaller error bars.
- The 1 ns unbiased overlay is one finite realization, not equilibrium truth.
  Its zero-count bins remain blank and its time-zero frame is excluded.
- Good φ overlap, native solver convergence and narrow conditional bootstrap
  bands **do not prove ψ or global equilibration**. Unvisited orthogonal basins
  cannot be recovered by bootstrap. No automatic simulation extension or change
  of force field, bias or thermostat follows from these diagnostics.

Outputs include the raw native probabilities/histograms, input and software
hashes, command logs, TPR audits, seed/block-start records, every bootstrap PMF,
φ PMF/density and unbiased overlays, window-overlap plots, ψ mixing plots, and a
machine-readable receipt. The combined scientific convergence claim remains
`not-established`; actual diagnostics and limitations decide further work.
