---
name: scientific-ai-tenant-lifecycle
description: Onboard, inventory, retire and consolidate Scientific AI tenants, inference users, API keys, S3 workspaces and their LibreChat bindings. Use for customer access, shared/private buckets, canonical demo-data seeding, cleanup and development/QA identity reuse. Do not use for model deployment or unrelated cloud storage.
---

# Scientific AI tenant lifecycle

Use one lifecycle across the platform API, IAM/S3 and the per-user workbench.
PostgreSQL and live APIs are the state authority. An operator policy is intent,
not a second identity database or permission to prune unlisted resources.

## Resolve the workflow

This skill is versioned with the solutions-library operator tools. Read
[the workflow](../README.md) for the selected operation and inspect
[deployment intent](../deployment-policy.json) before touching this deployment.
When installed by symlink, resolve the skill's real path first. An alternate
checkout may be selected with `FS2_LIFECYCLE_ROOT`; verify its remote/status and
live API compatibility, not just its filename.

Run `scripts/fs2-users.py --help` for user/key operations and
`scripts/fs2-seed-data.py --help` for demo-data distribution. Their implementations
and tests live next to the workflow, in `k8s-inference/operations/tenant-lifecycle`.
New/update commands preview by default; `--apply` is an explicit mutation, not
new authority beyond the user's request.

## Decisions that matter

- Default to one **shared bucket per tenant**, but separate API and S3
  credentials per user. Private-user mode is explicit and selected before
  provisioning. Do not switch a provisioned tenant's mode without migration.
- Workspace names: `fs2-<tenant>-<id>` or `fs2-<tenant>-<user>-<id>` only for
  private storage. Stable hashes disambiguate normalized/truncated names.
  Reuse existing bindings; never rename a live bucket by changing its database
  name. Terraform owns platform infrastructure; the storage reconciler owns
  customer workspace and per-user IAM provisioning.
- One canonical, system-owned demo-data bucket distributes reviewed immutable
  `examples/vN/` releases. Never source it from a customer's workspace. Use the
  existing qualified pack validator/provenance and completion ledger.
- Nebius Data Transfer uses a **single iteration**, no overwrites, no deletes
  and no unmanaged-object changes. Verify source and destination checksums;
  a stopped transfer is not necessarily successful. Reuse a deterministic
  transfer identity on retries. The installed old CLI may lack v1: report the
  compatibility gap, do not silently invoke deprecated v1alpha1 or install an
  unrelated copy pipeline. See the workflow's explicit current rollout boundary.
- One LibreChat instance per user remains the rule, even for shared storage.
  A new test iteration does not require another user, tenant, bucket or client.

## Current owner instruction

Customer users are **Rene and KopraBio**. KopraBio is existing tenant `kopra` and
its existing user: preserve current keys, history and bucket bindings. Do not
create a second `koprabio` identity or rotate credentials as part of renaming.
The intended other tenant groups are **system** (development/QA), **demo**, and
**speech-to-text**. The policy marks new targets and required migrations; it does
not mean those cutovers already happened. Future new customers remain supported.

Use `system/development` and `system/qa` with `runs/<task-id>/<run-id>/` prefixes
for ordinary work. Actual isolation tests can use additional scoped identities
only when justified; record their owner, expiry and exact closeout. Keep regression
evidence, not one permanently running client per test attempt.

## Retirement and handover

1. Inventory exact tenant/user, keys, S3 identity/bucket, client and outstanding
   work. Include legacy and storage-only identities; the admin user list is not
   a complete S3 inventory. An expired API key does not revoke S3 access.
2. Drain work and export client-local chats/files before stopping that client.
   Serverless stop destroys its local disk; an S3 mount is not a MongoDB backup.
3. Use `retire-user` to disable the owner, revoke API keys and verify S3
   deactivation. Preserve shared buckets and other users. Preserve billing and
   the disabled owner/tombstone, so historical discovery does not resurrect it.
4. Permanent bucket/IAM deletion needs the exact approved targets and retention
   decision; it is not implied by a user-list cleanup. Do not bypass the existing
   storage-aware API limitations with ad hoc SQL deletion. Missing/failed steps
   remain explicitly incomplete, rather than an invented successful retirement.
5. Before a customer handover, verify actual scoped API/model access, bucket
   isolation and persistence, starter checksums and that user's client. Retain
   secret-free resource IDs and evidence; disclose credentials only through the
   private handover, not stdout, Git, prompts or tool arguments.

Do not create new tasks/workers, add audits, change quotas, replace clients or
perform mass cleanup solely because this skill was loaded. Match the requested
operation and report what was changed versus merely planned.
