# Customer storage — 2026-09-16 acceptance and resume

## Outcome: partially deployed; cloud-quota blocked

The requested shared-tenant/default and optional private-user storage modes are
implemented. Customer S3 keys are encrypted with the existing user-associated
PostgreSQL records, and explicitly retrievable in the admin Users page or the
authenticated storage API. Default allowance is 5,000,000,000 bytes per bucket.
LibreChat, model input adapters, billing and old-result migration are unchanged.

The Nebius tenant has exhausted `iam.storageaccesspolicy.count`, limit **10**.
The failed bucket-create operation `opstoragebucket-e00kvcfj4q763m784j` reported
`RESOURCE_EXHAUSTED` for tenant `tenant-e00f3wdfzwfjgbcyfv`. **No cloud limits
were raised.** No unrelated policies were deleted. Approval has been requested
in the conversation and the user's requested Slack DM.

| Platform tenant | User(s) | State |
| --- | --- | --- |
| rene | rene | Ready; S3 upload/download and public API qualified |
| fs2-h100 | fs2-h100-operator-handover | Ready; S3 access qualified |
| kopra | kopra | Pending; quota blocked |
| rene-tech | dual-acceptance-h100-operator | Pending; quota blocked |
| robotics | timmothy | Pending; quota blocked |
| tenant-academic | Two Terraform bootstrap owners | Pending; quota blocked |
| tenant-e00f3wdfzwfjgbcyfv | terraform-bootstrap-client | Pending; quota blocked |
| stockholm | All 20 teams | Intentionally disabled; no buckets/credentials |

Failed provider creates may briefly appear as `CREATING` and then roll back.
Those are not ready workspaces. After quota is available, named resources are
adopted/retried and only successful completed operations are stored as ready.

## Evidence

- 196 backend, real disposable-PostgreSQL, packaging, migration, telemetry/debug
  and Helm rendering tests passed. Strict mypy and Ruff passed for storage code.
- Eight admin Users/credential-panel component tests passed; production UI build
  passed. Full live browser acceptance has **not** been performed.
- Real dedicated provisioner created/adopted a bucket and a per-user S3 key.
  Repeating credential creation recovered the same key/secret.
- Real S3 write/read/delete passed for Rene.
- A **17 MiB multipart upload**, download and SHA-256 comparison passed. The
  task-owned object was deleted afterward.
- Cross-tenant S3 list access was denied in both directions between the two
  ready tenants; each could list its own bucket.
- Two temporary platform API keys for Rene both returned the same user's S3
  identity through `/v1/storage` and `/v1/storage/credentials`. Both test API keys
  were revoked afterward. No customer key was rotated or revoked.
- Normal user metadata contains no S3 secret. Explicit disclosure returns
  `Cache-Control: no-store`. Inference keys cannot access admin storage APIs.
- All 20 Stockholm users report disabled storage with no bucket.
- Terraform applied the six-resource internal provisioner module; a subsequent
  plan returned **no changes**. No GPU/node resources or cloud quotas changed.

Blocked, not passed: full existing-tenant provisioning, live shared-bucket access
by two different users, live private-user bucket creation/isolation, live key
deactivation/re-enable, and live overflow rejection at the bucket byte limit.
Unit/PostgreSQL coverage is not a substitute for those live checks.

## Release and retained infrastructure

- Helm revision **123** completed successfully. Final rollout: 3/3 gateway,
  2/2 admin, 2/2 controller and 15/15 GPU-observer pods ready. Quota failures now
  identify `RESOURCE_EXHAUSTED` and operation IDs in runtime logs and back off.
- Target-specific values are retained in `retained-release.values.yaml`.
- Project `project-e00rene`, region `eu-north1`, cluster
  `mk8scluster-e00j5z9te7x5dd9g6a`, namespace `fs2-system`.
- Backend source: `6dbf648e8165c9eb434300ad2b310ac2d136ca92`.
- Backend image: `cr.eu-north1.nebius.cloud/e00akg9ndpx77eaexh/fs2-platform/fs2-serve-control-plane@sha256:da7f5076307476b636283752c41cc2ffd96bdefa951d12231ed13434a4709422`.
- Admin source: `8f84183fbf3c46993ae225ba242b5c96fdc0c185`.
- Admin image: `cr.eu-north1.nebius.cloud/e00akg9ndpx77eaexh/fs2-platform/fs2-serve-admin-console@sha256:4320eb8b1dbf9d0fce3122d6418001aac7e00f7f2ec5577c697455ab277949ce`.
- Images were built from an isolated committed snapshot of the deployed source;
  unrelated uncommitted Cosmos/readiness/semantic runtime/UI work was not shipped.
- Schema advanced from 29 to 31 migrations. Additive migration 0030 was included
  to preserve the canonical migration sequence; the old telemetry reader was
  made tolerant of extra columns. Migration 0031 adds customer storage tables.
  Do not blindly roll back to an image requiring exactly 29 migrations.
- The system CPU node lacked rollout headroom. Existing general-CPU capacity was
  enabled for the control plane/UI/controller and CPU hook jobs using Helm
  tolerations. No taints were removed and no new nodes were created. CPU hook
  templates now inherit the chart's top-level tolerations. GPU workloads unchanged.
- Separate provisioner Terraform state is retained privately at
  `/home/tux/secure-handoff/scientific-ai-customer-storage-20260916/provisioner`.
  It contains the internal RSA private key: never commit or disclose the state.
  The matching Kubernetes Secret is `fs2-customer-storage-provisioner`.
- Before enabling the new module in this cluster's canonical workloads state,
  **move/adopt** the existing six provisioner resources into
  `module.customer_storage_provisioner[0]` and import the existing Secret. Do not
  create a duplicate identity or discard the TLS-key state. Fresh deployments
  use the integrated workloads module directly.

Unrelated observation, not changed here: the maintenance command fails deleting
old operations referenced by `fs2_scientific_stage_attempts`. It was observed
during rollout and needs a separate retention fix; no scientific history was
deleted as part of the bucket work.

## Resume after explicit quota approval / capacity becomes available

1. Resolve the IAM policy quota through the approved path; do not silently
   remove unrelated policies. The controller resumes new provisioning within
   five minutes after a quota backoff expires.
2. Run `verify.py` with the control-plane venv, explicit kubeconfig/context and
   `--origin https://89.169.99.188`. It verifies all existing users, shared tenant
   access, multipart transfer, and two private-user fixtures. It disables fixture
   users and revokes temporary platform keys; empty fixture buckets are retained
   for repeatability. Read its documented mutation scope before execution.
3. `verify_existing.py` is the smaller test for already-ready Rene storage. It
   passed, but deliberately does not claim full provisioning acceptance.
4. Complete a live browser check under Admin → Users → user → Data storage,
   including explicit Show/Hide credentials, plus quota overflow acceptance on a
   disposable small-quota fixture. Do not lower any customer's quota for testing.
5. Document final per-user bucket inventory and test outcomes before claiming
   completion. Keep Stockholm excluded; do not touch LibreChat or BioIR work.
