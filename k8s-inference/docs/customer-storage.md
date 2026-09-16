# Customer data buckets

Customer storage is independent of model weights, snapshots, and scientific
result storage. It provisions bounded S3 workspaces and expiring credentials;
it does not send credentials to model runtimes or MCP tools.

## Security architecture

The public control-plane pods contain no cloud provisioning credential. A
single, non-serving `storage-reconciler` Deployment consumes durable requests
from PostgreSQL and is the only workload that mounts two cloud identities, the
dedicated `fs2_serve_storage` database login, and the storage key ring:

- a project-scoped `editor` creates buckets, service accounts, and expiring S3
  access keys;
- a separately held project-scoped `admin` creates storage groups and group
  memberships, which Nebius does not permit to an editor.

Use a dedicated customer-data project. The Terraform input rejects the platform
project, requires distinct externally managed Kubernetes Secrets, and registers
only the public halves of expiring JWT keys. Private keys must be generated and
rotated outside Terraform; they must never enter a plan or state file. The
reconciler's HTTPS egress is derived from a signed, 24-hour provider endpoint
resolution contract and rendered only as IPv4 `/32` and IPv6 `/128` host
routes. Public runtime NetworkPolicies do not contain a customer-storage HTTPS
exception.

New installations default to per-user buckets. The immutable layout and its
emergency enabled switch are separate: an existing layout can be disabled and
reactivated without replacing a bucket, while changing `tenant` to `user`
requires a data migration. Migration 0032 inventories historical tenant
layouts with multiple principals, marks them `inventory_required`, and disables
them. They are not represented as isolated until their object ownership has
been mapped and migrated. Each bucket has versioning enabled, retains
three noncurrent versions for 30 days, and aborts incomplete multipart uploads
after seven days.

New bucket names are opaque keyed identifiers; tenant and user slugs are not
published through provider bucket listings. Existing bucket names remain
unchanged because reconciliation never renames or replaces a data bucket.

Customer S3 secrets are AES-GCM encrypted in PostgreSQL with AAD bound to the
tenant and principal. The gateway database role cannot select the encrypted
columns; a security-definer function atomically consumes one exact
tenant/principal disclosure. The reconciler has no access to operations, audit,
token, result, or request-debug tables and does not mount the platform ledger
key. Retain old PayloadCipher generations in the storage-only key ring until
`payload_key_usage()` reports zero storage rows for them; reconciliation
re-encrypts current and staged envelopes under the active storage generation
with a row-version compare-and-swap.

The gateway necessarily handles plaintext for the single HTTP response and can
invoke the exact-row disclosure function. It therefore is not claimed to
protect a credential from arbitrary gateway code executing during that
authorized disclosure. The narrowed database grants prevent bulk ciphertext
selection, and consumable disclosure bounds replay; moving plaintext handling
to a separately authenticated broker would be a distinct architecture change.

## Configuration

Configure the workload stage with a dedicated project, two pre-created Secret
names, matching public keys and expiries, and a signed endpoint-resolution
contract:

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
  egress_contract_json             = file(var.customer_storage_egress_contract_file)
  egress_contract_public_key_pem   = file(var.customer_storage_egress_public_key_file)
  key_ttl_days                     = 90
  rotation_window_days             = 14
}
```

`auth_key_expires_at` must be valid RFC3339, later than the current Terraform
plan timestamp, and no more than 90 days later. Before each plan, create the
short-lived egress artifact with an offline Ed25519 signing key:

```bash
uv run --project components/control-plane python \
  stages/workloads/scripts/customer_storage_egress_contract.py \
  --create --private-key /secure/operator/egress-contract.pem \
  > customer-storage-egress-contract.json
```

The Terraform external verifier rejects missing, empty, aggregate, arbitrary,
expired, incorrectly signed, or DNS-stale sets. After rollout, run the same
tool with `--contract`, `--public-key`, and a JSON copy of the live
`NetworkPolicy` via `--network-policy`; equality and exact TCP/443 are required.

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

- `POST /v1/storage/credentials` consumes the current credential's single
  disclosure and returns it with `Cache-Control: no-store`. A replay is denied
  and audited. Rotation creates one new consumable disclosure.
- `POST /v1/storage/credentials/rotate` replaces the key and deactivates its
  predecessor.
- `DELETE /v1/storage/credentials` deactivates the key.
- Equivalent admin routes live below
  `/admin/api/v1/users/{user_id}/storage/credentials` and require `admin`.

Every successful disclosure and replay denial, plus every API-requested
rotation and revoke, appends a redacted audit row. The row contains actor,
tenant, target, action, and outcome, never an access key or secret. Disabling a
user waits for credential deactivation before committing the disabled user
record. The reconciler also repairs bucket-policy and group-membership drift to
one exact per-user editor.

## Release and historical-data gate

The first release is deliberately serialized and reversible. Do not deploy it
until the currently deployed source lineage is contained in the rollout branch
and the following preconditions are recorded:

1. Back up PostgreSQL and both Terraform states. Record the current Helm
   revision and images without reading Secret values.
2. Inventory the dedicated customer-data project and bind every existing
   service account, access-key resource ID, bucket ID, bucket policy, and group
   membership to its exact database owner. Any unknown or extra principal is a
   stop condition.
3. Produce a saved Terraform plan that adopts existing provisioner resources,
   changes the resource identity from `admin` to `editor`, adds only the split
   IAM identity in the dedicated project, and gives both JWT public keys a
   future expiry of at most 90 days. Bucket or customer-key replacement in this
   plan is forbidden.
4. For every `inventory_required` tenant layout, keep the policy disabled.
   Obtain a customer-approved object-to-principal mapping, copy and verify the
   objects into opaque per-user buckets, replace the shared group grant, and
   only then mark the migration ready. If no safe mapping exists, the tenant
   remains disabled; this repository makes no cross-principal-isolation claim
   for the retained shared bucket.
5. Roll out migration 0032 and one reconciler first. Wait until all historical
   active rows complete `rotate`, disabled rows complete `revoke`, every
   predecessor provider resource is `INACTIVE`, every replacement is `ACTIVE`
   with bounded expiry, and no owner has two active keys beyond one observed
   reconciliation cutover. Keep old PayloadCipher generations until storage
   generation usage is zero.
6. Roll out gateway pods only after the database privilege checks pass. Verify
   landing/catalog, scoped PAT denial and one-time disclosure, rotation/revoke,
   storage access, tenant-filtered admin access, and the complete enable,
   capture, view, export, purge, and disable request-debug workflow.
7. Verify the live NetworkPolicy equals the signed contract. Verify the old
   project-wide public key and every predecessor customer key are inactive, and
   that the new provisioner public keys expire within the recorded bound.

## Rotation and offboarding runbook

1. Confirm the user and tenant from server-side identity; never accept a bucket
   or cloud account identifier from a caller.
2. Rotate when continued access is needed. Rotation is automatically queued in
   the configured pre-expiry window. Confirm the replacement has a future
   bounded expiry, the previous resource ID is inactive, only the replacement
   is active after cutover, and a redacted rotation audit exists.
3. Revoke before user offboarding. Confirm the API reports `revoked` and the
   provider key is inactive before disabling or deleting external identity data.
4. Retain the versioned bucket by default. Export or delete objects only after a
   separately approved retention decision and recorded customer authorization.
5. Remove group membership and service accounts only after retention is closed;
   never delete a bucket as a side effect of cluster or user teardown.
6. Rotate both provisioner JWT keys before expiry, update the two external
   Secrets, restart only the reconciler, and verify no public pod mounts them.

Rollback is application rollback only while keeping migration 0032, the narrow
database grants, deactivated predecessor keys, and split project IAM in place.
Never reactivate an old admin/public key or restore a revision that mounts cloud
credentials into the public runtime. Existing buckets and encrypted user
records are deliberately preserved across rollback.

References: [Nebius IAM roles](https://docs.nebius.com/iam/authorization/roles),
[bucket policies](https://docs.nebius.com/object-storage/buckets/bucket-policy),
and [access keys](https://docs.nebius.com/iam/service-accounts/access-keys).
