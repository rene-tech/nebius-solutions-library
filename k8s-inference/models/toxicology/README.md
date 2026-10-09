# Molecular ADMET and cardiac ion-channel screening

Two shared, scale-to-zero CPU Apps. Customers receive model grants through their
existing API keys; no per-customer model deployment or GPU allocation is needed.
These are pretrained research predictors, **not proof that a molecule is safe
in humans**. Keep endpoint definitions, species, units and limitations with results.

| App | Pinned upstream | Outputs |
| --- | --- | --- |
| `admet-ai` | ADMET-AI **2.0.1**, commit `c65bf0418e19c65d7228f9e40da5d0152aade756` | 41 learned ADMET/assay endpoints plus 11 deterministic descriptors/alerts |
| `ctoxpred2` | CToxPred2 commit `2a31aa119e27b6b69a5588d18a01f2a27fef4524` | hERG, Nav1.5 and Cav1.2 blocker scores and classes |

Both upstream repositories distribute the selected code/weights under MIT. Their
original notices remain in the images. This does not establish rights in uploaded
customer data or clinical/regulatory approval. Neither App is an NVIDIA NIM and
neither carries an NVIDIA publisher badge.

## Call the Apps

`FS2_API_KEY` is your existing key with the desired model grant. Store credentials
outside source control. The hosted origin is currently `https://89.169.99.188`.

```bash
curl --fail-with-body https://89.169.99.188/v1/models/admet-ai:invoke \
  -H "Authorization: Bearer $FS2_API_KEY" \
  -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: admet-library-run-0001' \
  -H 'x-fs2-wait-seconds: 0' \
  -d '{"operation":"screen-molecules","payload":{"molecules":[
    {"id":"aspirin","smiles":"CC(=O)Oc1ccccc1C(=O)O"},
    {"id":"caffeine","smiles":"Cn1c(=O)c2c(ncn2C)n(C)c1=O"}
  ]}}'
```

The response includes an operation `id`. Poll `GET /v1/operations/{id}`; once it
is `succeeded`, fetch `GET /v1/operations/{id}/result`. Reuse the same idempotency
key for retries of the **same** request. A different request needs a different key.
Large results return an immutable artifact pointer; download
`GET /v1/artifacts/{artifact_id}/content` with your API key and verify its size/hash.

Typed MCP tools at `/mcp`:

- `infer_admet_ai_native`
- `infer_ctoxpred2_native`

Pass molecular fields directly, with `idempotency_key` and optional
`wait_seconds: 0`. Use `get_operation` and `get_operation_result` afterward.
Discover exact input schemas with `get_model_schema`. Do not paste CSV/SDF bytes
through an LLM: use `begin_model_artifact_upload`, upload the bytes out of band,
then `finalize_model_artifact_upload` and supply the returned artifact reference.

### Inputs and outputs

Supply exactly one of `smiles`, `molecules`, `csv`, or `sdf`:

- `smiles`: one molecule, including intended stereochemistry.
- `molecules`: ordered `{id, smiles}` records, unique IDs, at most **1,000**.
- `csv`: CSV text or tenant-owned immutable artifact. `smiles_column` defaults to
  `smiles`; optional `id_column` preserves customer identifiers.
- `sdf`: SDF text or artifact; names become IDs and structures become isomeric
  SMILES. Coordinates do not enter these 2D predictors.

File transport is bounded at 16 MiB, with the same 1,000-molecule execution limit.
Split larger libraries into bounded operations; existing key concurrency and
durable queuing apply. `molecules` can also be a JSON artifact reference. No
customer storage credentials are installed in the model runtime.

`endpoints` optionally selects named outputs. Unknown names are errors. Full
results include `endpoint_metadata`, so callers retain labels and units.
Invalid structures keep their row IDs and errors in a partial result. A wholly
invalid batch fails explicitly. Salts/fragments are flagged, not silently
stripped; no extra neutralization or tautomer standardization is imposed.

CToxPred2 additionally accepts `method: "rf-ssl"` (default, deterministic
semi-supervised random forests) or `"dl-sl"` (supervised networks with 100
MC-dropout draws) and a recorded `seed`. The seed is reproducible for the same
ordered request, endpoint selection and chunk layout; splitting a request can
change its Monte Carlo draws. RF scores do not provide calibrated uncertainty.
MC-dropout standard deviation is **not** calibrated human-safety confidence.
Class 1 is an ion-channel blocker, defined upstream as **IC50 ≤ 10 μM**
(pIC50 ≥ 5), not a diagnosis of cardiotoxicity or arrhythmia.

## Execution and cold starts

The existing ModelDeployment controller/KEDA own replica policy and admission.
Initial deployment uses the existing `batch-cpu` pool, 2 CPUs / 2 GiB per replica,
min 0 / max 2, 300-second idle/cooldown. These are operator-adjustable App settings;
GPU pools and customer model settings are not changed. The Terraform model
profiles and renderer manifests include these Apps for reproducible deployment.

Weights and dependencies are baked into digest-pinned images in the cluster's
regional registry. `IfNotPresent` reuses a node's image cache. GPU snapshots are
**not applicable** to these CPU workers. Local model initialization is roughly
3–4 seconds; this is **not** end-to-end cold start, which also includes scheduling,
an uncached image pull, container startup and gateway readiness detection.

Each runtime admits up to eight HTTP calls, serializes access to shared model
state and yields between 32-molecule chunks. Concurrent customers can progress
without unbounded PyTorch/sklearn process/thread fan-out. Whole-request admission,
retries, results, authentication, attribution and logs remain in the platform.

## Reproduce and qualify

Build with the supplied Dockerfiles and pinned Python dependency constraints.
Run `qualify_runtime.py --model <id>` in the exact image with this directory
mounted at `/qualification`. It inventories weights, checks two distinct
semantic fixtures, matches independently invoked upstream predictions, tests
the neural method's seeded replay and measures 1/32/1,000-molecule throughput.

`evaluate_public.py` runs all three pinned CToxPred2 published evaluation sets:
250 hERG, 62 Cav1.2 and 64 Nav1.5 records. Receipts preserve inputs, labels, hashes
and predictions. These are **upstream evaluation sets**, not newly independent
clinical validation. ADMET-AI's outputs are not scored against CTox labels because
its endpoint datasets/definitions differ.

In our local RF run the descriptive ROC-AUCs were about **0.790 / 0.782 / 0.626**
for hERG / Cav1.2 / Nav1.5 respectively. The Nav1.5 result is weak and must not be
hidden behind a broad "cardiotoxicity accuracy" claim. The sample sizes are small,
particularly for the latter two channels. Do not reuse older ADMET-AI v1 paper
performance figures as measured v2 performance.

The released CTox sklearn transforms intentionally skip features with no observed
training values. Their names are retained in runtime metadata; warnings are not
suppressed or "fixed" by fitting a new transform on customer inputs. The original
notebook's `CorrelationThreshold` class is resolved by importing that same pinned
upstream class, not replacing its behavior.

`acceptance/toxicology-20261008/qualify_hosted.py` is the independent public REST/
typed-MCP gate. Use only `system/qa`, two concurrent operations, and retain failed
attempts. Direct-container tests alone are not a hosted release qualification.
See that acceptance directory for exact rollout state and measured cluster timings.

## KERMT follow-up (research only)

The verified official public checkpoint is
[NVIDIA NV-KERMT-70M-v2](https://huggingface.co/nvidia/NV-KERMT-70M-v2), revision
`7df5eb3179235fdea1e8124db73215da33d77dce`. It is a molecular **encoder backbone**,
not a set of ready-to-serve toxicology heads. The Hugging Face/NGC entries referenced
by the v2 release are distribution locations for the same backbone, not multiple
independently pretrained endpoint models. The checked Figshare release contains
cluster-split datasets, not downstream checkpoint weights.

No public ready-to-serve endpoint heads were verified in this check. Do not expose
the backbone as a hERG/ADMET predictor. A separate embeddings App is possible;
toxicity predictions require verified task-trained heads (from NVIDIA or trained
and independently evaluated here). That follow-up is not part of this release.

Sources: [ADMET-AI](https://github.com/swansonk14/admet_ai),
[CToxPred2](https://github.com/issararab/CToxPred2),
[CToxPred2 paper](https://doi.org/10.1021/acs.jcim.4c01102),
[KERMT releases](https://github.com/NVIDIA-BioNeMo/KERMT/releases).
