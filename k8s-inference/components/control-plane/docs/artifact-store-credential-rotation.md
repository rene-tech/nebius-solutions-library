# Scientific artifact-store tenant identity and rotation

This runbook owns the SAI-19 storage boundary. Production must use the external
credential broker configured under `scientificArtifacts.credentialBroker`.
The control-plane pod mounts only a projected, audience-bound workload token;
it does not mount tenant S3 credentials. For each operation the broker receives
the independently authorized tenant, canonical key, action and (for reads,
downloads and deletes) immutable provider object version. It returns a
mandatory session credential scoped to that single tenant/action and expiring
within 15 minutes. The control plane does not cache brokered credentials.

Both static modes are explicit break-glass rollback mechanisms and are not an
accepted steady state: `allowStaticTenantCredentials` mounts every configured
tenant document into one process, while `allowLegacySharedCredentials` mounts
the historical all-tenant key. Neither mode remediates the control-plane
compromise blast radius.

If the static per-tenant rollback is used, the Secret contains one key named
`<tenant-id>.json` for each tenant. Each document has this shape; the values
below are non-secret examples.

```json
{
  "tenant_id": "tenant-example",
  "access_key_id": "provider-issued-tenant-key-id",
  "secret_access_key": "provider-issued-secret",
  "session_token": "optional-short-lived-session-token",
  "bucket": "tenant-example-scientific-artifacts"
}
```

`endpoint_url`, `region`, `addressing_style`, and `verify_tls` may also be set
per document. Missing connection fields inherit the non-secret Helm defaults.
The control plane refuses an empty directory, an unknown field, a filename that
does not equal `tenant_id`, an unknown tenant at request time, or an access-key
ID reused by two tenants. Secrets remain file-mounted and are not logged.

## Broker and provider policy

Before enabling the broker, the storage owner must prove all of these and
record the provider-side resource IDs outside Git:

1. Authenticate only the expected projected workload-token audience and bind
   the caller to its independently authorized tenant before inspecting the
   requested key.
2. Exact-match the canonical `scientific/v1/tenants/<tenant-id>/` prefix and
   the requested action. For versioned actions, bind the grant to the requested
   immutable version as well.
3. Mint a fresh session credential per request, require a session token, cap
   expiry at 15 minutes, and allow only the minimum requested operation.
4. Prove reads, writes, listing, and deletion outside that tenant prefix are
   denied. A second tenant's exact key and a same-key wrong-tenant request are
   mandatory negative cases.
5. Do not grant bucket-policy or credential-management authority to brokered
   sessions or to the control-plane workload identity.
6. Keep bucket versioning enabled. For regulated tenants, use a bucket created
   with object lock and a governance retention period longer than the maximum
   upload-handle lifetime. Confirm retention purge semantics before enabling
   lock; object lock is not a runtime toggle.

The application derives the expected canonical key from the authorized
repository record, asserts the record tenant and key tenant both match the
independent principal tenant, and only then calls the broker. Broker policy is
the separate authorization boundary if application routing is wrong.

## Zero-downtime rotation

Keep the old broker/provider signing generation active until the last old
broker replica and every credential it minted have expired. Do not revoke
first.

1. Record the broker deployment revision and image digest, workload-identity
   audience, provider policy/version IDs, and old signing/key generation.
   Never record credential values or broker responses.
2. Add the replacement broker/provider generation with the same
   least-privilege policy. Run positive own-prefix and negative foreign-prefix,
   wrong-tenant, wrong-action and wrong-version checks.
3. Roll the broker so new exchanges use the replacement while the old
   generation remains valid for already issued sessions.
4. Verify upload, finalize, service-mediated content read, signed download,
   materialization, retention visibility, and a foreign-tenant denial on the
   new broker replicas. Verify wrong-tenant dispatch fails before exchange and
   that finalized downloads remain pinned to the stored provider version.
5. After every old replica has drained, the maximum 15-minute credential window
   and rollback window have elapsed, deactivate the old provider generation.
   Record its deactivation time and the successful post-revocation smoke result.

If any verification fails, keep the old generation active and roll the broker
back to the recorded image/revision. The static per-tenant or shared-key modes
remain emergency compatibility paths only and must not be represented as an
accepted rollback state. This task authored the runbook and regression
contracts under a static-source-only boundary; it did not execute a credential
rotation or live verification.

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

Service-mediated content reads are restricted to the configured inline ceiling.
They read the exact finalized version once, buffer within that ceiling, verify
the complete digest, size and metadata, and release no byte before verification
succeeds. Larger artifacts use the exact-version download URL and therefore do
not incur a service-side full read followed by a second object-store read.
Historical rows without a version ID fail closed on every byte-release path
until an authorized, independently reviewed backfill binds them to an immutable
provider version.

## Integration gate

The existing Terraform artifact-store contract still provisions the historical
single identity and Secret. This source candidate must not be integrated or
rolled out until an independently reviewed infrastructure successor provides
the broker workload identity, broker endpoint and CA Secret, tenant/action IAM
policy, provider session minting, exact-version authorization and no-downtime
rotation path described above. The chart's broker default is deliberately
fail-closed until those inputs exist. Either static credential switch preserves
emergency compatibility but does not remediate SAI-19 and must not be used to
claim security acceptance.
