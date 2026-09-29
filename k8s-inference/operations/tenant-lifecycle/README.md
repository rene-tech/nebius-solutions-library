# Scientific AI tenant and workspace lifecycle

This is the operator workflow and CLI for customer onboarding, credential handover,
access retirement and demo-data seeding. It reuses the platform's existing APIs
and storage reconciler. It does not create a second identity store, directly edit
PostgreSQL, create model copies, raise cloud limits or deploy a backend image.

## The simple model

- A **tenant** is a customer or an internal workstream. It owns model grants and
  usage attribution; it does not own private copies of shared models.
- A **user** is an inference owner inside that tenant. Each has distinct API and
  S3 credentials, even when users share storage.
- Default: **one shared workspace bucket per tenant**. Optional: **one private
  bucket per user**, selected before the first user is provisioned.
- Each user keeps their **own LibreChat instance**, including users sharing a
  tenant bucket. Do not merge their chat databases or copy customer keys between
  users unless the owner explicitly requests a shared tenant deployment.
  **Lynx is that explicit exception:** one instance, two separate local logins,
  one shared API key with eight concurrent operations total. Preserve
  `/workspace` bucket bindings and every login's MongoDB state across upgrades.
- One **system-owned demo-data source bucket** contains immutable, qualified
  `examples/vN/` packs. It is platform data, not another customer tenant. Customers
  get their own copies, never write access to the canonical source.
- Model weights, reference databases, snapshots, scientific result stores and
  speech-service artifacts are separate infrastructure stores. A five-tenant
  policy does not mean the entire project should contain only five buckets.

New workspace names are `fs2-<tenant>-<id>` for shared storage and
`fs2-<tenant>-<user>-<id>` for private storage. The backend already implements
stable, collision-resistant IDs and S3-compatible truncation. The `bucket-name`
command previews that algorithm; it must not be used to rename existing resources.

## Current deployment intent

The owner confirmed the following on September 29, 2026:

| Name | Stable/target tenant ID | Purpose | Intended users |
| --- | --- | --- | --- |
| Rene | `rene` — existing | Customer/internal | `rene` |
| KopraBio | `kopra` — existing | Customer | Existing `kopra` user |
| Lynx | `lynx` — existing | Molecular-dynamics customer | Shared inference principal `lynx`; one LibreChat instance with separate Gyorgy/Daniel logins |
| System | `system` — target | Development and QA | `development`, `qa` |
| Demo | `demo` — target | Showcases | `demo` |
| Speech to text | `speech-to-text` — target | Current speech workstream | `speech` |

`deployment-policy.json` records this intent and aliases. It is not a background
pruner. Future actual customers can be added deliberately; tests should not invent
new customer tenants. **KopraBio is existing Kopra: preserve its keys, tenant ID,
history and bucket.** The CLI alias resolves to `kopra`; it does not change server
identity or display metadata. Existing legacy bucket names remain valid until an
explicit data/client migration is approved and tested.

System QA stores each run in `runs/<task-id>/<run-id>/`. Reuse its small set of
users, workspaces and clients. If an isolation test genuinely requires a private
user or a separate client, record its owner, purpose, expiry and closeout in that
task. Retain evidence as objects/files, not a perpetually running VM per attempt.
Do not configure broad bucket expiration: removing a prefix also needs an exact
scope, retained-evidence decision and object-version check.

## Normal operator workflow

Use Python 3.10+ for `lifecycle.py`; the seed helper also uses the qualified
control-plane environment's `boto3`, `botocore` and `fs2_serve.starter_packs`.
No secret belongs in command arguments, the policy file or Git.

Set `FS2_ADMIN_URL` to the exact HTTPS origin, `FS2_ADMIN_TOKEN_FILE` to an existing
private bootstrap-token file, and optionally `FS2_TENANT_POLICY` to this policy.
The CLI exchanges the bootstrap token for a short-lived operator session, keeps
it in memory and logs out afterward. It does not disable TLS verification or
follow redirects. `--ca-file` supports an explicitly trusted private CA.

### Create or reuse a user

1. Select the tenant, stable user ID, shared/private mode, model grants and quota.
   Default quota is 5 GB **per bucket**, not per API key. Do not change an existing
   tenant's mode to accommodate a new user: private/shared transitions migrate
   data and access, and the backend deliberately rejects an in-place switch.
2. Preview, then apply the exact user request. Existing enabled users are reused;
   their keys and storage are not reset. Disabled users are not silently restored.
3. Wait for storage `ready`, then independently verify the selected starter pack.
4. Issue one named API key; collect the user's S3 credentials separately. Deliver
   both through the established private handover. API key revocation and S3 access
   retirement are independent.
5. Deploy or reuse that user's client, mount the API-reported bucket at
   `/workspace`, and check user-visible read/write and authorized model access.

```bash
python3 lifecycle.py --policy deployment-policy.json create-user \
  --tenant system --user qa --kind service --display-name 'QA'

# Repeat the reviewed command with --apply; this can provision a bucket and IAM.
python3 lifecycle.py --policy deployment-policy.json create-user \
  --tenant system --user qa --kind service --display-name 'QA' --apply

# Explicit private storage for a future tenant, before its first user exists:
python3 lifecycle.py create-user --tenant research-lab --user alice --mode user

python3 lifecycle.py --policy deployment-policy.json inventory
python3 lifecycle.py --policy deployment-policy.json inventory --include-disabled
```

New key specification (example; choose the actual authorized model set and limits):

```json
{
  "name": "workbench",
  "models": ["boltz2", "openfold2"],
  "scopes": ["inference.invoke", "mcp.invoke", "catalog.read"],
  "max_concurrency": 5
}
```

```bash
python3 lifecycle.py issue-key --tenant research-lab --user alice \
  --spec key-request.json --output /private/handover/alice-api-key.json --apply
python3 lifecycle.py storage-credentials --tenant research-lab --user alice \
  --output /private/handover/alice-storage.json --apply
```

The output directory must already exist and be private. Secret output files are
created with mode 0600 and never overwritten; secret values are never printed.
A repeated key name is rejected rather than silently issuing another key. An
ambiguous network/write failure requires reconciliation, not a new key name.

### Retire a user (the normal “delete user” operation)

1. Resolve the exact tenant/user and its API keys, S3 identity, bucket mode and
   client. Check all outstanding work, including operations outside the admin
   usage window. Decide how to retain results and billing history.
2. Export client-local MongoDB/chats and referenced files before any client stop.
   Serverless stop destroys the VM/local disk; the bucket mount is not a complete
   chat backup. Do not stop another user's instance.
3. Preview `retire-user`, then apply after work is drained. It disables the owner,
   revokes inference keys, waits for S3 deactivation, and verifies key revocation.
   A timeout/failure is an incomplete retirement that must be reconciled.
4. Separately retire the exact exported client. Shared buckets remain for other
   tenant users; private data remain until their retention/deletion decision.

```bash
python3 lifecycle.py retire-user --tenant system --user qa
python3 lifecycle.py retire-user --tenant system --user qa --drained --apply
```

Retirement is **not permanent data deletion** and does not erase the database
identity/tombstone. Deleting rows first can resurrect owners through historical
API-key discovery or strand S3 credentials. The current backend has no general
user-purge endpoint, and its event-tenant DELETE refuses tenants owning storage.
Do not work around this with direct SQL or guessed `kubectl delete` commands.

There are legacy S3-only identities invisible to normal admin user discovery.
Include the read-only storage/IAM inventory in a consolidation. They require a
reviewed disabled-owner adoption or a storage-aware backend retirement path;
`retire-user` intentionally fails if it cannot resolve the exact owner. Do not
call such a cohort fully retired after processing only visible user rows.

Permanent purge is a separate, exact-target operation after retention approval:
verify all current/old object versions and multipart uploads, reconcile mounts,
disable provisioning, retire IAM/access records, preserve accounting, then delete
only the approved resources. This CLI does **not** contain a wildcard purge.

## Canonical demo data and Nebius Data Transfer

One dedicated system-owned bucket named `fs2-system-<stable-id>` (purpose label
`demo-data-source`) is the distribution source, separate from the system QA
workspace. Terraform/foundation should own that platform bucket and scoped
publisher/reader identity. The live bucket ID is a deployment setting, not a
hard-coded guess. Do not copy a customer bucket or historical transcripts into it.

Reuse the existing qualified starter pack, including source licenses and
manifests. Current verified release v3 is 498 objects / 53,900,577 bytes, with
microscopy, molecular dynamics, approved speech, structures and other examples.
Its manifest SHA-256 is
`c8afd07ca1b6734c690839ba6d3eb4b461b65380852bcf705a863e7bd07a7709`.
Large private results, model weights and every historical QA output are not
initial demo data. New content gets a new qualified immutable version.

`seed_data.py publish` uses the existing `StarterPack` and
`BucketPackInstaller`, including conditional create-only writes, full checksums,
quota headroom and provenance validation. `transfer` uses the managed service,
then verifies every expected object. `verify` is read-only. The credential files
use the platform storage-disclosure shape: `bucket_name`, `endpoint`, `region`,
`access_key_id`, `secret_access_key`.

```bash
# Run in the qualified control-plane environment, or provide --platform-source
# pointing to its components/control-plane/src directory.
python3 seed_data.py publish --pack-dir /qualified/pack \
  --manifest-sha256 <qualified-sha256> --credentials /private/source-writer.json

# After reviewing the publish plan, repeat it with --apply.
python3 seed_data.py transfer --pack-dir /qualified/pack \
  --manifest-sha256 <qualified-sha256> \
  --source-credentials /private/source-reader.json \
  --credentials /private/new-user-storage.json \
  --project <destination-project> --profile <explicit-profile>
```

Transfer settings are one iteration, identical `examples/vN/` source/destination
prefixes, `overwrite_strategy: NEVER`, destination deletes disabled, and
`touch_unmanaged: false`. The transfer name is deterministic per source,
destination and manifest; retries observe the same transfer. `STOPPED` alone is
not success: the iteration must be `COMPLETED` and all checksums must pass.
The helper passes credentials in an ephemeral 0600 file, never CLI arguments or
retained plans. Its result contains transfer identity and verification evidence.
See [Nebius launching transfers](https://docs.nebius.com/object-storage/transfer/launch)
and [v1 transfer API](https://raw.githubusercontent.com/nebius/api/main/nebius/storage/v1/transfer.proto).

**Compatibility discovered September 29:** the installed Nebius CLI 0.12.206
only exposes `storage v1alpha1 transfer`; the current documented command is
`storage transfer` (v1). The helper refuses the legacy command and accepts a
current isolated binary through `--nebius-bin`. Do not globally upgrade a shared
CLI or silently fall back to the deprecated API. Managed transfer acceptance
requires that compatible binary and real source/destination qualification.

**Current rollout boundary:** the source bucket has not been created or switched
into the live controller by this change. Automatic new-bucket seeding currently
still uses the deployed immutable OCI starter pack and PostgreSQL completion
ledger. Keep it working. Before replacing that transport, use the existing
ledger keyed by `(bucket_id, pack_version, manifest_sha256)` and its one-writer
coordination; record transfer IDs and verify completion durably. A CLI copy
receipt is not a new database completion record. Do not run two competing
seeders, clear completion rows, or restore examples a customer intentionally
deleted from an already-completed version.

## Consolidating the existing deployment

Generate a non-mutating plan from the sanitized cloud/database inventory:

```bash
python3 lifecycle.py --policy deployment-policy.json consolidation-plan \
  --snapshot /private/inventory.json
```

Preserve Rene and KopraBio unchanged. Introduce/reuse system users; export the
existing demo and speech clients and migrate them into their target shared
tenants before retiring their source identities. Retire obsolete simulated
scientists, robotics canaries, event rehearsals and acceptance accounts after
their task/data closeout. Legacy Terraform/bootstrap keys may be infrastructure
dependencies; replace/retire them only after dependency checks. Do not infer
"unused" from an expired inference key, empty-looking bucket or stopped client.

The September 29 inventory and private details are at
`/home/tux/secure-handoff/fs2-bucket-user-inventory-20260929/`.
That snapshot had 71 buckets, 33 configured users, 8 extra admin-discovered
owners, and 9 storage-only identities. Reinspect before execution; the policy
does not auto-delete anything outside the five intended tenant groups.

## Validation

The [Lynx onboarding](LYNX_ONBOARDING_20260929.md) records the explicitly requested
shared-client exception, model grants, eight-operation cohort and private handover.

The [September 29 LibreChat cleanup](LIBRECHAT_CLEANUP_20260929.md) records the
owner-requested running-instance consolidation, exact retained clients and
private export/release evidence. It does not constitute user or bucket deletion.

See [the exact verification and rollout status](VALIDATION.md). The installed
skill is a symlink to `skill/` in this versioned directory, so its launchers use
these implementations rather than independent copies. In another checkout,
install that skill directory or set `FS2_LIFECYCLE_ROOT` to this directory.

```bash
python3 -m unittest -v test_lifecycle.py
```

These are stateful contract/behavior tests, not customer-cloud acceptance. They
cover shared/private buckets, stable aliases/names, repeat provisioning, no
implicit restoration/migration, dry-run behavior, secret handover, retirement
and preservation of another shared-bucket user, partial inventories, transfer
binding and state, and checksum failure. Live create/retire/managed-copy tests
must reuse the system test identities and record IAM/bucket/client outcomes;
do not create another collection of disposable tenants to test this workflow.
