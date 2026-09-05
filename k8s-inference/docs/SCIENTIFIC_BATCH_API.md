# Scientific batch API quick start

This page is for a researcher, hackathon team, or proof-of-concept customer who
holds a scientific access token and wants to run one of the ten qualified
cancer-immunotherapy models through the public endpoint. It describes the
customer-visible contract only. Queue placement, images, commands, GPU
scheduling, and licences are operator-owned and never appear in a request.

The same flow is available over plain HTTPS and over MCP; both call the same
service, enforce the same token policy, and return the same documents.

## What you need

| Item | Where it comes from |
| --- | --- |
| Base URL | `inference_base_url` from `inference-stack output` (`https://<public-ip>/v1`), or the endpoint your operator handed you. |
| Bearer token | `credentials.scientific_access_token` for the academic tenant, or a PAT issued in the admin console under **Access / API keys**. |
| Scopes | `catalog.read` to discover, `inference.invoke` to upload and submit, `operations.read` to poll, `operations.result` to fetch results and artifacts, `operations.cancel` to cancel. |

Send the token as `Authorization: Bearer <token>`. Every mutating call needs
an `Idempotency-Key` header of 8 to 200 characters that you choose; repeating
the same call with the same key returns the same operation instead of a
second run. Keep the token out of shell history, tickets, and logs.

The general serving token also reaches the scientific catalog, but the two
licensed academic profiles (AlphaFold 3 and BindCraft) are visible only to the
academic tenant's token. Tenant boundaries are enforced on every route; a
resource from another tenant returns `404`, never `403`.

## 1. Discover the profiles your token can submit

```bash
curl -sS "$BASE/scientific-models" -H "Authorization: Bearer $TOKEN" | jq '.data[] | {model_id, operations, service_classes, parameter_schema}'
```

`GET /v1/scientific-models` lists only profiles with a complete, tenant-specific
admission path for this exact caller: it runs the same static gates as
submission, so a listed profile is submittable and an unlisted one is not.
Each row carries `model_id`, `display_name`, `operations`, `service_classes`,
`parameter_schema`, the pinned upstream `source_repository` and
`source_revision`, `runtime_image_digest`, the execution identity, the
qualification receipt digests, and the MCP tool name. The MCP tool
`list_scientific_models` returns the identical rows. `GET /v1/models` remains
the OpenAI-compatible catalog of serving models and does not list batch profiles.

The `parameter_schema` value names the JSON schema for the `parameters`
object of that model. The repository keeps a checked-in public example request
for every profile; they are the exact requests used for public acceptance:

| Model | Operation | Example request |
| --- | --- | --- |
| `alphafold3` | `predict-complex-structure` | `models/structure/batch-adapters/alphafold3/activation/public-request.json` |
| `bindcraft` | `design-binder` | `models/cancer-immunotherapy/images/bindcraft-native/activation/public-request.json` |
| `boltzgen` | `design-binders` | `models/cancer-immunotherapy/runtime-images/boltzgen/activation/public-request.json` |
| `esmfold2` | `predict-structure` | `models/structure/batch-adapters/esmfold2/activation/public-request.json` |
| `esmfold2-fast` | `predict-protein-structure` | `models/structure/batch-adapters/esmfold2-fast/activation/public-request.json` |
| `mosaic` | `design-binder` | `models/cancer-immunotherapy/runtime-images/mosaic/activation/public-request.json` |
| `openfold3-openbind` | `predict-complex-structure` | `models/structure/batch-adapters/openfold3/activation/public-request.json` |
| `proteina-complexa` | `design-binders` | `models/cancer-immunotherapy/runtime-images/proteina-complexa/activation/public-request.json` |
| `protenix-v2` | `predict-complex-structure` | `models/structure/batch-adapters/protenix-v2/activation/public-request.json` |
| `rfdiffusion` | `design-backbone` | `models/cancer-immunotherapy/runtime-images/rfdiffusion/activation/public-request.json` |

Read the `operation` field of each example rather than assuming a name; the
discovery row is authoritative for the operations a deployment accepts.

## 2. Upload your inputs

Inputs are immutable, content-addressed artifacts. You reserve an upload with
the exact SHA-256, size, and media type, write the bytes, and finalize. The
platform matches the bytes against the reservation before anything is stored;
a body that disagrees is rejected and stores nothing.

```bash
SHA=$(sha256sum target.fasta | cut -d' ' -f1)
SIZE=$(stat -c %s target.fasta)

RESERVED=$(curl -sS -X POST "$BASE/scientific-artifacts/uploads" \
  -H "Authorization: Bearer $TOKEN" -H "Idempotency-Key: upload-target-0001" \
  -H "Content-Type: application/json" \
  -d "{\"model_id\":\"esmfold2\",\"sha256\":\"$SHA\",\"size_bytes\":$SIZE,\"media_type\":\"text/x-fasta\"}")
OPERATION=$(echo "$RESERVED" | jq -r .operation_id)
UPLOAD=$(echo "$RESERVED" | jq -r .upload_id)

curl -sS -X PUT "$BASE/scientific-artifacts/uploads/$UPLOAD/content?operation_id=$OPERATION" \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: text/x-fasta" \
  --data-binary @target.fasta

curl -sS -X POST "$BASE/scientific-artifacts/uploads/$UPLOAD:finalize" \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d "{\"operation_id\":\"$OPERATION\"}"
```

The reservation response also contains a direct object-store `handle`; the
gateway `content` path above writes the same bytes for a client that can reach
only the public API, and the inline path is bounded by
`max_content_bytes` from the same response. Finalization returns the immutable
artifact pointer:

```json
{"artifact_id": "…", "sha256": "…", "size_bytes": 1234, "media_type": "text/x-fasta", "compression": "none"}
```

Repeat this for every input file, then upload one canonical manifest
(`application/vnd.fs2.scientific-manifest+json`, schema
`fs2-serve.nebius.ai/scientific-artifact-manifest/v1`) whose entries point at
those artifacts. The checked-in `input-manifest.json` beside each example
request shows the entry names a model expects. Manifest bytes must be
canonical JSON (sorted keys, no insignificant whitespace, one trailing
newline) because the request binds their SHA-256.
`acceptance/scientific-fleet/run_acceptance.py` performs exactly this
materialization for the committed examples and is the reference
implementation for a client.

## 3. Submit a run

```bash
curl -sS -i -X POST "$BASE/models/esmfold2:submit" \
  -H "Authorization: Bearer $TOKEN" -H "Idempotency-Key: esmfold2-run-0001" \
  -H "Content-Type: application/json" \
  -d @request.json
```

`request.json` follows `fs2-serve.nebius.ai/scientific-run-request/v1`:

```json
{
  "schema": "fs2-serve.nebius.ai/scientific-run-request/v1",
  "operation": "predict-structure",
  "service_class": "customer-batch",
  "input_manifest": {
    "artifact_id": "<manifest artifact_id>",
    "sha256": "<manifest sha256>",
    "size_bytes": 414,
    "media_type": "application/vnd.fs2.scientific-manifest+json",
    "compression": "none"
  },
  "parameters": {"sequence": "MKTAYIAKQRQISFVKSHFSRQDILDLWIYHTQGYFP", "mode": "single-sequence", "seed": 101},
  "client_context": {"batch_id": "my-batch", "correlation_id": "notebook-42", "display_name": "ESMFold2 test"}
}
```

`service_class` must be one the discovery row lists; `customer-batch` is the
class every profile offers. The response is `202 Accepted` with a `Location`
header of `/v1/operations/{operation_id}`, an `x-fs2-operation-id` header, and
`x-fs2-idempotent-replay: true` when an earlier identical submission was
reused. The body is the same status document as step 4.

Requests are validated before anything is queued. A request that violates the
schema, names an operation or service class the profile does not offer, or
points at a manifest whose digest does not match returns `422` with
`scientific_request_invalid`; a model your token cannot reach returns `503`
with `scientific_profile_unavailable`; a missing `Idempotency-Key` returns
`400`.

## 4. Follow the operation

```bash
curl -sS "$BASE/operations/$OPERATION_ID" -H "Authorization: Bearer $TOKEN" | jq '{status: .operation.status, batch: .batch.status, stages: [.batch.stages[] | {stage_id, status}]}'
curl -sS "$BASE/operations/$OPERATION_ID/events?after_sequence=0&limit=200" -H "Authorization: Bearer $TOKEN"
```

The operation moves through `queued`, `running`, and one terminal state:
`succeeded`, `failed`, `cancelled`, `preempted`, or `expired`. The `batch`
object shows every stage, each attempt's lifecycle phase, its Kueue admission
(`resolved_pool_id`, `admitted_resource_flavor`, `accelerator_count`,
`admitted_at`), and a stable failure code when something went wrong. Events
are an append-only page keyed by `sequence`; poll with the last sequence you
saw. Poll status every few seconds; the semantic result appears only when the
operation is terminal and `operation.result_available` is `true`.

Cancel with `POST /v1/operations/{id}:cancel` (or `DELETE /v1/operations/{id}`).
Cancellation is asynchronous and the operation reports `cancelled` once its
workloads are released.

## 5. Fetch the result and its artifacts

```bash
curl -sS "$BASE/operations/$OPERATION_ID/result" -H "Authorization: Bearer $TOKEN" > result.json
jq '{terminal_status, semantic_validation, output_manifest}' result.json
```

The result is the immutable `fs2-serve.nebius.ai/scientific-run-result/v1`
document: `terminal_status`, `submitted_at`/`completed_at`, the exact
`execution_identity` (source revision, runtime image digest, recipe and
execution identity digests), the frozen `scheduling_snapshot`, every attempt
with its phase timestamps and admission, the `semantic_validation` verdict with
its receipt digest, the `input_manifest` pointer, the `output_manifest` pointer
on success, and a structured `error` on failure. A run is reported `succeeded`
only when its output passed the pinned semantic validator.

Download the output manifest and then each artifact it names:

```bash
MANIFEST_ID=$(jq -r .output_manifest.artifact_id result.json)
curl -sS "$BASE/artifacts/$MANIFEST_ID/content" -H "Authorization: Bearer $TOKEN" > output-manifest.json
for ID in $(jq -r '.entries[].artifact.artifact_id' output-manifest.json); do
  curl -sS "$BASE/artifacts/$ID/content" -H "Authorization: Bearer $TOKEN" -o "$ID.bin"
done
```

`/content` streams the exact stored bytes with `x-fs2-artifact-sha256`; compute
your own digest and compare. `GET /v1/artifacts/{id}/download` returns a
short-lived presigned handle instead, for clients that can reach object
storage directly. Both require `operations.result` and stay inside your tenant.

## The same flow over MCP

Point an MCP client at `mcp_url` with the same bearer token. Tool discovery is
private and uncached, so the tool list reflects exactly your token's policy.

| Step | MCP tool | HTTP route |
| --- | --- | --- |
| Discover | `list_scientific_models` | `GET /v1/scientific-models` |
| Reserve upload | `begin_scientific_artifact_upload` | `POST /v1/scientific-artifacts/uploads` |
| Write bytes | `put_scientific_artifact_bytes` (base64) | `PUT /v1/scientific-artifacts/uploads/{id}/content` |
| Finalize | `finalize_scientific_artifact_upload` | `POST /v1/scientific-artifacts/uploads/{id}:finalize` |
| Submit | `submit_scientific_run` | `POST /v1/models/{model_id}:submit` |
| Status | `get_scientific_status` | `GET /v1/operations/{id}` |
| Events | `list_scientific_events` | `GET /v1/operations/{id}/events` |
| Cancel | `cancel_scientific_run` | `POST /v1/operations/{id}:cancel` |
| Result | `get_scientific_result` | `GET /v1/operations/{id}/result` |
| Artifact pointer | `get_scientific_artifact` | `GET /v1/artifacts/{id}` |
| Artifact bytes | `read_scientific_artifact_bytes` (base64) | `GET /v1/artifacts/{id}/content` |
| Download handle | `download_scientific_artifact` | `GET /v1/artifacts/{id}/download` |

The byte tools carry base64 and are bounded by the same inline ceiling as the
HTTP routes. MCP submission additionally requires the profile to be
MCP-invocable; every currently qualified profile is.

## What to expect on the retained H100 deployment

The ten profiles run as Kubernetes Jobs on the two capacity-block
`h100-reserved-8x` nodes and borrow the preemptible `h100-1x` pool when the
reserved nodes are busy; CPU-only stages run on an elastic `batch-cpu` pool
that scales from zero. Each run pays for its own image and artifact
localization today, so first results take from about 100 seconds (AlphaFold 3,
ESMFold2) to about 15 minutes (BoltzGen's twenty-design campaign). The
measured per-model figures from the accepted 30-attempt campaign are in
[Live acceptance](../LIVE_ACCEPTANCE.md#scientific-fleet-acceptance-2026-09-05).
Operators see the same runs, their GPU lifecycle accounting, and the Kueue
queue under the admin console's scientific pages.
