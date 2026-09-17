# Scientific artifact-store tenant identity and rotation

This runbook owns the SAI-19 storage boundary. Production must use one object
store identity per tenant prefix (or a dedicated tenant bucket). A single key
authorized for `scientific/v1/*` is a break-glass rollback mechanism, not an
accepted steady state.

The tenant credential Secret contains one key named `<tenant-id>.json` for each
tenant. Each document has this shape; the values below are non-secret examples.

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

## Provider policy

Before adding a tenant document, the storage owner must prove all of these with
that exact identity and record the provider-side resource IDs outside Git:

1. Allow only the configured bucket and
   `scientific/v1/tenants/<tenant-id>/*` prefix.
2. Allow only the object actions required by upload, verification, download,
   and retention purge. Do not grant bucket-policy or credential management.
3. Prove reads, writes, listing, and deletion outside that tenant prefix are
   denied. A second tenant's exact key is the mandatory negative case.
4. Prefer short-lived session credentials. If static S3 credentials are the
   only provider option, use a distinct key per tenant and rotate it on the
   schedule owned by the storage team.
5. Keep bucket versioning enabled. For regulated tenants, use a bucket created
   with object lock and a governance retention period longer than the maximum
   upload-handle lifetime. Confirm retention purge semantics before enabling
   lock; object lock is not a runtime toggle.

The application enforces the canonical tenant prefix before selecting an SDK
client and re-hashes every service-mediated read. Provider policy remains the
independent boundary if application routing is wrong.

## Zero-downtime rotation

Keep the old identity active until the last old control-plane replica is gone.
Do not revoke first.

1. Record the current Secret resource version, deployment revision, image
   digest, tenant document checksum, and the old access-key ID. Never record the
   secret value.
2. Create the replacement tenant identity with the same least-privilege policy.
   Run the positive own-prefix and negative foreign-prefix provider checks.
3. Update only that tenant's `<tenant-id>.json` in the Secret. Leave both old
   and new provider identities active.
4. Perform a rolling control-plane rollout. New replicas use the new identity;
   old replicas continue using the old identity, so in-flight requests remain
   valid.
5. Verify upload, finalize, service-mediated content read, signed download,
   materialization, retention visibility, and a foreign-tenant denial on the
   new replicas. Verify that a same-size object substitution after finalize is
   rejected by digest verification.
6. After every old replica has drained and the rollback window has elapsed,
   deactivate the old identity. Record its deactivation time and the successful
   post-revocation smoke result.

If any verification fails, keep the old identity active, restore the prior
Secret version, and roll back to the recorded image/revision. This task authored
the runbook and regression contracts under a static-source-only boundary; it did
not execute a credential rotation or a live verification.

## Handle properties

Presigned URLs are bearer material. They necessarily expose the storage
endpoint, bucket, canonical tenant prefix, and operation identity to the
authorized recipient. They must never be logged, persisted, placed in model
context, or sent in a referrer. Upload and download defaults are two minutes and
both are capped at five minutes in the service, independently of the legacy
ten-minute setting retained for rollback compatibility.

PUT URLs cannot be revoked individually, but their signed
`If-None-Match: *` precondition makes replay unable to replace an existing
content address. Digest verification independently rejects changed bytes when
they are read, while provider object lock adds defense in depth. Direct-download
clients must verify the `sha256` from the accompanying artifact reference;
service-mediated reads enforce it automatically.

## Integration gate

The existing Terraform artifact-store contract still provisions the historical
single identity and Secret. This source candidate must not be integrated or
rolled out until the integration owner either provisions
`fs2-serve-artifact-store-tenants` with the provider-policy proofs above or adds
an equivalent tenant-identity broker. The chart's default is deliberately
fail-closed. Setting `allowLegacySharedCredentials=true` preserves rollback
capability but does not remediate SAI-19 and must not be used to claim security
acceptance.
