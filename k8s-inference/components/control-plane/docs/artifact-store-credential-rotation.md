# Scientific artifact-store tenant identity and rotation

This runbook owns the SAI-19 storage boundary. Production must use the external
credential broker configured under `scientificArtifacts.credentialBroker`.
The control-plane pod mounts only a projected, audience-bound workload token;
it does not mount tenant S3 credentials. For each operation the broker receives
the independently authorized tenant, canonical key, action and (for reads and
downloads) immutable provider object version. Provider deletion is dormant in
this candidate. The tenant broker
performs the exact provider operation itself and returns only the operation
result; provider credentials never cross into the control plane.

The legacy shared credential remains declared and `prevent_destroy`. This
candidate intentionally keeps its prefix authorization during the reversible
`legacy-overlap` phase: infrastructure is applied before workloads, so removing
that grant first would interrupt the still-running gateway. Ordinary Terraform
cannot select an active cutover phase, and the workloads stage renders neither
the one-shot closer nor its forever-retrying controller. The compatibility
bridge remains open until a separate reviewed activation can cryptographically
verify the complete provider/IAM, version/input inventory, broker fleet drain,
gateway handoff, database watermark, and rollback state. A caller-supplied
digest is not such authority.

Ordinary Terraform also creates a distinct provider identity, exact-prefix
group binding, access key and immutable Secret for each tenant and retained
generation. Each broker process mounts exactly one active tenant generation;
the gateway, authority issuer and maintenance process mount none. This static
candidate does not claim the long-lived credential design is final or ready
for integration; per-request provider federation and authoritative IAM closure
remain independent acceptance gates.

The artifact clients are created only when the artifact plane is enabled.
Broker metadata, proxied PUT, and streamed-read calls use the bounded operation
timeout; connection and pool waits remain bounded by the shorter broker
timeout. Each authorized operation and broker readiness probe verifies bucket
versioning plus exact tenant-prefix visibility, and every response carries the
credential generation and provider-binding digest checked against the caller's
mounted routing inventory. This detects a stale or unusable selected broker,
but it does not prove provider-wide IAM closure and cannot authorize a future
generation switch by itself.

## Broker and provider policy

Before enabling the broker, the storage owner must prove all of these and
record the provider-side resource IDs outside Git:

1. Authenticate the expected projected workload-token audience, then verify a
   separate raw PAT or scientific-workload capability. Derive tenant,
   operation, attempt, key, and version from PostgreSQL; request fields and key
   parsing are consistency checks, not authority.
2. Exact-match the canonical `scientific/v1/tenants/<tenant-id>/` prefix and
   the requested action. For versioned actions, bind the grant to the requested
   immutable version as well.
3. Run one independently addressable broker per tenant. Mount only that
   tenant's active provider-key generation and make the broker perform the
   authorized operation without returning the key. Bind the provider endpoint
   to `https://storage.<region>.nebius.cloud`; no operator-supplied exchange or
   STS URL may receive a projected Kubernetes token.
4. Prove reads, writes, listing, and deletion outside that tenant prefix are
   denied, and prove deletion is denied even inside the tenant prefix. A second
   tenant's exact key and a same-key wrong-tenant request are mandatory
   negative cases.
5. Give each tenant provider group only `storage.uploader`,
   `storage.object-viewer`, and `storage.object-lister` on the exact
   `scientific/v1/tenants/<tenant>/*` bucket-policy path. Never give a broker
   `storage.object-editor` or another `DeleteObject` grant. Give the gateway and
   authority issuer no bucket-policy or credential-management authority.
6. Keep bucket versioning enabled and configure no lifecycle expiration for
   noncurrent versions. The provider resource used here does not expose Object
   Lock, so delete-free credentials plus indefinite exact-version retention are
   the enforced write-once-equivalent: an attacker may create a newer version,
   but cannot remove the finalized VersionId pinned in PostgreSQL. Do not claim
   provider Object Lock until a separate authoritative resource implements it.

`artifact_broker_server.py` implements that independent boundary with its own
ServiceAccount, Deployment, TokenReview credential and least-privilege database
login. The gateway forwards the unmodified caller bearer separately from its
workload identity. The broker loads the durable row itself, verifies the PAT or
workload capability and operation ownership, exact-matches every identity, and
only then selects the tenant provider binding.

Artifact capabilities are not HMACs. The ordinary gateway ledger HMAC remains
an idempotency/ledger primitive, while the token pepper verifies an already
issued raw PAT or operator-session proof against durable state. Neither can
produce an artifact capability. Capabilities use Ed25519: only the isolated
authority issuer mounts the private key, tenant brokers mount only the public
verification key, and the gateway mounts neither. The issuer and each broker
independently re-read the durable operation, attempt, artifact, or operator
session named by the claim. A rollout must prove this mount separation from the
rendered manifests and running pod specs before artifact traffic is enabled.
Terminal scientific-batch publication has its own read-only capability: the
issuer derives the current controller ID and fencing token from the active
batch lease, signs one artifact ID, and the broker repeats that lease check.
It cannot write, list, delete, or read an artifact from another operation.

## Future staged rotation (not activated by this candidate)

Keep every old provider generation declared and protected from destruction.
Do not revoke or delete first.

1. Record the broker deployment revision and image digest, workload-identity
   audience, provider policy/version IDs, and old signing/key generation.
   Never record credential values or broker responses.
2. In a first apply, add generation N to `retained_generations` and
   `authorized_generations` while N-1 remains active and authorized. Do not
   change `active_generation`. Prove the generation-addressed N Service reaches
   every expected N replica and run positive own-prefix plus negative
   foreign-prefix, wrong-tenant, wrong-action, delete, and wrong-version checks.
3. Stop. The current Terraform contract requires `active_generation = 1` and
   rejects this switch even when N-1 and N are both authorized. Kubernetes
   Deployment readiness and the broker's local identity echo do not prove the
   provider key, bucket/versioning, exact prefix, database and issuer path.
   A future source change must consume a purpose-bound, independently signed
   pre-cutover receipt for the exact tenant and generation before it may change
   the stable Service selector.
4. After that future gate exists, verify upload, finalize, service-mediated content read, signed download,
   materialization, retention visibility, and a foreign-tenant denial on the
   new broker replicas. Verify wrong-tenant dispatch fails before exchange and
   that finalized downloads remain pinned to the stored provider version.
5. This source candidate stops before steps 3-4. Its `legacy-overlap` validation requires
   `authorized_generations` to equal `retained_generations`, so an apply that
   selects N cannot revoke N-1 and every generation through N must have been
   prepared. A future, independently reviewed protocol may permit a third apply
   only after a signed, independently witnessed fleet-wide receipt proves every
   N-1 replica drained, exact provider/IAM closure, and a completed rollback
   window. That future protocol must retain the old identity, key and immutable
   Secret; credential revocation or resource deletion remains outside this
   candidate.

If any verification fails, keep the old generation active and roll the broker
and provider IAM bindings back to their recorded revisions. Do not enable a
static per-tenant or shared-key mode. This task authored the runbook and
regression contracts under a static-source-only boundary; it did not execute a
credential rotation or live verification.

## Abandoned upload cleanup and historical version backfill

A public signed PUT can succeed even when the client never calls finalize.
URL expiry bounds when a request may start, not when a permitted 1 TiB transfer
must finish, so a fixed grace period cannot prove abandonment. Ordinary source
therefore renders no orphan-cleanup CronJob, and the preserved cleanup entry
point returns without claiming or fencing an upload. Its append-only ledger,
bounded listing, signed observation and write-fence implementation remain
preserved for review, but must not be activated until provider-backed
multipart/in-flight closure or an equivalent authoritative quiescence proof is
available. Provider bytes and database metadata remain retained in the
interim; this candidate does not claim abandoned-upload reclamation.

Migration `0031_scientific_artifact_version_backfill.sql` adds an append-only
proof ledger and the only permitted `NULL` to exact-version transition. Run
`fs2-serve artifact-version-backfill` from the maintenance ServiceAccount with
an immutable, root-owned schema-v1 manifest containing explicit artifact IDs
and candidate provider version IDs. The process cannot list a bucket or choose
`latest`: it asks the broker for a maintenance-only `backfill-inspect` session,
reads and hashes the exact version, verifies digest, size, media type and
compression, computes a canonical evidence receipt, and invokes the
security-definer transition. The trigger verifies every immutable row field
and matching ledger proof before accepting the version.

Canary one artifact first, verify exact-version read and download, then advance
in bounded batches. An operation with any unresolved version remains wholly
purge-fenced: no object delete, metadata delete, or purge count occurs. The
transition is intentionally one-way, so rollback means stopping the backfill
and returning application/broker revisions; already proved version bindings
remain valid immutable evidence and must not be cleared.

## Handle properties

Presigned URLs are bearer material. They necessarily expose the storage
endpoint, bucket, canonical tenant prefix, and operation identity to the
authorized recipient. They must never be logged, persisted, placed in model
context, or sent in a referrer. Upload and download defaults are two minutes and
both are capped at five minutes in the service, independently of the legacy
ten-minute setting retained for rollback compatibility.

PUT URLs cannot be revoked individually, but their signature binds
`If-None-Match: *` and the content-address SHA-256 checksum header, so replay
cannot replace an existing object and the provider can reject bytes that do not
match the declared digest. Finalization stores the provider's immutable version
ID. Download URLs sign that exact version rather than the mutable latest key,
eliminating the inspect-then-sign race and avoiding a full pre-download read.
Direct-download clients still verify the artifact reference's SHA-256 as an
end-to-end check.

The direct-upload handle names `x-amz-version-id` as its
`version_response_header`; clients must capture that exact provider response
header and submit it to finalize. Browser clients may use the gateway
`content_path`, whose response exposes the same value as
`x-fs2-object-version-id`. Before enabling cross-origin direct uploads, the
storage owner must independently prove that the exact trusted origin is allowed
and `x-amz-version-id` is exposed by provider CORS. A deployment without that
proof must keep browser uploads on `content_path`; it must not fall back to a
latest-object lookup.

Service-mediated content reads are restricted to the configured inline ceiling.
They read the exact finalized version once, buffer within that ceiling, verify
the complete digest, size and metadata, and release no byte before verification
succeeds. Larger artifacts use the exact-version download URL and therefore do
not incur a service-side full read followed by a second object-store read.
Historical rows without a version ID fail closed on every byte-release path
until an authorized, independently reviewed backfill binds them to an immutable
provider version.

## Integration gate

Ordinary Terraform now creates one provider principal, group and exact prefix
policy per declared tenant and retained generation, the independent tenant
broker Deployment/Service/NetworkPolicy, projected reviewer identities,
broker TLS/CA mounts, immutable per-generation credential Secrets, and a
dedicated column-scoped database login. The gateway receives only non-secret
routing bindings. Integration remains blocked until an independent review
validates authoritative provider-wide IAM/key closure, replaces or explicitly
accepts the long-lived per-tenant credential design, supplies distinct verified
TLS identities, implements a separate exact-version retention deletion
principal, proves complete legacy online/MCP as well as batch input enrollment,
and introduces a cryptographically verified multi-observer cutover receipt.
Until then the compatibility bridge and metadata remain retained and the
one-way cutover stays unreachable. This static source candidate is not
deployment or live evidence.
