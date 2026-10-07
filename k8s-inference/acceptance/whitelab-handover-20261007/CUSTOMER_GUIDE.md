# WhiteLab Scientific AI workspace

Provisioned 7 October 2026. Release qualification and remaining limitations are
recorded in [README.md](README.md); this guide is not itself a readiness claim.

## Access

| Interface | Address |
| --- | --- |
| LibreChat | https://port3080-erfrwhsyah6v68v.tunnel.applications.eu-north1.nebius.cloud |
| REST API base | `https://89.169.99.188/v1` |
| Streamable HTTP MCP | `https://89.169.99.188/mcp` |
| S3 endpoint | `https://storage.eu-north1.nebius.cloud` |
| S3 region | `eu-north1` |
| Shared bucket | `fs2-whitelab-951ca2389f78c7ca` |

The API key and S3 access/secret key pair are handed over privately. Do not put
them into chat messages, source code or command-line arguments. The initial
LibreChat login needs the customer's verified email; registration is closed.
The workspace already has its platform, Token Factory and Tavily integrations
configured. The same platform key works for REST and MCP (Bearer authentication).

The tenant has a **1,000 GB decimal** bucket and **eight simultaneous operations
per key**. This is an admission limit, not eight reserved GPUs: requests can queue
while compatible capacity is occupied. This PoC key has no scheduled expiration.
Large-file upload intents also occupy operation slots while active. A ninth
request receives an explicit concurrency response; the provided submission
client can wait for a slot without duplicating an accepted operation.

## Files and getting started

The bucket is mounted as `/workspace` in LibreChat. Starter files and their
manifest are under `/workspace/examples/v3/`. Store inputs and results in a
study-specific directory such as `/workspace/studies/my-first-integration/`.
Chat history, account state and application settings use a separate persistent
filesystem, so an operator image replacement need not reset the account.

For large datasets, upload directly to the bucket with an S3 client, not the chat
attachment picker. The workspace browser's upload control currently accepts up
to 512 MiB per file; this is separate from the batch API's 25 GiB artifact limit
and the bucket's total quota. With the privately supplied S3 credentials set in
the standard `AWS_ACCESS_KEY_ID` and `AWS_SECRET_ACCESS_KEY` environment variables:

```bash
aws --endpoint-url https://storage.eu-north1.nebius.cloud \
  --region eu-north1 s3 cp counts.h5ad \
  s3://fs2-whitelab-951ca2389f78c7ca/studies/my-study/counts.h5ad
```

That file appears at `/workspace/studies/my-study/counts.h5ad`. The S3 credential
pair is different from the platform Bearer key. The provider keys already bound
to LibreChat do not need to be pasted into a chat.

When preparing a new H5AD in the workbench, write it on local scratch storage,
close and validate it, then copy the completed file into `/workspace` and verify
the copy. An S3 mount is not a fully POSIX filesystem for HDF5's random writes.
Preserve the original uploaded input and record any explicit cell/gene selection.

For a single-cell study, upload an H5AD containing raw, non-negative integer
counts. Identify their location (`X`, `raw.X`, or a named layer), the batch column,
and, for scANVI, the annotation column and explicit unlabeled category. Missing
labels should not be silently interpreted as an unlabeled category.

Example prompt, **replace the file and column names with your actual metadata**:

> Use the single-cell-analysis skill to integrate `/workspace/studies/my-study/counts.h5ad`
> with scVI. Raw counts are in `layers/counts`, and the batch column is `sample_id`.
> Inspect the file and explain the selected genes and training parameters before
> submitting. Keep all cells. Run through the durable scientific batch API, save
> the operation receipt and outputs under `/workspace/studies/my-study/run-001`,
> and tell me how to follow it and retrieve validated results.

For annotation, request scANVI and specify the labels column and unlabeled value.
For reference mapping, supply a reference archive from a trusted completed
scVI/scANVI run. These are distinct workflows; the agent must not silently switch
between them or silently remove cells to make a dataset fit.

Long jobs run remotely after a chat turn finishes. Ask the agent to collect the
**existing operation**, verify the downloaded results, and identify their paths.
An accepted job or a local pending receipt does not mean results are already
downloaded. Closing the browser does not cancel the operation.

## Direct API and MCP

The [single-cell API guide](../../models/visual-science/scvi-scanvi/README.md)
contains runnable upload, submission, collection and reference-mapping examples.
Use `submit.py` and `collect.py` from that directory with Python and `httpx`.
Set the privately supplied key in `SCIENTIFIC_MODELS_API_KEY` using your local
secret-management method, then:

```bash
python submit.py --origin https://89.169.99.188 \
  --input counts.h5ad --parameters parameters.json \
  --output run-001 --idempotency-key my-integration-001
python collect.py --origin https://89.169.99.188 \
  --operation-id OPERATION_ID --output run-001
```

Preserve the operation ID, output directory and idempotency key after a client
disconnect. Do not create another job just to check progress. Collection verifies
each output's byte count and SHA-256. A local collection timeout does not cancel
the remote job. Persistent errors are reported, not silently treated as success.

MCP clients should discover `get_model_schema` with `model_id: scvi-scanvi` and
`protocol: scientific-batch-v1` (or `tool_name: submit_scvi_scanvi`). Upload the
H5AD first; pass its artifact manifest to `submit_scvi_scanvi`, not base64 or raw
file contents. Use `get_scientific_status` and `get_scientific_result` with the
returned ID. The batch interface is separate from the older small-file native
demo endpoint.

## Qualified scope and outputs

The batch interface accepts artifacts up to 25 GiB, subject to expanded-memory
preflight. There is no fixed cell-count limit. Qualified profiles currently use
one H100 with 128 GiB host RAM (`routine`) or 256 GiB (`atlas`). Larger compressed
files can expand beyond memory; neither a file-size limit nor a successful public
dataset test is a promise that every atlas fits.

Outputs include embeddings, learning curves and a reusable model reference;
scANVI additionally supplies predicted labels and probabilities. The integrated
H5AD retains selected raw-count genes and adds embeddings/annotations: it is
**not** a batch-corrected expression matrix. Full-Lightning training checkpoints
support worker interruption recovery; GPU-process snapshots and multi-GPU
training are not claimed for this App.

Also granted: Boltz2, DiffDock, ESMFold2/Fast, OpenFold2/3, OpenFold3 OpenBind,
GROMACS, GROMACS MPI, NAMD, LAMMPS, BoltzGen, RFdiffusion, ProteinMPNN and MSA
Search PDB70. Each App has its own input contract and runtime requirements.
BindCraft is deferred by agreement pending the relevant licence; the standard
Evo2 choice remains open. Hosting a customer fine-tune is outside this PoC.

This is a research PoC. Successful execution and artifact validation do not
establish annotation accuracy, biological conservation or clinical suitability.
Assess held-out labels, batch mixing and biological conservation for each study.
When reporting a problem, include the operation ID, intended workflow and observed
error; never include a secret key.
