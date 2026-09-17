# Customer data buckets

Customer storage is independent of model weights, snapshots, and scientific
result storage. It provisions bounded S3 workspaces and expiring credentials;
it does not send credentials to model runtimes or MCP tools.

## Security architecture

The public control-plane pods contain no cloud provisioning credential. A
single, non-serving `storage-reconciler` Deployment consumes durable requests
from PostgreSQL and is the only workload that mounts two cloud identities and
the dedicated `fs2_serve_storage` database login. A separate
`storage-disclosure` Deployment mounts only the storage envelope key ring, the
token/session pepper generations, and the execute-only
`fs2_serve_storage_disclosure` database login:

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
routes. The canonical enforcement is outside Kubernetes: the separately
approved `security/customer-storage-egress-authority` root adds a dedicated
tainted node group to the exact target cluster; the group's only VPC security
group has the signed provider/API routes and no default-route egress. Two
independently signed, read-only filesystem anchors
pin the prior manifest and generation heads, canonical provider/workloads state
lineages and managed addresses, exact provider IAM inventory, a
provider-native effective-authority graph including inherited/external
principals, accepted custody and complete Kubernetes credential inventory plus
a fresh exact RBAC object/subject inventory. A
second security-owner root adds immutable,
generation-named contract, trust and NetworkPolicy objects as defense in depth.
ServiceAccount/system groups are derived deterministically, and the live RBAC
graph independently recomputes every signed and authenticated subject's
direct-plus-group authority. Unmediated dangerous capabilities, including
resource-name-limited ConfigMap mutation, are rejected rather than blessed by
a signed descriptive list. Release ConfigMap create is admission-mediated to the exact Helm v1
record, update/patch is RBAC-restricted to that name, and delete is forbidden.
The reconciler verifies the signed bytes, freshness, live DNS, and the effective
union of every NetworkPolicy selecting its full actual label set, including its
controller-assigned `pod-template-hash`, before readiness.
Admission continuously permits only the exact content-bound selecting policy;
an additional signed policy constrains every Pod or workload-producing object
to the exact Secret allowlist, image and provider-protected node target.
The same retained policy has no namespace exemption. Each successor uses a
signed lane-unique selector and taint key plus an exact `Equal` toleration, so a
retained policy cannot select or deny a later storage or observer generation.
A keyless blanket `Exists` toleration is guarded because, without an excluding
selector or required affinity, it makes a Pod schedulable on the protected
tainted node. Admission evaluates the complete signed scheduling-label and
node-name projection: `nodeSelector` and each requirement within a term are
ANDed, terms are ORed, and the resulting constraints must match together with
the lane-taint tolerance. Direct `nodeName` is compared to the exact activated
node. The signed inventory binds the two additive lane
observers and the retained filesystem CSI, Prometheus node-exporter, and OTel
node agents to exact namespaces, names, UIDs, specs, release owners and
DaemonSet-controller child identities. This makes retained storage/telemetry
replacement Pods an explicit allow rather than a namespace or blanket-
toleration exception. UPDATE matching evaluates both the old and new object,
and Pod binding is admitted only from the provider-bound scheduler. The
security owner's otherwise namespace-wide RBAC create permission is
admission-confined to a generation-named, read-only NetworkPolicy inventory
Role and a same-name ServiceAccount binding; it cannot delegate ConfigMap or
workload authority. Every retained legacy and v3 policy and Deny binding is
re-read and compared with the canonical
separately signed prior checkpoint before a new generation is admitted. The
transition never edits or deletes a predecessor. Before a new protected node
group exists, the two exact observer successors are created against its
as-yet-unmatched signed lane key, their live UIDs/specs are captured and signed,
and the provider root activates one node and records its exact name before the
new Deny policy is installed. The credential-bearing storage
release; only its admitted pending Pod may trigger lane scale-up. Existing
observer and admission objects remain retained; the old and
new policy conjunction admits the exact successors and rejects a rogue blanket
DaemonSet.
Public runtime NetworkPolicies do not contain a customer-storage HTTPS
exception.

New installations default to per-user buckets. The immutable layout and its
emergency enabled switch are separate: an existing layout can be disabled and
reactivated without replacing a bucket, while changing `tenant` to `user`
requires a data migration. Migration 0032 inventories historical tenant
layouts with multiple principals, marks them `inventory_required`, and disables
them. They are not represented as isolated until their object ownership has
been mapped and migrated. Each bucket has versioning enabled. New buckets
receive the two historical lifecycle templates only in a disabled state. For an
existing bucket, reconciliation preserves every observed rule ID, order and
provider-specific field exactly and changes only an enabled status to disabled;
it never synthesizes or substitutes a missing historical rule. No rule name can
bypass the no-deletion default. Rules must not be enabled without a separate
customer-data retention and deletion authorization.

New bucket names are opaque keyed identifiers; tenant and user slugs are not
published through provider bucket listings. Existing bucket names remain
unchanged because reconciliation never renames or replaces a data bucket.
Unexpected or duplicate IAM group membership fails reconciliation before any
new membership or credential activation. The reconciler never deletes a
membership automatically; the exact drift remains preserved for separately
authorized review while disclosure stays fail-closed.

Customer S3 secrets are AES-GCM encrypted in PostgreSQL with AAD bound to the
tenant and principal. The gateway has neither the storage envelope key nor
permission to select encrypted columns or execute the disclosure consumer. It
forwards the raw authenticated credential to the narrow disclosure service;
PostgreSQL derives the live token/session actor, tenant, principal and admin
target from durable rows, creates a short-lived entitlement, atomically
consumes it once, and writes the redacted audit event in the same transaction.
The reconciler has no access to operations, token, result, or request-debug
tables and has only insert access to the audit outbox. Retain old PayloadCipher
generations in the storage-only key ring until
`payload_key_usage()` reports zero storage rows for them; reconciliation
re-encrypts current and staged envelopes under the active storage generation
with a row-version compare-and-swap.

The gateway necessarily proxies the single no-store response, but arbitrary
gateway code cannot enumerate or decrypt database envelopes. A disclosure
requires a still-live raw PAT with `storage.credentials`, or a still-live admin
cookie whose retained pepper generation matches the database-bound session.

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
  egress_boundary                  = var.customer_storage_egress_security_handoff
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

Before any Kubernetes change, a separately approved provider-security operator
applies `security/customer-storage-egress-authority`. It accepts only the
root-owned registry and its exact signed ledger, then returns the dedicated VPC
security-group/node-group handoff. The Kubernetes security operator passes that
handoff to `security/customer-storage-egress-boundary`. The ordinary workloads
identity owns neither root. The workloads plan rejects missing, mutable, empty,
aggregate, arbitrary, expired, incorrectly signed, DNS-stale, or non-equal
generations and rejects any widening policy in the effective selecting union.

The first migration is compatibility-first. The fixed predecessor Deployment,
NetworkPolicy, ConfigMap, VAP and binding remain unchanged and at their original
Terraform addresses under `prevent_destroy` plus `ignore_changes = all`; no
state-forget handoff exists. The retained first additive chart generation uses
component `storage-reconciler-v2`. Its additive compatibility successor remains
in `charts/security/customer-storage-reconciler-v2` for source compatibility
but creates distinct `fs2-storage-v3-*` objects with component
`storage-reconciler-v3`, a content-bound release identity, and generation-local
canonical admission specs. Neither the fixed nor retained v2 VAPs match or
reject the v3 generation. The exact
fixed object UIDs and content digests form a compatibility receipt whose digest,
workloads backend identity, state lineage and serial are committed by the
separately signed prior-head checkpoint. Both reconcilers
may overlap behind the existing durable locks. No fixed selector changes, and
no retirement occurs during this handoff.

Later contract or provider-route rotations are overlap-only. Add the signed
generation and a separately named reconciler; never remove an older ledger/map
entry or use target/replace. Every additive chart object carries Helm's `keep`
policy. Rollback means adding a prior application version under a fresh retained
authority generation, never uninstalling or deleting a generation.

Both security roots verify actual remote state lineage, serial, snapshot bytes,
non-empty exact managed addresses, and object-store version through fixed
root-owned read-only adapters. The provider prior-state receipt carries an
installed-generation hash chain ending at the separately anchored prior head.
It also carries the complete payload for every retained or partially installed
generation; all provider resources iterate retained-plus-new keys at their
original addresses, so rotation cannot plan predecessor deletion and then fail
at `prevent_destroy`.
The only future apply path descriptor-binds an externally signed exact saved
plan to a clean source commit/tree and predecessor state under fixed
root-owned read-only key, binary, plugin/data, CLI-config, environment and
backend custody. It rejects rejected-SAI-10 ancestry, permits only
create/read/no-op, freezes the backend descriptor across execution, and
verifies the actual successor state. It has no cleanup or state-forget mode.

The two Secrets are supplied by the credential rotation system and each exposes
only a `credentials.json` key to the reconciler. Do not manage their private
contents in this module.

The additive chart remains fail-closed until it receives an independently
accepted SAI-10 commit, tree and immutable review receipt from the signed
provider authority. The rejected SAI-10 commit
or any of its descendants are not valid custody inputs. The integration head
itself must also exclude that ancestry; preserved task-branch commits must be
transplanted onto the accepted clean lineage rather than rewritten in place.

### Existing-state adoption

Before the first managed rollout, copy the five existing Nebius resource
addresses (service account, group, membership, access permit, and public auth
key) from the provisioner handoff state into the corresponding
`resource_provisioner` addresses in the workloads state. The module's `moved`
blocks preserve the old logical names when that state is already colocated.
Use provider IDs obtained from a read-only inventory; never infer them from
display names.

The sixth historical resource, `tls_private_key.provisioner`, is deliberately
not adopted. Under the active no-delete/no-revoke constraint it remains
preserved: no state removal, key rotation, credential revocation, replacement,
or live IAM change is authorized by this source task. A later separately
approved security-owner window must supply an additive, reversible plan and
fresh independent review before any credential transition. Any bucket,
customer identity, membership, access-key, state-address, or existing resource
deletion is a stop condition.

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
rotation and revoke, appends a redacted audit row. Rotation/revoke authority,
idempotency key, requested action and audit intent are committed together; the
reconciler fences the cloud transition and terminal audit in the durable action
outbox. The row contains actor, tenant, target, action, and outcome, never an
access key or secret. User state and desired storage state commit atomically,
then the API waits for provider convergence. Tenant emergency disable reports
success only after every retained current, staged, or predecessor key is
provider-inactive. The reconciler also repairs inverse key drift, bucket-policy
drift, and group-membership drift to one exact per-user editor.

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
5. Roll out migrations 0032 and 0033 and one reconciler first. Wait until all historical
   active rows complete `rotate`, disabled rows complete `revoke`, every
   predecessor provider resource is `INACTIVE`, every replacement is `ACTIVE`
   with bounded expiry, and no owner has two active keys beyond one observed
   reconciliation cutover. Keep old PayloadCipher generations until storage
   generation usage is zero.
6. Roll out the disclosure service and gateway pods only after the database
   privilege checks pass. Verify
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

Rollback is application rollback only while keeping migrations 0032 and 0033, the narrow
database grants, deactivated predecessor keys, and split project IAM in place.
Never reactivate an old admin/public key or restore a revision that mounts cloud
credentials into the public runtime. Existing buckets and encrypted user
records are deliberately preserved across rollback.

References: [Nebius IAM roles](https://docs.nebius.com/iam/authorization/roles),
[bucket policies](https://docs.nebius.com/object-storage/buckets/bucket-policy),
and [access keys](https://docs.nebius.com/iam/service-accounts/access-keys).

## Protected-lane handoff (v10, source only)

Provider provisioning and Kubernetes attestation are separate additive phases.
The lane provisioning root consumes a signed provider-only generation and
creates the immutable lane key/taint, security group and NodeGroup. After a
Node exists, a read-only collector records its exact UID, resourceVersion, full
labels and taints. A separately signed cluster receipt records the actual
controller ServiceAccount `userInfo` identities and every live DaemonSet with
a blanket toleration. The admission generation is accepted only when those
receipts match its exact Node and critical-agent inventory.

This source is not deployable on the current branch: accepted SAI-10 ancestry
and external dependency receipts remain unresolved. Under the active
no-delete constraint, no Terraform, Helm, Kubernetes, provider, database,
credential or cleanup action is authorized.
