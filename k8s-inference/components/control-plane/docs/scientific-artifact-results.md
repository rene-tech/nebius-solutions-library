# Scientific artifact and result backend

`fs2_serve.scientific_artifacts` is the reusable persistence boundary for
asynchronous scientific workloads. No API, MCP, controller, model-adapter, or
live-deployment wiring is included in this slice.

## Contract and lifecycle

An adapter creates a stable UUID for `BeginArtifactUpload` and retries that
same intent after a timeout. The service derives the only accepted object key:

```text
scientific/v1/tenants/{tenant}/operations/{operation}/attempts/{attempt}/{input|output}/sha256/{digest}
```

The repository atomically compares `tenant_id` and `attempt` with the current
`fs2_operations` row before it appends the upload and its `upload_begun` event.
The returned PUT handle is generated only after that commit, must be
write-once, and expires within 15 minutes. A content address has one upload
intent: retry the same upload UUID, because a different UUID cannot mint a
second handle for the same key. Handles are never accepted by, or sent to, a
repository method; finalized intents never mint another PUT handle.

Finalization asks the trusted object-store adapter to hash and measure the
stored bytes. It compares the measured SHA-256 digest, byte count, media type,
optional compression, and storage key with the durable upload intent before
appending an immutable internal `ArtifactRecord` and `artifact_finalized`
event. The record uses
`fs2-serve.nebius.ai/scientific-artifact-record/v1`; it does not claim or reuse
the shared public ArtifactRef schema. The object-store protocol returns
metadata only; the service never receives biological bytes.

`ArtifactRecord.to_public_ref()` is the only projection boundary supplied by
this module. Its schema-neutral value contains exactly `artifact_id`, raw
lowercase 64-character `sha256`, `size_bytes`, `media_type`, and optional
`compression`. Tenant, attempt, direction, storage key, access profile/receipt,
and timestamps remain internal. The shared contract branch owns the public
ArtifactRef/`scientific-artifact-pointer/v1` schema; focused tests validate the
projection against that merged schema without redefining it here.

A terminal internal result record contains content-addressed input/output
records, exact model/runtime/workload/scheduling identities, bounded
Kubernetes execution UIDs, and a semantic-validator identity plus its evidence
artifact. Successful results require a validated output. Canonical JSON
produces the manifest SHA-256. A unique operation key and an immutable-row
trigger permit exactly one terminal record; an identical replay is idempotent
and a different replay is a conflict. Its internal schema is
`fs2-serve.nebius.ai/scientific-result-record/v1`, leaving the shared contract
branch as the sole owner of public result schemas.

All write transactions hold a share lock on the operation row. A retry that
advances the operation attempt therefore fences late upload, finalize, event,
and result commits. The database trigger repeats that check even for writes
outside the repository.

## Privacy and gated artifacts

The persistence schema contains hashes, sizes, closed media types, object keys,
non-secret execution identities, and a gated-access receipt digest. It has no
object bytes, request/response body, credential, presigned URL, signed header,
free-form event detail, argv, or environment field. Restricted and academic
artifacts require a non-secret access receipt digest; public artifacts cannot
claim one.

Pydantic validation errors suppress input values. Handles redact their URL and
headers from `repr`, and the service performs no logging. Integrators must keep
the same boundary: return a handle directly to the authorized caller, never
serialize it into an operation, event, manifest, audit record, exception, or
log field.

## Repository and migration integration

`MemoryArtifactRepository` is a deterministic backend for adapter and
concurrency tests. `PostgresArtifactRepository` uses the existing asyncpg pool
and operation ledger. The additive migration is
`0014_scientific_artifact_results.sql`; the normal migration runner applies it
under the sole DDL owner and grants the runtime role only the bounded DML needed
by this repository.

Production migrations are forward-only and immutable. For disposable
pre-release databases, `SCIENTIFIC_ARTIFACT_ROLLBACK_SQL` is an explicit
owner-only down path. It removes the four tables, their triggers/functions, and
the `0014` ledger row. The focused PostgreSQL test executes up/down/up against
the real migration runner. Do not run the down path after later migrations or
against a database containing retained scientific results.

Integration owners should wire the service only after the shared scientific
batch API and controller contracts are merged. The object-store adapter must
independently stream/hash the stored object; trusting caller-provided metadata
does not satisfy finalization. API authorization must scope every begin,
finalize, download, result, and event call to the operation tenant.

## Verification

From `components/control-plane`:

```bash
uv run pytest -q tests/test_scientific_artifacts.py
uv run ruff check src/fs2_serve/scientific_artifacts.py tests/test_scientific_artifacts.py
uv run ruff format --check src/fs2_serve/scientific_artifacts.py tests/test_scientific_artifacts.py
PYTHONPATH="src:../../catalog/runtime" uv run mypy src
```

Set `FS2_TEST_DATABASE_URL` to a disposable PostgreSQL database to exercise the
real concurrency fences and migration up/down/up path. The tests clean their
artifact and operation fixtures and leave the migration reapplied.
