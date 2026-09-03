# Scientific artifact provenance and result service

This service is what every scientific model adapter writes through. It records
what each stage attempt produced, proves the bytes match what was declared, and
publishes two immutable documents: one manifest per stage, and one canonical
result per operation.

The public JSON Schemas in `catalog/runtime/schema` own the shape of everything
that leaves this component. The control plane projects onto them and never
defines a competing schema of its own.

## What is stored, and what is not

Only content addresses and identities are persisted. No table holds object
bytes, a presigned URL, a signed header, or a credential, and no code path logs
one. `tests/test_scientific_artifacts.py` asserts this against the migration's
own DDL and against a debug-level log capture.

## Scope

An artifact belongs to exactly one stage attempt:

```
(operation_id, stage_id, shard_id, attempt_id)
```

`shard_id` is stored as the sentinel `-` for a gang-scheduled stage with no
shard identity, so every foreign key stays composite and `NOT NULL`. The object
key is derived from that scope and is the only key the service accepts:

```
scientific/v1/tenants/{tenant}/operations/{operation}/stages/{stage}
  /shards/{shard}/attempts/{attempt}/{input|output}/sha256/{digest}
```

A row whose `storage_key` disagrees with that derivation is rejected by a CHECK
constraint, not only by the application.

Each attempt also records the Kueue admission it actually received: resolved
pool, admitted resource flavor, accelerator resource name and count, plus the
Kueue workload, Job, Pod, node and GPU identities observed while it ran. Those
are what the canonical result reports per attempt.

## Lifecycle

1. **Open the attempt.** The controller registers the attempt before anything
   can be written under it.
2. **Begin an upload.** The service reserves the content address and returns a
   short-lived write-once handle. Reusing the same `upload_id` returns the same
   reservation, so a client that timed out can retry safely.
3. **Finalize.** The service streams the stored object back from the gateway,
   recomputes its digest, and refuses to publish unless digest, size, media type
   and compression all match the declared intent. Finalize is idempotent:
   concurrent callers get one artifact and one durable event.
4. **Close the attempt** with its terminal outcome and observed GPU lifecycle.
5. **Commit the stage.** Exactly one `scientific-artifact-manifest/v1` per
   `(operation, stage)`. The commit must name precisely the stage's succeeded
   attempts, and every committed artifact must belong to one of them.
6. **Commit the result.** Exactly one canonical `scientific-run-result/v1` per
   operation, assembled from the stored attempts rather than from caller input.

## Fences

Three fences are enforced in SQL, so they hold even if a caller misbehaves:

| Fence | Rule | SQLSTATE |
| --- | --- | --- |
| Scope | The operation must exist and own the tenant | `FS201` |
| Stale attempt | A superseded attempt number cannot write | `FS201` |
| Terminal | A published result blocks all later writes | `FS203` |

Rows are immutable: `UPDATE` is refused except for the two documented one-way
transitions, running to terminal on an attempt and unfinalized to finalized on
an upload. Everything else raises `FS202`.

The runtime database role has no table-level `UPDATE` on artifacts, commits,
results or events at all, so an attempted rewrite is refused by the grant before
the trigger is even reached.

## Handles

Handles are presigned by the AWS SDK, so they carry a real SigV4 signature that
an unmodified S3-compatible gateway accepts. The upload signature binds the
object key **and** the declared content type, so a client that uploads different
bytes under a different media type is rejected by the gateway rather than only
by finalize.

Handles live at most fifteen minutes, default to ten, and are never persisted.
The deadline is stamped from the same wall clock the SDK signs with, because
`generate_presigned_url` accepts a duration rather than a deadline; anchoring it
anywhere else would advertise an expiry the gateway does not enforce.

## Bytes through the gateway itself

A presigned handle only helps a caller that can reach object storage. An
external customer usually cannot: the object store sits behind its own
endpoint, and in a private deployment nothing but the public gateway is
routable. So the same bytes also move through the API.

`PUT /v1/scientific-artifacts/uploads/{upload_id}/content?operation_id={id}`
takes the artifact bytes as the request body. The upload must already be
reserved, and the reservation is what the bytes are judged against: the
service measures the body and compares its SHA-256, length, media type and
content encoding with the immutable upload intent **before** any object is
created. A body that disagrees is rejected with `422` and stores nothing, so a
mismatched upload can never be finalized. The adapter then re-measures the
object it actually persisted, so a store that rewrote or re-typed the body is
caught at write time rather than surfacing later as a puzzling finalize
failure. A finalized content address is write-once and answers `409`.

`GET /v1/artifacts/{artifact_id}/content` streams one artifact's exact bytes to
the tenant that owns it, in bounded chunks, so a large result is never
buffered. The response carries `x-fs2-artifact-sha256`, `x-fs2-artifact-id` and
`x-fs2-artifact-size-bytes`, and the body is the addressed object itself: the
SHA-256 the client computes over what it received must equal that header.
`content-encoding` is deliberately **not** set for a compressed artifact,
because transparent decompression by an HTTP client would break exactly that
equality; compression is reported as `x-fs2-artifact-compression` instead.

The inline path is bounded, and that bound is honest rather than implicit. The
begin response returns `max_content_bytes` alongside `content_path`, so a client
discovers both where to write and how much it may write. An object above the
ceiling is refused with `413` and must use the presigned handle, which stays
available and unchanged. The ceiling can never exceed `max_request_bytes` or
the artifact ceiling; configuration that tries to is rejected at startup.

Nothing about this path relaxes the tenant boundary. The upload is resolved
through the caller's own tenant, and a foreign tenant receives `404` on the
reservation, on the bytes and on finalization alike.

## Retention

Retention only ever applies to an operation that has already published its
terminal result. A purge claims the operation by inserting into
`fs2_scientific_retention_ledger`, whose unique operation identity is the lock,
then deletes the objects and finally the rows. Deletes are refused unless that
transaction sets `fs2.retention_purge`, so the immutability guarantee still
holds for everything else.

Object deletion runs before the durable rows are removed and is idempotent, so
an interrupted purge converges on the next pass instead of leaving metadata
pointing at bytes that are already gone. The ledger survives the purge it
records, so what was deleted and when remains provable.

## Configuration

The service is disabled by default. It is mounted only when object storage is
fully configured, so an unconfigured cluster never exposes an endpoint backed by
absent credentials.

| Helm value | Environment variable | Notes |
| --- | --- | --- |
| `scientificArtifacts.enabled` | `FS2_SCIENTIFIC_ARTIFACTS_ENABLED` | Gates the routes entirely |
| `scientificArtifacts.endpoint` | `FS2_ARTIFACT_STORE_ENDPOINT` | HTTPS unless TLS verification is off |
| `scientificArtifacts.bucket` | `FS2_ARTIFACT_STORE_BUCKET` | Same region as the cluster |
| `scientificArtifacts.region` | `FS2_ARTIFACT_STORE_REGION` | |
| `scientificArtifacts.addressingStyle` | `FS2_ARTIFACT_STORE_ADDRESSING_STYLE` | `path` or `virtual` |
| `scientificArtifacts.handleTtlSeconds` | `FS2_ARTIFACT_HANDLE_TTL_SECONDS` | 30 to 900 |
| `scientificArtifacts.maxBytes` | `FS2_ARTIFACT_MAX_BYTES` | |
| `scientificArtifacts.inlineContentMaxBytes` | `FS2_ARTIFACT_INLINE_CONTENT_MAX_BYTES` | Gateway byte ceiling; at most `max_request_bytes` |
| `scientificArtifacts.retentionSeconds` | `FS2_ARTIFACT_RETENTION_SECONDS` | |
| `scientificArtifacts.mediaTypes` | `FS2_ARTIFACT_MEDIA_TYPES` | Exact allowlist |
| `secrets.artifactStore` | `FS2_ARTIFACT_STORE_CREDENTIALS_FILE` | Mounted `0400`, never an env value |

Credentials are a JSON object with `access_key_id` and `secret_access_key`,
projected read-only into the pod. They are never passed as environment values.

`scientificArtifacts.egressCidrs` opens TCP 443 to object storage in the
default-deny NetworkPolicy. Leave it empty and finalize cannot reach the
gateway, because presigning is local but digest verification is not.

## Customer upload and authorization

The public path never asks a customer to manufacture an internal Operation,
stage, attempt, or upload identity. `POST /v1/scientific-artifacts/uploads`
requires `inference.invoke`, a permitted target model, and an
`Idempotency-Key`. It creates an isolated
`scientific-artifact-upload-v1` Operation plus deterministic attempt/upload
identities, then returns the write-once handle. Generic inference workers
explicitly exclude this protocol. Finalization independently verifies the
stored bytes and terminalizes the upload Operation as `artifact_uploaded`.

A customer first uploads each input, then uploads a canonical
`scientific-artifact-manifest/v1` referring to those immutable pointers, and
passes the manifest pointer to `POST /v1/models/{model_id}:submit`. Upload
artifacts use the standard access profile but remain tenant-scoped; “public” in
the internal access-admission vocabulary does not mean anonymous access.

`GET /v1/artifacts/{artifact_id}/download` requires `operations.result` and
returns a fresh short-lived handle. Handles are the only bearer material
returned by these methods and remain excluded from persistence and logs.

Customer input staging depends on the artifact plane and the declared profile
set, not on the batch controller. An input may therefore be staged for a
profile whose runtime is not GPU-qualified yet, but never for a model this
deployment does not declare at all: an unknown `model_id` is refused. Only
submission requires a runnable profile.

MCP carries the whole flow, not a subset of it, so an MCP client needs no
object-store access either:

| MCP tool | HTTP equivalent |
| --- | --- |
| `begin_scientific_artifact_upload` | `POST /v1/scientific-artifacts/uploads` |
| `put_scientific_artifact_bytes` | `PUT /v1/scientific-artifacts/uploads/{id}/content` |
| `finalize_scientific_artifact_upload` | `POST /v1/scientific-artifacts/uploads/{id}:finalize` |
| `submit_scientific_run` | `POST /v1/models/{model_id}:submit` |
| `get_scientific_status` | `GET /v1/operations/{id}` |
| `get_scientific_result` | `GET /v1/operations/{id}/result` |
| `get_scientific_artifact` | `GET /v1/artifacts/{id}` |
| `read_scientific_artifact_bytes` | `GET /v1/artifacts/{id}/content` |
| `download_scientific_artifact` | `GET /v1/artifacts/{id}/download` |

The two byte tools carry base64 and enforce the same inline ceiling, rejecting
an over-large payload on its encoded length before decoding it. They apply the
identical digest, size, media-type and tenant checks as the HTTP routes,
because they call the same service.

The controller surface stays under `/internal/scientific-artifacts`. It takes
the tenant from the verified bearer principal; no request body can choose one.
Controller writes require `artifacts.write`, reads require
`operations.result`. A caller from another tenant receives `404`, never `403`,
so the surface leaks no existence.

| Route | Scope |
| --- | --- |
| `POST /v1/scientific-artifacts/uploads` | `inference.invoke` and model policy |
| `PUT /v1/scientific-artifacts/uploads/{id}/content` | `inference.invoke` and owning principal |
| `POST /v1/scientific-artifacts/uploads/{id}:finalize` | `inference.invoke` and owning principal |
| `GET /v1/artifacts/{id}/content` | `operations.result` and tenant boundary |
| `GET /v1/artifacts/{id}/download` | `operations.result` and tenant boundary |
| `POST /attempts` | `artifacts.write` |
| `POST /attempts/{id}:close` | `artifacts.write` |
| `POST /uploads` | `artifacts.write` |
| `POST /uploads/{id}:finalize` | `artifacts.write` |
| `POST /stages:commit` | `artifacts.write` |
| `POST /operations/{id}:result` | `artifacts.write` |
| `GET /{id}:download` | `operations.result` |
| `GET /operations/{id}/stages/{stage}:commit` | `operations.result` |
| `GET /operations/{id}:result` | `operations.result` |
| `GET /operations/{id}/events` | `operations.result` |

## How the batch controller consumes this

`ScientificBatchRepository.artifact_commit` is answered from the stage commit
this service published, through `ScientificArtifactBatchBridge`. The controller
accepts a commit only when it is semantically valid and its attempt identities
are exactly the stage's succeeded attempts, so the repository must not maintain
a second copy of that state.

## Testing

```bash
# The public gateway-only byte flow, tenant isolation and MCP parity
uv run pytest -q tests/test_scientific_artifact_public_bytes.py

# Contract, fencing, privacy and retention, plus real PostgreSQL
FS2_TEST_DATABASE_URL=postgresql://... uv run pytest -q tests/test_scientific_artifacts.py

# Real S3-compatible gateway, including signature acceptance and expiry
FS2_TEST_S3_ENDPOINT=http://127.0.0.1:9000 \
FS2_TEST_S3_ACCESS_KEY=... FS2_TEST_S3_SECRET_KEY=... \
  uv run pytest -q tests/test_scientific_object_store.py
```

Both suites skip cleanly when their environment is absent, so the default run
stays offline.
