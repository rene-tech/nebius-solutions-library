# Customer data buckets

Customer storage is independent of model weights, snapshots, and scientific
result storage. It provisions bounded S3 workspaces and expiring credentials;
it does not send credentials to model runtimes or MCP tools.

## Security architecture

The public control-plane pods contain no cloud provisioning credential. A
single, non-serving `storage-reconciler` Deployment consumes durable requests
from PostgreSQL and is the only workload that mounts two cloud identities:

- a project-scoped `editor` creates buckets, service accounts, and expiring S3
  access keys;
- a separately held project-scoped `admin` creates storage groups and group
  memberships, which Nebius does not permit to an editor.

Use a dedicated customer-data project. The Terraform input rejects the platform
project, requires distinct externally managed Kubernetes Secrets, and registers
only the public halves of expiring JWT keys. Private keys must be generated and
rotated outside Terraform; they must never enter a plan or state file. The
reconciler's HTTPS egress is limited to reviewed Nebius API CIDRs. Public runtime
NetworkPolicies do not contain a customer-storage HTTPS exception.

New installations default to per-user buckets. Shared tenant mode is available
only as an explicit compatibility choice and cannot be changed after the first
bucket without a data migration. Each bucket has versioning enabled, retains
three noncurrent versions for 30 days, and aborts incomplete multipart uploads
after seven days.

New bucket names are opaque keyed identifiers; tenant and user slugs are not
published through provider bucket listings. Existing bucket names remain
unchanged because reconciliation never renames or replaces a data bucket.

Customer S3 secrets are AES-GCM encrypted in PostgreSQL with AAD bound to the
tenant and principal. Stored secrets remain dependent on the payload key ring;
retain old key-ring entries until all associated credentials have been rotated.

## Configuration

Configure the workload stage with a dedicated project, two pre-created Secret
names, matching public keys and expiries, and reviewed API destination ranges:

```hcl
customer_storage = {
  enabled                          = true
  project_id                       = "project-customer-data"
  default_mode                     = "user"
  quota_bytes                      = 5000000000
  excluded_tenants                 = []
  resource_credentials_secret_name = "customer-storage-resource"
  iam_credentials_secret_name      = "customer-storage-iam"
  resource_public_key_pem          = var.customer_storage_resource_public_key_pem
  iam_public_key_pem               = var.customer_storage_iam_public_key_pem
  auth_key_expires_at              = "2026-12-01T00:00:00Z"
  egress_cidrs                     = var.nebius_api_cidrs
  key_ttl_days                     = 90
}
```

The two Secrets are supplied by the credential rotation system and each exposes
only a `credentials.json` key to the reconciler. Do not manage their private
contents in this module.

### Existing-state adoption

Before the first managed rollout, copy the five existing Nebius resource
addresses (service account, group, membership, access permit, and public auth
key) from the provisioner handoff state into the corresponding
`resource_provisioner` addresses in the workloads state. The module's `moved`
blocks preserve the old logical names when that state is already colocated.
Use provider IDs obtained from a read-only inventory; never infer them from
display names.

The sixth historical resource, `tls_private_key.provisioner`, is deliberately
not adopted. Back up the old state, rotate to an externally generated expiring
key during the coordinated rollout, verify the new public-key identity, and
then remove only that obsolete private-key address from the retired state. The
pre-apply plan must show the five resources adopted without replacement, the
resource provisioner permit changing from `admin` to `editor`, and only the new
IAM-binder resources being created. Any bucket, customer identity, membership,
or access-key deletion is a stop condition.

## API and audit contract

User bearer tokens require the explicit `storage.credentials` scope to reveal,
rotate, or revoke a storage credential. The scope is absent from bootstrap/MCP
token defaults. Metadata remains available from `GET /v1/storage`.

- `POST /v1/storage/credentials` reveals the current credential with
  `Cache-Control: no-store`.
- `POST /v1/storage/credentials/rotate` replaces the key and deactivates its
  predecessor.
- `DELETE /v1/storage/credentials` deactivates the key.
- Equivalent admin routes live below
  `/admin/api/v1/users/{user_id}/storage/credentials` and require `admin`.

Every successful disclosure, rotation, and revoke appends a redacted audit row.
The row contains actor, tenant, target, action, and outcome, never an access key
or secret. Disabling a user waits for credential deactivation before committing
the disabled user record. The reconciler also repairs drift on its next pass.

## Rotation and offboarding runbook

1. Confirm the user and tenant from server-side identity; never accept a bucket
   or cloud account identifier from a caller.
2. Rotate when continued access is needed. Confirm the replacement has a future
   expiry, the previous key is inactive, and a redacted rotation audit exists.
3. Revoke before user offboarding. Confirm the API reports `revoked` and the
   provider key is inactive before disabling or deleting external identity data.
4. Retain the versioned bucket by default. Export or delete objects only after a
   separately approved retention decision and recorded customer authorization.
5. Remove group membership and service accounts only after retention is closed;
   never delete a bucket as a side effect of cluster or user teardown.
6. Rotate both provisioner JWT keys before expiry, update the two external
   Secrets, restart only the reconciler, and verify no public pod mounts them.

Rollback is chart revision rollback plus restoration of the prior externally
held credential Secrets. Do not roll back to a revision that remounts a cloud
credential into the public runtime. Existing buckets and encrypted user records
are deliberately preserved across rollback.

References: [Nebius IAM roles](https://docs.nebius.com/iam/authorization/roles),
[bucket policies](https://docs.nebius.com/object-storage/buckets/bucket-policy),
and [access keys](https://docs.nebius.com/iam/service-accounts/access-keys).
