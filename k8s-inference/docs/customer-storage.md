# Customer data buckets

Customer storage is independent of model weights, snapshots and the existing
scientific result store. This first increment provisions S3 workspaces and
credentials; it does **not** connect LibreChat, migrate old artifacts, or lift
model-specific input limits.

## Configuration

New facade deployments enable customer buckets by default. In `terraform.tfvars`,
under the existing `deployment.storage` object:

```hcl
customer_buckets = {
  enabled          = true
  default_mode     = "tenant"
  quota_bytes      = 5000000000
  excluded_tenants = ["stockholm"]
}
```

`tenant` gives all users in a tenant one shared bucket. `user` gives every user
an independent bucket. The quota applies **per bucket**: 5 GB per tenant in
tenant mode, or 5 GB per user in user mode. GB means 1,000,000,000 bytes.
Each user gets a distinct S3 access key/secret pair, including shared-bucket
users. Multiple inference API keys belonging to the same user share that S3
identity. Customers do not receive project-wide IAM permissions.

Terraform creates the internal provisioner identity and its Kubernetes Secret.
It needs project-scoped `admin` to create IAM groups, members and access keys;
`editor` cannot perform those operations. The provisioner's RSA key is sensitive
Terraform state: protect and back up that state. Individual customer S3 secrets
are instead encrypted in PostgreSQL with the existing payload key ring and AAD
bound to `(tenant_id, principal_id)`. Retain old key-ring entries while any
stored customer credential still uses them; queued-payload TTL alone is not
sufficient to retire these encryption keys.

The controller reconciles configured users and legacy owners with a current
inference API key every 60 seconds. It does not create buckets for historical
acceptance-run identities that have no current key. Exclusions override tenant
policies; Stockholm is excluded in the retained deployment, not hard-coded
into the reusable application. Provisioning is off the inference request path.
Cloud failures leave storage pending and are retried independently of inference.

## User and operator APIs

Existing platform bearer authentication selects tenant/user server-side:

- `GET /v1/storage`: bucket, endpoint, region, quota, state and access key ID.
- `POST /v1/storage/credentials`: connection details including S3 secret.
- `GET /admin/api/v1/users/{user_id}/storage`: operator view.
- `POST /admin/api/v1/users/{user_id}/storage/credentials`: admin-only disclosure.
- `GET /admin/api/v1/tenants/{tenant_id}/storage`: effective policy.
- `PUT /admin/api/v1/tenants/{tenant_id}/storage`: configure mode/quota.

Configure a tenant's mode **before** adding its first user:

```json
{"mode":"user","quota_bytes":5000000000}
```

Changing mode after provisioning returns a conflict, because switching shared
and private storage requires a deliberate data/access migration. Increasing
quota takes effect on the next reconciliation. `disabled` can exclude an
unprovisioned tenant; it is not a delete operation.

User details in the admin console show storage information and an explicit
“Show S3 credentials” action for admins. Credential responses use `no-store` and
are excluded from HTTP body-debug capture. Disabling a user prevents credential
disclosure immediately and deactivates its S3 key on the next reconciliation;
reenabling reactivates that same identity. Revoking an individual inference key
does not revoke the user's independent S3 credentials.

Use the disclosed `endpoint`, `region`, `bucket_name`, `access_key_id`, and
`secret_access_key` with standard S3 clients. Use an explicit bucket URL:
customers cannot enumerate every bucket in the project. No credential is sent
to a model or put into an MCP argument by this increment.

## Retention and quota semantics

Buckets are dynamically managed, not one Terraform resource per customer.
Reconciliation never deletes customer data; cluster teardown does not delete
these buckets or their user IAM identities. Preserve the database, encryption
key ring and Terraform state for recovery/adoption.

Nebius enforces `max_size_bytes` on the bucket, including direct S3 uploads.
Its documented limit can be exceeded slightly by fast concurrent writes; this
is not a transactional zero-overshoot billing limit. Quota changes are not cloud
quota increases and do not enable automatic charging. Usage billing, resumable
platform Files APIs, and LibreChat integration are separate increments.

References: [bucket limits](https://docs.nebius.com/cli/reference/storage/bucket/create),
[bucket policies](https://docs.nebius.com/object-storage/buckets/bucket-policy),
[IAM roles](https://docs.nebius.com/iam/authorization/roles).
