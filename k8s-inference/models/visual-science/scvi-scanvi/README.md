# scVI / scANVI batch workflows

Train scVI for integration/latent embeddings, train scANVI for annotation, or
map a query dataset into a saved reference. This is research software, not
clinical interpretation. Runtime: pinned `scvi-tools 1.5.0.post1`.

Use the durable batch endpoint for real datasets:

- REST: `POST /v1/models/scvi-scanvi:submit`
- MCP: `submit_scvi_scanvi`, then `get_scientific_status` / `get_scientific_result`
- Discovery: `get_model_schema` with `model_id: scvi-scanvi` and
  `protocol: scientific-batch-v1` (or `tool_name: submit_scvi_scanvi`).

The older small-file native endpoint is separate; its 64 MiB/100k-cell demo
limits are **not** the limits of this batch endpoint. Do not put H5AD/base64
data in an MCP call. Upload bytes first, then submit the artifact manifest.

For exact deployment/qualification evidence, see [BATCH_READINESS.md](BATCH_READINESS.md).
Availability of a route alone is not a qualification claim.

## Inputs and settings

Supply `.h5ad` with unique cell/gene identifiers and **unnormalized integer
counts** in `X`, `raw.X`, or `layers/<name>`. `batch_key` and `labels_key` refer
to columns in `obs`. Use an explicit string such as `Unknown` for unlabeled
cells; no missing metadata values. Fully labeled scANVI input is supported.

The batch API does not impose a fixed cell-count or epoch cap. It accepts input
artifacts up to 25 GiB and checks expanded matrix memory before loading.
Compressed file size alone does not determine fit. Larger inputs require an
operator-configured envelope, not splitting cells blindly or raising cloud quotas.

| Profile | GPU | CPU | Host RAM | Intended use |
| --- | --- | --- | --- | --- |
| `routine` | 1 H100 | 8 cores | 128 GiB | Ordinary and large sparse single-cell data |
| `atlas` | 1 H100 on a large-memory host | 8 cores | 256 GiB | Larger expanded matrices; may wait for a full-node pool slot |

Both profiles allocate **one GPU**, not an eight-GPU node. GPU compatibility
and available pools are operator catalog settings; these are the qualified
hardware targets, not a universal cell-capacity promise.

Important configurable options:

- `method`: `scvi` or `scanvi`; `mode`: `train` or `map-query`.
- `max_epochs`: scVI budget; `null` uses upstream's cell-count heuristic.
  `scanvi_max_epochs` and `query_max_epochs` are independent.
- Early stopping, patience, batch size, seed, latent size, network size and
  likelihood (`nb`, `zinb`, `poisson`) are explicit parameters.
- `gene_selection`: batch-aware Seurat-v3 `hvg`, a supplied boolean
  `var.highly_variable` mask (`provided`), or `all`. Numerically singular
  batches may require a larger `hvg_span` (up to 1) or a supplied gene mask.
  The platform never silently switches HVG algorithms or batch definitions.
- `visualization`: sampled UMAP (default), full, or none. Sampling affects
  visualization only, **not** the trained cells or exported embeddings.
- `checkpoint_every_n_epochs`: default 5. Checkpoints include optimizer,
  scheduler, loops and RNG state. They are training checkpoints, **not GPU
  process snapshots**. A replacement worker still stages and loads input data.
- `max_wall_seconds`: default 86,400; configurable up to 14 days.
- `output_destination`: tenant `customer-bucket` by default, or
  `platform-artifacts`. `output_prefix` chooses a prefix inside your workspace.

## Submit from Python or the command line

From this directory, install the small client dependency (`pip install httpx`).
Set `SCIENTIFIC_MODELS_API_KEY` in your environment; do not put keys in command
arguments or commit them. Your key must allow `scvi-scanvi`.

Save parameters in a JSON file, for example:

```json
{
  "schema": "fs2-serve.nebius.ai/scvi-workflow-request/v1",
  "method": "scanvi",
  "counts_source": "layers/counts",
  "batch_key": "sample_id",
  "labels_key": "cell_type",
  "unlabeled_category": "Unknown",
  "resource_profile": "routine",
  "n_top_genes": 3000,
  "batch_size": 512,
  "max_epochs": null,
  "scanvi_max_epochs": 20,
  "output_destination": "customer-bucket"
}
```

Choose metadata names that actually exist in your input, then:

```bash
python submit.py --origin https://89.169.99.188 \
  --input counts.h5ad --parameters parameters.json \
  --output run-001 --idempotency-key my-integration-001
python collect.py --origin https://89.169.99.188 \
  --operation-id OPERATION_ID --output run-001
```

`submit.py` streams uploads, automatically uses multipart for large files,
verifies finalization, and prints the operation ID. Reuse the output directory
and idempotency key after a disconnected client. Use a new key/output directory
for an intentional independent run. `collect.py` verifies every output's bytes
and SHA-256 and restores meaningful filenames under `run-001/data/`. A local
collection timeout does not cancel the remote job; poll the same operation again.
If your key's concurrent-operation slots are occupied, submission waits up to
900 seconds, retrying only an explicit non-admission response with the same
idempotency key. Configure this with `--admission-wait-seconds`; a timeout
preserves completed uploads. Input-upload intents also use those slots while
active. Other errors are reported, not retried blindly.

For reference mapping, set `mode: map-query`, use the matching `method`, and add
`--reference previous-run/data/reference.tar.gz` to `submit.py`. The reference
fixes the gene order and model architecture; query genes are aligned by scVI's
scArches workflow. Record missing genes and inspect mapping quality before use.

For an agent/MCP call, use `submit.py --prepare-only` to stage data and write
`request.json` without training. Pass that JSON's fields plus an idempotency key
to `submit_scvi_scanvi`. Poll the returned ID; do not resubmit on each poll.
The generic `get_operation`/`get_operation_result` pair also supports batch jobs.

## Outputs, restart and interpretation

Outputs include `latent_embeddings.csv`, scANVI `predicted_labels.csv` and
`label_probabilities.csv`, `reference.tar.gz`, learning curves, preflight
metadata and optionally `integrated.h5ad` / UMAP. The integrated H5AD contains
**selected raw-count genes plus embeddings/labels**, not a batch-corrected
expression matrix. The full original input remains an immutable input artifact.

Tenant-bucket publication includes content-addressed objects and checkpoint
manifests under the requested prefix plus operation/job IDs. API artifacts are
also retrievable by the submitting tenant. Saved model references must come
from a trusted run; they are not arbitrary code-upload endpoints.

The scheduler retries recognized interruptions within the same operation, up
to the frozen attempt budget. No new billable request needs to be submitted
because a polling client disconnects. A terminal failed/cancelled operation is
not currently user-resumable through a separate scVI `:resume` endpoint; do not
use the GROMACS-specific resume tool for it.

Inspect held-out annotation quality, batch mixing and biological conservation
for your own study. A successful job, normalized probabilities, or an attractive
UMAP does **not** demonstrate biological correctness or convergence.

## PoC handover inputs

No customer dataset is required to exercise the provided public-data examples.
For a customer's exact study, collect a representative H5AD (or its shape and
storage size), the raw-count location, batch/donor column, annotation column,
and whether the task is integration, annotation or reference mapping. The
operator grants `scvi-scanvi` to their existing inference identity and workspace;
these scripts do not create a tenant or expose an operator credential.

This release covers the Python REST client and typed MCP protocol. It does not
qualify a particular LibreChat/LLM workflow, biological accuracy, multi-GPU
training, or an arbitrary atlas size. There is no GPU-process-snapshot claim.

## References

- [scvi-tools documentation](https://docs.scvi-tools.org/en/stable/)
- [scANVI integration tutorial](https://docs.scvi-tools.org/en/stable/tutorials/notebooks/scrna/harmonization.html)
- [Scanpy HVG parameters](https://scanpy.readthedocs.io/en/stable/generated/scanpy.pp.highly_variable_genes.html)
- [HLCA publication](https://www.nature.com/articles/s41591-023-02327-2)
