# Model tools over MCP

Connect your MCP client to the Terraform `mcp` endpoint using a normal inference
API key. The key's allowed models determine access to both serving and scientific
Apps. Users share model workers, not each other's operations or artifacts.
No separate scientific or academic customer credential is needed. Model licenses
and scientific usage constraints still apply; a model grant is not a license.

## Find the right tool and inputs

Use `tools/list` to discover authorized tools and their descriptions. Every named
model tool publishes concrete fields and constraints in its `inputSchema`.
Call `get_model_schema` for examples, source references and runtime identity:

```json
{"model_id":"openfold2","protocol":"native"}
```

The result contains `model_id`, a `contracts` array, and (for serving models)
`active_runtime`. Each contract has `tool_name`, `protocol`, `input_schema`,
`examples`, `source_refs` and canonical `model_ref`. Use the returned tool name;
independent Apps of one model have separate public identities and settings.
Omitting `protocol` returns the model's available contracts.
Legacy routes may have null runtime metadata; this is unknown information, not
a claim that their worker is currently Ready.

Examples describe valid input shape, not scientific recommendations. Large
fields advertise `x-fs2-artifact-materialization` and accept the immutable
reference returned by the model-artifact upload tools. Pinned `fixture_id`
examples are server-side assets and contain no bytes in discovery. Other sample
artifact IDs are not already uploaded on your behalf.
JSON-schema validity does not guarantee valid biology, imaging geometry, or a
valid external asset; the model performs those additional checks.

| Protocol family | Inputs and model behavior |
|---|---|
| OpenAI chat | Structured `messages`, supported generation/tool options; the named App selects the model. Multimodal Apps require the image/content format in their actual schema. |
| Native prediction/generation | Actual runtime fields: for example Boltz2 polymers/A3M, OpenFold2 sequence settings, Cosmos media-generation mode, or PhenoAge biomarker samples and units. These are not interchangeable JSON formats. |
| Scientific batch | Versioned run request, declared operation, model-specific parameters, queue service class, and a previously finalized input-manifest artifact. Submission creates one logical run with stage/shard progress. |

Named tools take model fields **directly**, without a `payload`/`request` wrapper.
For example, call the `tool_name` returned for OpenFold2 with these existing
synthetic contract-check inputs:

```json
{
  "input_id":"example-contract-only",
  "sequence":"ACDEFGHIKLMNPQRSTVWY",
  "selected_models":[1],
  "relax_prediction":false,
  "idempotency_key":"example-openfold2-request-0001",
  "wait_seconds":0
}
```

`idempotency_key` is a submission control, not a model parameter. Reuse it only
for an identical intended submission. Serving tools also accept bounded
`wait_seconds`; zero returns after durable acceptance without waiting for model
execution. An accepted operation is **not** the final prediction.

For compatibility, named serving tools still accept the old `payload` wrapper,
and named scientific tools accept the old `request` wrapper. Controls remain
outside these wrappers. Wrapped input is checked against the same concrete
contract; unsupported fields do not gain a bypass. The generic `invoke_model`
and `submit_scientific_run` envelopes remain available for existing clients.

## Core tools and workflow

| Tool | Purpose and what to do next |
|---|---|
| `list_models` | Discover authorized serving Apps, protocols and active runtime metadata. Choose a model, then read its schema. |
| `list_scientific_models` | Discover authorized scientific profiles, operations and submission capabilities. Use their named tool and upload workflow. |
| `get_model_schema` | Read one authorized model's concrete input fields, examples and source references. This does not start a worker or a run. |
| `invoke_model` | Generic serving submission with explicit model/protocol/payload; retain the returned operation ID. Prefer the named typed tool for new integrations. |
| `get_operation` | Read your operation's queue/running/terminal state and result availability. Poll after asynchronous acceptance; it does not rerun inference. |
| `get_operation_result` | Retrieve your published serving result after successful completion. Preserve actual structured/binary result metadata rather than treating status as output. |
| `cancel_operation` | Request cancellation of your serving operation; observe its subsequent terminal state and resource release. |
| `acknowledge_operation` | Acknowledge an operation and release its retained result/payload according to platform policy. Download what you need first. |
| `begin_model_artifact_upload` | Reserve tenant-owned bytes for any authorized serving or batch App; use the returned handle without passing bytes through the LLM. |
| `put_model_artifact_bytes` | Inline-transfer only a small reserved file from a trusted helper, never from model-generated base64. |
| `finalize_model_artifact_upload` | Verify and return the immutable artifact reference accepted by transport-enabled typed fields. |
| `get_model_artifact` | Inspect metadata for a serving input/output artifact without returning file bytes. |
| `download_model_artifact` | Obtain a short-lived handle for a serving artifact; download and verify it outside model context. |
| `read_model_artifact_bytes` | Base64-read a small artifact for non-LLM client code; do not copy its result into another model call. |
| `begin_scientific_artifact_upload` | Declare an input artifact's exact metadata and obtain an upload identity; does not submit science. |
| `put_scientific_artifact_bytes` | Transfer the artifact bytes using the declared upload contract and encoding. Preserve exact size and hash. |
| `finalize_scientific_artifact_upload` | Complete and validate the upload; retain the finalized artifact metadata for the manifest/run request. |
| `submit_scientific_run` | Generic scientific submission for an explicit model/run request. Prefer its named typed model tool, then retain the operation ID. |
| `get_scientific_status` | Follow the existing run's stages, shards, queueing and execution state; no duplicate submission. |
| `list_scientific_events` | Read ordered run events using the last sequence as a cursor for incremental progress. |
| `get_scientific_result` | Read published result metadata after execution and publication finish; use returned artifact identifiers. |
| `get_scientific_artifact` | Inspect one caller-owned artifact's metadata before downloading; metadata is not the file bytes. |
| `download_scientific_artifact` | Obtain the existing artifact download response/location described by the tool. Follow its expiry and access requirements. |
| `read_scientific_artifact_bytes` | Retrieve caller-owned artifact bytes in the documented MCP encoding where supported; respect the tool's size limit. |
| `cancel_scientific_run` | Request cancellation of your scientific run; continue checking terminal/cleanup status. |

Scientific sequence: upload constituent input files → build/upload/finalize the
manifest that references those files → submit the typed run → follow status and
events → wait for result publication → inspect/download artifacts. Never replace
artifact IDs with arbitrary local paths or reuse another customer's uploads.
[Scientific batch quick start](SCIENTIFIC_BATCH_API.md) contains the exact upload
and run envelopes. Mixed or long-running batches may queue on limited capacity;
retain operation IDs rather than repeatedly submitting requests.

## Errors and troubleshooting

Invalid typed model fields return MCP error `-32602` before durable admission.
Its `data.type` is `model_input_validation`, with `model_id` and `issues`.
Each issue has a JSON-pointer `field` and a validation `rule`, plus applicable
`missing_fields`, `allowed_fields` or `expected` constraints. Correct that field
using the published schema; do not blindly retry the same invalid input.
Unauthorized/unknown tools or models return a generic policy error rather than
another customer's schema or operation data.

Runtime failures are different: an accepted operation may fail while executing.
Operators can inspect its actual request and upstream error in
[request debug logging](request-debug-logging.md) when enabled. Full model inputs
belong in that access-controlled view, not copied API keys or ordinary logs.

## NVIDIA BioNeMo toolkit compatibility

NVIDIA skills can help an agent select a workflow, but REST examples and Python
scripts may call NVIDIA endpoints directly, use different authentication, and
expect immediate model JSON. They are **not transparently redirected** by adding
our MCP server. A client adapter must use this platform's key, selected App/tool,
durable operation ID and result retrieval, while preserving scientific inputs.

Our current portable Boltz2 is protein-only with explicit A3M; it does not expose
the full NIM ligand/affinity/template/full-PAE interface. Portable OpenFold2 is
single-checkpoint, no relaxation, no external MSA/template input. Do not silently
discard those inputs or claim equivalent results. The
[pinned compatibility mapping](../acceptance/mcp-model-contracts-20260909/BIONEMO-COMPATIBILITY.md)
records exact supported differences and offline tests. Other models have their
own selected-runtime contracts; NVIDIA branding is not a universal API schema.
