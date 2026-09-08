# AltumAge and clinical PhenoAge

These are two independent model Apps, not two interchangeable age estimators.
The side-by-side demo compares measured computing cost and latency, not accuracy.
All supplied fixtures are synthetic and contain no patient data.

| App | Input | Output target | Runtime |
| --- | --- | --- | --- |
| `altumage` | Normalized DNA-methylation beta values at 20,318 named CpGs | Predicted chronological age in years | Official weights, upstream pyaging network, CPU or explicit CUDA |
| `phenoage` | Chronological age and nine blood biomarkers | Mortality-derived phenotypic age in years | Small CPU formula; no model weights or GPU |

Both expose native `POST /v1/predict`, `GET /v1/health/ready`,
`GET /v1/health/live`, OpenAPI and `/metrics`. Existing platform APIs, MCP,
authentication, logical operations, queueing, Apps and usage accounting remain
the integration layer. This worker does not implement another control plane.
These are research/demo calculations, not diagnostic recommendations.

## Sources and reference precision

AltumAge uses [the original repository](https://github.com/rsinghlab/AltumAge/tree/696c477dac9b7641bf283c48af1cc9bb0a0803a3)
at `696c477dac9b7641bf283c48af1cc9bb0a0803a3` and its
[primary paper](https://www.nature.com/articles/s41514-022-00085-y).
The runtime reuses `AltumAgeNeuralNetwork` from
[pyaging's pinned MIT source](https://github.com/rsinghlab/pyaging/tree/edb1b37dc8a14a2ceed4f20a7d6fa05b8c3dabb9),
version 0.5.2. Original artifacts and conversion results are checksummed by
`altumage/materialize.py`. The official PyTorch artifact is 2,961,094 bytes;
the original Keras artifact is 8,637,312 bytes. The serving process loads a plain
state dictionary and JSON/NumPy preprocessing data, not the legacy pickles.
Upstream MIT notices are in `UPSTREAM-LICENSES.txt`.

Clinical PhenoAge is explicitly the **published supplement, rounded-coefficient
variant**, `levine-2018-supplement-rounded-v1`. It uses the primary
[Levine et al. paper](https://pmc.ncbi.nlm.nih.gov/articles/PMC5940111/) and
[Supplement 1](https://cdn.aging-us.com/article/101414/supplementary/SD1/0/aging-v10i4-101414-supplementary-material-SD1.pdf),
pages 1–2 and Table S1. The Gompertz shape is `0.0076927`; `0.0192` is the
variable-selection penalty, not that shape. The pinned pyaging implementation
currently substitutes `0.0192` in the mortality conversion, so this CPU formula
does **not** call `pyaging.models.PhenoAge`.
It also does not silently clamp low CRP measurements to pyaging's `0.01 mg/dL`
floor. Positive raw CRP is required and logged internally.

For the synthetic fixture age 50, albumin 45 g/L, creatinine 80 µmol/L,
glucose 5 mmol/L, CRP 0.1 mg/dL, lymphocytes 30%, MCV 90 fL, RDW 13%,
ALP 70 U/L and WBC 6 thousand/µL:

- Published rounded coefficients and correct shape: **41.90792243378 years**.
- Same rounded coefficients with pyaging's shape: **51.52728733548 years**.
- [BioAge's separately pinned, higher-precision implementation](https://github.com/dayoonkwon/BioAge/blob/b1f9fc02f086cd4aa74185f2335ab1366082e7fe/R/phenoage_calc.R#L105-L114): **41.83810863094 years**.

The first is this App's declared reference; the other two are not silently
treated as identical. Stable log-space algebra avoids numerical cancellation
and saturation in the intermediate mortality CDF. The output is the age score,
not a claim about an individual's mortality probability.

## Inputs

Clinical field names carry their units. `c_reactive_protein_mg_dl` is **raw
mg/dL**, not mg/L and not pre-logged. `white_blood_cell_count_10e3_per_ul` is
thousands of cells per microlitre, numerically equivalent to billions per litre.
All ten measurements must be present and finite. No inferred measurements,
normal-range substitutions or hidden unit conversions are made.

```json
{
  "samples": [{
    "sample_id": "synthetic-50",
    "age_years": 50,
    "albumin_g_l": 45,
    "creatinine_umol_l": 80,
    "glucose_mmol_l": 5,
    "c_reactive_protein_mg_dl": 0.1,
    "lymphocyte_percent": 30,
    "mean_cell_volume_fl": 90,
    "red_cell_distribution_width_percent": 13,
    "alkaline_phosphatase_u_l": 70,
    "white_blood_cell_count_10e3_per_ul": 6
  }]
}
```

AltumAge takes `cpg_sites` (all 20,318 unique trained CpG names) once per request,
then `samples[{sample_id,beta_values}]` with matching columns. Column order may
differ: the runtime explicitly reorders by name into the trained order. Duplicate,
missing or unexpected probe names are rejected. Input values are normalized
beta values in `[0,1]`, not raw IDAT intensity files. The original example uses
BMIQ-normalized input; assay normalization is an upstream data-preparation step,
not something the worker silently guesses. It applies only the supplied robust
scaler. Default missing-value behavior is an error. Explicit
`missing_values: "reference_median"` permits nulls, uses the original scaler's
training median and reports each sample's imputed CpG count.

The native schema allows 128 AltumAge samples or 512 clinical samples per
request. The platform's existing request-byte limit still applies independently:
full-precision synthetic AltumAge JSON is about 0.67 MB for one sample and
3.47 MB for eight. Split large matrices into multiple queued requests; the local
64-sample benchmark is not proof a 25.9 MB payload fits the default 16 MiB gateway.
No platform limits are raised by this package.

## Build and run

Run from the solutions-library repository root. The CPU and CUDA AltumAge images
share source and artifacts; GPU support is not a license to label an unmeasured
GPU as qualified.

```bash
docker build -f k8s-inference/models/aging/Dockerfile.phenoage -t fs2-phenoage .
docker build -f k8s-inference/models/aging/Dockerfile.altumage -t fs2-altumage-cpu .
docker build -f k8s-inference/models/aging/Dockerfile.altumage \
  --build-arg TORCH_INDEX_URL=https://download.pytorch.org/whl/cu128 \
  -t fs2-altumage-cuda .
```

For a reproducible regional publication, use the committed-source helper with
your existing registry credentials. It retains an OCI archive, provenance and
the immutable image digest; it does not deploy or claim hardware qualification.

```bash
python k8s-inference/models/aging/build_image.py phenoage \
  --ref <commit> --registry <regional-registry>/models \
  --builder <buildx-builder> --output-dir <fresh-receipt-directory>
python k8s-inference/models/aging/build_image.py altumage --device cuda \
  --ref <commit> --registry <regional-registry>/models \
  --builder <buildx-builder> --output-dir <another-fresh-receipt-directory>
```

`AGING_DEVICE=cpu` is the default. A CUDA deployment must set `AGING_DEVICE=cuda`
and allocate an actually supported GPU. Missing CUDA causes a clear startup
failure, not silent CPU fallback. `AGING_CPU_THREADS` defaults to one;
`AGING_ARTIFACT_ROOT` defaults to `/opt/altumage`. Clinical PhenoAge refuses a CUDA
device setting. Model artifacts are baked into the image, so its measured cold
start must include any cold image pull. GPU snapshotting is **not qualified** for
AltumAge and not applicable to clinical PhenoAge; no benefit is presumed for a
three-megabyte network.

Generate complete synthetic requests without copying real assay data:

```bash
PYTHONPATH=k8s-inference/models python -m aging.fixtures phenoage --samples 2
PYTHONPATH=k8s-inference/models python -m aging.fixtures altumage \
  --artifact-root /path/to/materialized/altumage --samples 2
```

## Verification and benchmark boundaries

Install the pinned HTTP/AltumAge requirements and CPU Torch 2.8.0 in a disposable
environment. Reference tests additionally use TensorFlow CPU 2.20.0, pytest and
httpx; TensorFlow is **not** a runtime dependency.

```bash
python k8s-inference/models/aging/altumage/materialize.py /path/to/artifacts --reference
ALTUMAGE_TEST_ARTIFACT_ROOT=/path/to/artifacts \
  python -m pytest k8s-inference/models/aging/tests -q
PYTHONPATH=k8s-inference/models python -m aging.benchmark_runtime altumage \
  --artifact-root /path/to/artifacts --device cpu
PYTHONPATH=k8s-inference/models python -m aging.benchmark_runtime phenoage
```

The final local run passed **25 tests**, including actual official PyTorch vs
original Keras inference on three synthetic matrices (absolute tolerance
0.001 years), column reordering, explicit imputation, two distinct real HTTP
responses per model, batch validation and exact clinical formula parity.
Two existing FastAPI/Starlette compatibility deprecation warnings were retained.

Local CPU receipts in `evidence/` measured imports plus model initialization:
AltumAge **2.125 s**, clinical PhenoAge **0.077 s**. Median JSON validation plus
one-sample prediction over 20 repetitions was **8.13 ms** and **0.0062 ms**
respectively. These are developer-host, already-present-artifact measurements,
not Kubernetes cold starts or user-visible latency. Image pulls, node scheduling,
networking and public admission are explicitly excluded. Large JSON matrices
also require measured host RAM; do not infer memory requirements from weight
size alone. The r01 `ru_maxrss` counter was found to inherit a pre-exec ancestor
peak on this runner, so it is not model memory-demand evidence; r02 records
process-local Linux `VmHWM`/`VmRSS` instead and retains the original counter's
limitation. Real CPU/H100 worker qualification and its separate image-pull timing
are in [the cluster acceptance report](../../acceptance/aging-20260908/README.md).
Direct worker tests do not establish public App scale-from-zero latency.

## Add the Apps to a cluster

Select `altumage` and `phenoage` in `deployment.models.enabled` in
`terraform.tfvars`, alongside existing model IDs. Native additions do not change
the historical `full_catalog` profile's default model set. The optional `aging`
profile selects just these two Apps. Use an existing qualified H100 pool for
AltumAge and enable the general CPU pool/queue for PhenoAge; the CPU formula must
not receive a GPU allocation. The selected immutable runtime images are declared
in `catalog/runtime/deployment-runtimes/{altumage-cuda,phenoage-cpu}.json`.
Regional mirroring can change the repository, but not the image digest.

With dynamic models enabled and `workload_owner = "controller"`, include the two
IDs in `deployment.dynamic_models.bootstrap_model_ids`. An initial hot worker is
selected with `deployment.models.scaling.hot`; explicit scaling overrides can
instead start at zero. After bootstrap, use **Apps → the App → Settings** for
minimum/maximum ready workers and idle/cooldown periods. Terraform owns the
infrastructure envelope and bootstrap, not subsequent App replica edits.

Both use the existing native API, `POST /v1/models/{model_id}:invoke`, and MCP
tools `infer_altumage` / `infer_phenoage`; these are not chat-completion models.
The platform wraps the worker payload in its normal invocation contract and
retains the operation ID, status and result. Generate the model payload using
`aging.fixtures` above. Public App names/IDs can differ for user-created copies.

An explicit zero floor is allowed before a measured elasticity receipt exists;
the admin page shows that it is not yet benchmark-qualified. This permits the
real zero-to-one-to-zero test without pretending it already passed. Snapshot
and fast-start qualification remain separate. PhenoAge has no GPU state;
AltumAge currently uses normal image-baked weight loading.

See [the integration boundary](INTEGRATION.md) and
[CPU-managed App implementation evidence](../../acceptance/aging-20260908/CPU-MANAGED-APPS.md).
The acceptance directory distinguishes direct worker qualification from public
HTTP/MCP and dynamic scaling results; only a retained successful live receipt
establishes those latter claims.
