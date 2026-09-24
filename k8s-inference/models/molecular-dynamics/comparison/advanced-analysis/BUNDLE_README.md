# Alanine dipeptide analysis and umbrella-sampling delivery

Bucket: `renes-bucket`

Prefix: `four-engine-alanine-20260923/advanced-analysis-20260924/`

Start with [RESULTS.md](RESULTS.md). It distinguishes successful native execution
from statistical convergence. All numerical plots use native trajectories,
not interpolated visualization frames. Original simulations are unchanged.

## Read or view

| File | Contents |
|---|---|
| `plots/free-energy-common-scale.png` / `.pdf` | Four-engine 2×2, 10° Ramachandran free-energy surfaces, common scale |
| `plots/basin-populations.png` | β/PPII, α-R, α-L populations and conditional block-bootstrap intervals |
| `plots/{gromacs,namd,amber,lammps}-full-stages.png` | Native equilibration/production series and running means |
| `plots/circular-autocorrelation.png` | Circular φ/ψ correlations |
| `plots/production-comparison.png` | Cross-engine bulk observables |
| `plots/phi-pmf-and-unbiased-overlay.png` | Native periodic umbrella WHAM versus the original 1 ns φ histogram |
| `plots/window-overlap.png` | Umbrella sampling support, including the periodic seam |
| `reports/ramachandran.md` | Basin definitions, uncertainty, engine comparison and sampling estimates |
| `reports/equilibration.md`, `reports/EQUILIBRATION.md` | Per-property results, missing data, native conventions and TIP3P references |
| `reports/umbrella-wham.md` | Actual enhanced-sampling result and block-length sensitivity |
| `tables/ensemble-mean-comparisons.csv` | Original production mean contrasts and conditional intervals |
| `tables/phi-pmf-100ps.csv`, `tables/phi-pmf-200ps.csv` | Full PMF, both bootstrap intervals and unbiased overlay |

The plot names in the publication manifest are authoritative; auxiliary plots
and complete numerical arrays are also retained in the analysis archives.
Gray or missing bins in the original histograms represent unobserved states.
Unvisited α-L has unresolved uncertainty, not a zero-width confidence interval.

## Complete archives

- `original-four-engine-delivery.tar.gz`: extracts `delivery-02/`, including
  the original master, converted inputs, all four native trajectories and
  logs, scripts, prior validation, visuals and unchanged hash inventory.
- `ramachandran-analysis.tar.gz`: extracts `analysis-02/`. Every native
  φ/ψ record, common-grid counts, basin assignments, nonoverlapping block
  means, bootstrap arrays and grid/basin/block-size sensitivity are included.
- `equilibration-analysis.tar.gz`: extracts `run-03/`. Full-stage native
  observables, running means, correlation functions, comparisons and receipts.
- `umbrella-inputs.tar.gz`: extracts `fixtures-04/`. All 24 individual
  input archives, scientific parameters, seeds and preparation provenance.
- `umbrella-submission-batches.tar.gz`: extracts `batches-01/`. The submitted
  8/8/7 batch requests for windows 01–23, original per-window requests,
  transformed paths, exact archive hashes and runtime schema checks.
  Window 00 used its retained individual request.
- `umbrella-native/window-NN.tar.gz`: each extracts `window-NN/`, containing
  `request.json`, `result.json`, and all native files under `data/`.
  All 24 must be extracted **inside `umbrella-native/`** for the portable
  `umbrella-native/windows.json` manifest. It is bound to each archived
  TPR, pull series and result hash; `windows-original.json` is provenance
  only and retains the original machine's paths.
- `umbrella-wham-100ps.tar.gz` and `umbrella-wham-200ps.tar.gz`: native WHAM
  runs, TPR audits, point/half profiles, all 200 bootstrap profiles per
  analysis, temporal block starts, seeds, exact commands and receipts.
  These extract `real-100ps-01/` and `real-200ps-01/`, respectively.
- `reproduction-source.tar.gz`: tracked source under
  `k8s-inference/models/molecular-dynamics/`. `source-provenance.json`
  identifies the exact repository commit and archive hash. No private
  engine source, model keys, registry passwords or storage secrets are included.
- `method-and-execution-evidence.tar.gz`: extracts `evidence/`. Separate
  method tests, original-data checks, actual native-window validation,
  archive verification, completed operation histories and the documented
  two-window capacity requeue. Synthetic method tests are **not** real MD.
  Failed setup/validator receipts remain explicitly failed, not rewritten.

`publication-manifest.json` and `SHA256SUMS` bind the delivered files.
`upload-verification.json` records full object-download hash verification;
it is a publication receipt, not evidence of scientific convergence.

## Reproduce the analyses locally

Use a new directory with enough space for compressed and extracted data
(at least 20 GB is a reasonable allowance). Use your own authorized bucket
credentials; access keys are intentionally absent from this bundle.

```sh
aws --profile YOUR_PROFILE --endpoint-url https://storage.eu-north1.nebius.cloud \
  s3 sync s3://renes-bucket/four-engine-alanine-20260923/advanced-analysis-20260924/ \
  ./alanine-study
cd alanine-study
sha256sum -c SHA256SUMS
tar -xzf reproduction-source.tar.gz
tar -xzf original-four-engine-delivery.tar.gz
for archive in umbrella-native/window-*.tar.gz; do
  tar -xzf "$archive" -C umbrella-native
done
python3.12 -m venv .venv
.venv/bin/python -m pip install \
  -r k8s-inference/models/molecular-dynamics/comparison/advanced-analysis/requirements.txt
.venv/bin/python k8s-inference/models/molecular-dynamics/comparison/advanced-analysis/ramachandran.py \
  --delivery delivery-02 --output reproduced-ramachandran \
  --seed 20260924 --bootstrap-replicates 50000
.venv/bin/python k8s-inference/models/molecular-dynamics/comparison/advanced-analysis/equilibration.py \
  --delivery delivery-02 --output reproduced-equilibration
.venv/bin/python k8s-inference/models/molecular-dynamics/comparison/advanced-analysis/umbrella_wham.py analyze \
  --manifest umbrella-native/windows.json --output reproduced-wham-100ps \
  --bins 180 --bootstrap 200 --block-ps 100 --seed 20260924
.venv/bin/python k8s-inference/models/molecular-dynamics/comparison/advanced-analysis/umbrella_wham.py analyze \
  --manifest umbrella-native/windows.json --output reproduced-wham-200ps \
  --bins 180 --bootstrap 200 --block-ps 200 --seed 20260924
```

WHAM uses the exact pinned GROMACS image, Docker and CPU resources only.
Pull authorization for the private registry is required if the image is not
already cached. The image and binary path are recorded in `UMBRELLA_WHAM.md`;
alternatively its CLI accepts `--image '' --gmx /your/compatible/gmx`, but
that is not a claim of byte-identical results from another binary. The Python
environment alone does not supply native GROMACS. All output directories
must be new. Analysis needs neither a live model endpoint nor a GPU.

## Reproduce native simulations

Use the retained input/request archives and the existing typed GROMACS
workflow with your own authorized API key. The archived source includes
`gromacs/qualification/run_image_customer_client.py` and the actual runtime
contracts. It accepts an input fixture, pinned client image, private key
file, output path and idempotency key. Do not reuse the revoked temporary
campaign credential or reuse an old operation's idempotency key when you
intend to run a **new** simulation. Each `result.json` records the expanded
native commands, step completion, artifacts and measured performance.

Protocol: unique centers −180 through +165° in 15° increments; fixed native
φ restraint 200 kJ/mol/rad², unrestrained ψ monitor, 300 K, 1 bar, 2 fs,
100 ps NVT + 100 ps NPT + full 2 ns production/window, coordinates each
1 ps and instantaneous pull data each 0.1 ps. Per-window/per-stage seeds
and all actual PME, constraint, thermostat and barostat settings are retained.
Hardware or binary changes need not reproduce stochastic trajectories
bit-for-bit; compare controlled ensemble properties instead.

The requested campaign is complete without secretly extending its sampling.
Neither the original 1 ns data nor a converged WHAM solver proves global
thermodynamic convergence. Read the ψ-mixing and uncertainty limitations
before interpreting the PMF or planning a longer study.
