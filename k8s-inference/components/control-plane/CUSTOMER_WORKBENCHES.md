# Customers and Serverless LibreChat

The **Customers** admin section joins existing inference identities, API-key
counts, actual model usage, workspace storage and Serverless LibreChat bindings.
It does not create a second identity system or a customer Kubernetes cluster.
LibreChat source and deployment live in
[`templates/hcls-librechat`](https://github.com/rene-tech/serverless-ai-cookbook/tree/main/templates/hcls-librechat).

## Ownership and persistence

- A tenant normally shares an S3 workspace; existing per-user workspaces are
  also supported. Storage credentials remain in the existing storage service.
- An instance may serve one person, or an explicitly approved group with separate
  LibreChat logins. Shared inference keys attribute usage to their inference
  owner, not to individual chat logins. Registration never splits a shared client.
- The workspace bucket stores scientific files. A **separate dedicated filesystem
  mounted at `/data`** stores MongoDB, encryption keys, logins, chats and uploads.
  Never put MongoDB on the S3 mount or share its filesystem between running clients.
- Customer-owned instances remain observational unless delegated to the operator.
  Explicit owner holds disable lifecycle operations both in the API and executor.

## Data and APIs

Migration `0037_customer_workbenches.sql` adds durable PostgreSQL customer profiles,
workbench bindings, sanitized cloud observations and lifecycle operations. Existing
identity, usage and storage records are reused. No user/bucket is deleted during
registration or upgrade.

All routes below are under the existing authenticated `/admin/api/v1` surface:

| Method | Route | Purpose |
|---|---|---|
| GET | `/customers` | Existing customers, usage and resource bindings |
| GET | `/customers/{tenant_id}` | One customer's users, models, buckets and clients |
| PUT | `/customers/{tenant_id}/profile` | Display name and operational purpose |
| GET | `/workbench-inventory` | Cloud endpoint/bucket inventory, including unassigned resources |
| POST | `/workbench-inventory/refresh` | Refresh observations; no cloud resources changed |
| POST | `/workbenches` | Adopt an exact existing endpoint; metadata only |
| POST | `/workbenches/{id}/operations` | Queue a durable, idempotent runtime upgrade |

Usage uses accepted model operations in the selected time window, excluding polls
and artifact uploads. It is not the model access allowlist. Existing **Users**
screens continue to manage keys and grants. Observations refresh every two minutes;
data older than ten minutes is explicitly stale, not invented as zero. Legacy
identities are hidden by purpose in the normal overview, not silently deleted.

Upgrade request example (IDs and release supplied from the customer detail):

```json
{
  "kind": "upgrade",
  "target_release": "qualified-release-name",
  "expected_revision": 1,
  "idempotency_key": "unique-customer-change-id",
  "confirm_interruption": true
}
```

Only `upgrade` is implemented. Backup, restore and retire are not enabled admin
commands in this release; unsupported command kinds are rejected explicitly.

## Enable qualified releases

Use these values in the existing control-plane Helm release, after applying the
schema contract and qualifying the exact client digest:

```yaml
workbenches:
  executorEnabled: true
  releases:
    qualified-release-name: registry.example/client@sha256:<64-hex-digest>
  protectedEndpoints:
    - aiendpoint-owner-held
```

Defaults disable execution and provide no releases. The executor uses the existing
customer-storage service account credentials; it additionally needs Serverless
endpoint read/create/stop permissions and access to the retained mounts/secrets.
It performs the provider's actual create dry run before interruption. This checks
the request and permissions, **not future capacity, image pulls or readiness**.

The worker holds a per-operation PostgreSQL advisory lock. A deterministic name,
persisted source/target and stage markers allow interrupted operations to resume.
The native Nebius SDK protobuf copy constructor preserves secret references;
Python `deepcopy` does not safely copy those SDK messages.

## Replacement and recovery

The provider requires an endpoint replacement for image changes. Finish active
chat turns, stop the predecessor, create its successor with the same mounts and
secret references, snapshot `/data` before MongoDB startup, then wait for the public
health endpoint. The binding and URL update atomically. The stopped predecessor
record is retained; this is **not zero downtime**, and its tunnel URL changes.

On failure, stop any partial successor and launch the old image on the same state
filesystem, restoring the pre-upgrade snapshot if it was made. Snapshot restoration
is one-shot so future restarts cannot rewind newly created chats. No automatic
retry submits scientific jobs; their backend operation IDs remain durable.

Filesystem snapshots protect the upgrade, not deletion/loss of the filesystem.
Independent off-filesystem backup and retention are separate operational work.
Do not call this cross-region disaster recovery.

## Existing clients and cleanup

Clients with only local disk remain visible but cannot be upgraded. Export their
database, encryption keys and uploads while the original remains accessible, then
prove a full restore before their first stop. A stopped endpoint record is not a
backup of its former local disk. The UI labels these **migration required**.

Unassigned and stopped resources remain visible in inventory for reconciliation.
Do not infer that unassigned means unused. Establish ownership and a recoverable
state copy before cleanup; exact deletion remains a separate explicit operation.
The deployment-specific release receipt records owner holds and retained resources.

For customer self-service use the same client template, not a separate fork of its
agent. The launch link contains no secrets. Registry pull access, a project/subnet,
customer keys and the S3 mount must be configured; private images do not become
public by adding a button. Platform credentials must never appear in launch URLs.
