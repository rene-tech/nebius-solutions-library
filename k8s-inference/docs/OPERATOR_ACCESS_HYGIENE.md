# Operator access and credential migration

Operator handoff, durable-key rotation, and legacy-state retirement are gated
migrations. A named credential generation is immutable: advance by creating a
new generation, retain predecessors for reads, and never put new bytes behind
an existing generation ID.

The value-free registry at
`security/durable-credential-registry.json` is the authority for every durable
credential class. Each entry identifies its Terraform root, protected address
patterns, owner, purpose, readers, expiry enforcement, and rotation contract.
The exact current Terraform resource inventory must equal the registry; adding
a credential without registering and protecting it fails the security tests.

## Terraform apply gate

Every Terraform root invokes `scripts/secret_migration_guard.py` through the
external provider. An absent, expired, source-mismatched, registry-mismatched,
or configuration-mismatched receipt makes planning and applying fail closed.
All registered credential resources also have `prevent_destroy`, immutable
`ignore_changes = all`, and a dependency on that native gate. Thus a direct
Terraform invocation cannot update, replace, or destroy an existing generation.

`inference-stack` creates the short-lived native receipt from the exact source,
configuration, registry, and current protected-state fingerprints. Whenever
protected state exists, it first requires a write-once identity receipt. The
wrapper then inspects the complete saved plan both after plan creation and
immediately before apply. The inspector:

- checks `address` and `previous_address`, and rejects every move involving a
  protected address;
- rejects update, replace, delete, recreate, or omission of every protected
  prior-state address;
- compares exact prior-state hashes with the identity receipt; and
- binds Kubernetes Secrets to provider-observed namespace/name, UID,
  resourceVersion, and a content hash without recording Secret values.

Capture each Terraform root independently from owner-only files. Roots with
Kubernetes Secrets require a complete, freshly fetched Secret inventory:

```bash
scripts/secret_migration_guard.py capture-state \
  "$PRIVATE_STATE_JSON" "$PRIVATE_IDENTITY_RECEIPT" \
  --terraform-root workloads \
  --live-secrets "$PRIVATE_LIVE_SECRET_INVENTORY" \
  --source-commit "$(git rev-parse HEAD)"
```

The receipt is valid only for the exact registry/root and is value-free. It is
not deployment authority. Production plan/apply remains serialized through the
reviewed wrapper and deployment control plane.

## Independent credential generations

Payload AEAD, ledger HMAC, PAT pepper, route attestors, database logins,
general/scientific/website PATs, admin authentication, registry access,
Grafana, reference-data storage, scientific-artifact storage, and database
backup storage have independent generation lineages. Generation 1 preserves
the imported live identities and bytes. New generations use new provider IDs,
PAT IDs, fingerprints, and Secret names.

For payload, customer-storage cipher/name, ledger, pepper, and attestor keyrings, a later Secret contains all
retained readers while only its selected current key writes. Payload keys use
the stable `fs2-serve.nebius.ai/payload-aad/v1` contract. The payload
predecessor cannot be disabled until deployed inventories prove successful
reads of operation payloads, request-debug exchanges, and customer-storage
ciphertext, a successor write, and zero remaining old-key references. Ledger
replay, old-pepper PAT authentication, and route-attestation verification have
equivalent pre-existing-data canaries for their own independent lineages.

Customer storage uses one coordinated generation containing an AES-GCM ring
and an opaque bucket-name HMAC ring. Only the isolated storage reconciler and
disclosure service mount it. `fs2_user_storage` ciphertext key IDs and
`fs2_storage_buckets.name_key_id` form the authoritative reference inventory;
the rotation controller refuses predecessor disable until both old-generation
counts are zero. Historical unkeyed bucket names remain explicitly identified
as `legacy-unkeyed-v0` and are never misrepresented as a retireable key.
Run `fs2-serve storage-key-inventory` with the least-privilege storage database
identity to emit only these generation counts; the command cannot decrypt or
print credentials.

General, scientific, and website PAT IDs must be distinct across audiences and
all retained generations. Generation-1 expiry is mandatory. Before disabling a
predecessor, prove both old and new tokens through semantic authenticated
requests; after disabling, prove only the new token succeeds. Database and
admin rotation similarly require old/new overlap before current-write cutover.

Registry, object-storage, Grafana, and every other singleton-looking class use
the same dual-read/current-write rule. Do not replace a mounted Secret in
place. Add a new provider credential and versioned Secret, add the successor to
consumers, prove readiness, switch writes, disable the predecessor, prove zero
readers, and only then delete it.

`scripts/credential_rotation.py` enforces this provider-agnostic state machine.
Its adapter must return provider-observed identity, ownership, project,
generation, fingerprint, status, readers, and class-specific canary evidence.
The controller uses an exclusive lock and an fsync'd write-ahead journal before
provider create. `reconcile` resolves an interrupted or uncertain create by
operation ID and exact provider lineage; it never silently starts another
generation. Deletion requires explicit predecessor confirmation plus provider
`get` and complete-list absence proofs.

```bash
scripts/credential_rotation.py \
  --directory "$PRIVATE_ROTATION_DIR" \
  --provider-command "$REVIEWED_PROVIDER_ADAPTER" \
  create --credential-class payload-keyring \
  --owner-id "$OWNER_ID" --project-id "$PROJECT_ID" \
  --predecessor-id "$OLD_ID" --predecessor-generation 1 \
  --successor-fingerprint "$NEW_VALUE_SHA256"

scripts/credential_rotation.py \
  --directory "$PRIVATE_ROTATION_DIR" \
  --provider-command "$REVIEWED_PROVIDER_ADAPTER" reconcile
```

Run the remaining `prove-dual-read`, `switch-write`, `disable-old`, and
`delete-old --confirm-predecessor-id ...` transitions one at a time, preserving
their value-free receipts.

## Ordered consumer rollout

Non-secret generation metadata is hashed into affected pod templates. This
includes control-plane and model-controller workloads, migrations and
maintenance, access bootstrap jobs, database consumers, ModelExpress, DCGM,
scientific-artifact consumers, and Grafana. Secret creation precedes consumer
rollout; rollout readiness precedes current-write cutover; predecessor disable
precedes deletion. A rotation is incomplete if the declared generation and a
running pod template disagree.

Helm upgrades remain atomic and readiness-gated. Record the previous release
revision and image digests before rollout. After each class advances, compare
expected generation hashes with every affected Deployment/StatefulSet/Job and
run both the predecessor-read and successor-read canaries appropriate to that
class.

## Expiring viewer handoff

Terraform creates a versioned service account and group with exactly one
project `viewer` permit. Authentication-key issuance happens outside Terraform,
so private key material never enters configuration, plan, or state.

`scripts/operator_handoff.py issue` requires adjacent generations and derives
both predecessor and successor identity from the provider: service account,
project, group membership, role, lineage label, and generation label. A legacy
admin predecessor is accepted only when its provider labels identify the same
handoff lineage and the group has exactly that project `admin` permit. The new
viewer key must have a provider-enforced expiry equal to the requested expiry.

Issuance is lock-protected and journaled before provider create. If interruption
occurs after create, `reconcile-issuance` finds exactly the matching provider
key by lineage, generation, service account, name, expiry, and public-key
fingerprint. Zero or duplicate matches fail safely.

After delivery acknowledgment, `verify` requires:

- successful permitted inventory;
- an exhaustive SelfSubjectRulesReview containing no mutation/escalation verb
  and no Secret read/list/watch rule;
- explicit pod-create and Secret-read denials; and
- normalized live control-plane CIDRs equal to the approved egress set.

Only then may `revoke-old` accept the exact receipt-bound predecessor ID. It
rechecks both provider lineages, requires explicit confirmation, deletes that
key, and proves absence by authoritative `get` and complete provider inventory.
A different, arbitrary, or repeatedly revoked ID fails without another delete.
The receipt records the key ID, expiry, recipient acknowledgment, and provider
lineage, never the private key or bearer value.

## Control-plane CIDRs

`deployment.cluster.control_plane_allowed_cidrs` is the approved operator
egress set. Configuration accepts only canonical IPv4 `/32` or IPv6 `/128`
hosts, so default routes, broad networks, and complementary networks cannot
reconstruct public access. Live acceptance normalizes the provider-reported
set and requires exact set equality with the approved inputs.

Before applying a CIDR change, keep the current session open and prove a second
connection from every intended egress address. Rollback restores the previous
approved host set, never an empty or universal set.

## Scoped credential delivery

There is no aggregate credential export. Request one expiring PAT explicitly:

```bash
install -d -m 0700 "$PRIVATE_HANDOFF_DIR"
./inference-stack output --var-file terraform.tfvars \
  --credential-kind general-access \
  --credential-expires-in-seconds 3600 \
  --credential-file "$PRIVATE_HANDOFF_DIR/general-access.json"
```

Only `general-access` and `scientific-access` are deliverable, and only if the
server-enforced PAT expiry is no later than the requested TTL. The wrapper
writes a new mode-`0600` credential file and a separate mode-`0600`, value-free
receipt. It prints receipt metadata only. Admin bootstrap and Grafana source
credentials are excluded because metadata on a copied long-lived credential
does not enforce expiry.

## Global plaintext-state retirement

Legacy local state is retained only while it is the authority for importing
generation-1 identities and bytes. During that interval every directory is
owner-only, every file is mode `0600`, and symlinks are rejected. It must not be
copied into tickets, logs, CI artifacts, or ordinary backups.

The retirement scope must be the authoritative common owner directory, not a
single self-selected run root. `capture-global-state` recursively manifests
every file—including state, backups, binary plans, `*.plan.json`, cookies,
scoped handoffs, and unknown artifacts—and binds the real scope path, device,
inode, registry, relative path, classification, and file hash:

```bash
scripts/secret_migration_guard.py capture-global-state \
  "$AUTHORITATIVE_OWNER_SCOPE" "$ENCRYPTED_RECEIPT_DIR/artifacts.json"
```

After generation-1 import and read canaries, migrate state to encrypted,
access-logged storage and prove a no-op guarded plan. Each manifest item then
needs a structured `encrypted-rewrap` or `secure-retire` disposition containing
the authoritative provider, object ID, version ID, audit event ID, verifier,
verification time, and receipt hash. Opaque/self-asserted evidence IDs are
rejected.

```bash
scripts/secret_migration_guard.py capture-disposition \
  "$ENCRYPTED_RECEIPT_DIR/artifacts.json" \
  "$ENCRYPTED_RECEIPT_DIR/dispositions.input.json" \
  "$ENCRYPTED_RECEIPT_DIR/dispositions.json"

scripts/secret_migration_guard.py global-state \
  "$AUTHORITATIVE_OWNER_SCOPE" --retired \
  --artifact-manifest "$ENCRYPTED_RECEIPT_DIR/artifacts.json" \
  --disposition-receipt "$ENCRYPTED_RECEIPT_DIR/dispositions.json"
```

Retirement passes only when the manifest and dispositions match exactly and
the authoritative local scope contains zero files. Containment is not
retirement.

## Integration, rollback, and acceptance

Credential changes must be rebased onto accepted platform remediations before
a shared rollout. The website PAT lineage is part of the access-generation
contract. Database topology/backups, customer-storage payload encryption, and
the live deployed control-plane lineage must each come from their independently
accepted successors; an unreviewed dependency is not integration evidence.

Acceptance proves pre-existing customer-storage and request-debug ciphertext
before and after payload rotation, operation payload decryption, PAT/bootstrap
continuity and predecessor revocation, ledger replay, route-attestation
verification, exact live Secret UID/resourceVersion/content hashes, consumer
rollout/readiness, viewer-only handoff, and exact live CIDRs.

Rollback selects the last verified retained generation and application
revision. It never deletes historical generations, restores exposed plaintext
files, reactivates a disclosed credential, or discards a journal. A disclosed
value is always rotated forward and then revoked under the same evidence gates.
