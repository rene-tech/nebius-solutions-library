# Operator credential custody: additive migration procedure

This is the canonical operator procedure. It permits read-only inspection and
additive immutable generations only. It does not authorize deletion, disable,
revocation, replacement, force, history rewrite, state removal, credential
creation, live rollout, or cleanup. Historical generation 1, evidence, customer
data, and predecessor credentials remain retained.

## Source status (2026-09-17)

This document is a fail-closed target procedure, not readiness or deployment
evidence. No test, Terraform initialization, state migration, provider read,
cluster read, credential action, or deployment was performed for this source
successor. The checked-in source trust policy has
`deployment_authorized=false`; no independent anchor/witness keys, genesis
checkpoint, production endpoint, or consistency proof has been accepted. The
authority therefore cannot admit evidence. SAI-06 has static SOURCE GO only;
SAI-08 and SAI-09 remain without an accepted source successor, and none of the
three is integration/live accepted.

## Production authority boundary

`scripts/credential_authority_service.py` is the only production evidence
authority. It runs as root on a fixed Unix socket, authenticates clients with
kernel peer credentials, and loads provider commands only from a root-owned
mode-0600 configuration. Every executable or absolute command file is pinned by
SHA-256. The proposed response contract includes a detached Ed25519 producer
signature and an independent mTLS evidence-log anchor. This repository does
not yet contain or operate that independent service, accepted source-owned
pins, a prior-head/Merkle consistency verifier, or independent witnesses. A
local configuration cannot authorize them: the source trust policy blocks the
client until those identities are committed and independently accepted. The service exposes read
operations only and writes one lifetime, crash-recoverable, write-once,
hash-chained audit stream. The client cannot select a provider,
profile, project, kubeconfig, state root, or evidence file.

The authority is a mandatory deployment prerequisite. If its configuration,
socket ownership, complete artifact scope, provider adapters, or 19 active consumer
probes have not been independently accepted, migration and rollout remain
blocked. Do not replace it with JSON fixtures or an operator-selected command.

## Unbypassable Terraform gate

`security/durable-credential-registry.json` records the currently known durable
Terraform addresses. It is not a claim that live provider inventory is
complete. The authority rejects any cluster Secret or project IAM object that
is neither state-bound nor source-registered; the empty exception registry
cannot be populated by caller input. The guard requires the plan's exact
embedded configuration to equal that inventory and requires every address to
match at least one credential class (the imported combined storage Secret is
intentionally protected by both independent storage lineages). It rejects unregistered credential-shaped
resources, `previous_address`, moved blocks, missing prior-state addresses,
fixed-generation creates, and any update/replace/delete action. A state `mv` or
`rm` followed by an escaped delete or fixed-ID create therefore fails closed.

The production authority does not accept a state path. It runs `terraform state
pull` from each fixed, root-owned configuration and exact workspace, refuses to
start unless that private deployment configuration declares a `remote` or `s3`
backend, and binds the returned lineage, serial, and canonical state digest.
All six deployable roots now declare a partial `s3` backend and the platform
wrapper accepts only fixed root-owned backend configuration files. Existing
local state still contains generation-1 material and has not been migrated;
admission remains blocked until a separately reviewed, non-destructive,
encrypted and access-logged backend migration is authorized. Planning
and saved-plan state are sent to the guard in
one in-memory JSON envelope. The wrapper does not create then remove temporary
state or Secret files. A saved-plan receipt binds:

- exact backend lineage, serial, Terraform version, and raw state hash;
- exact plan bytes, canonical plan JSON, configuration, registry, source commit,
  and expiry;
- every existing protected Secret's authority-observed namespace/name, UID,
  resourceVersion, immutable flag, generation, and exact decoded-data-map
  content commitment; and
- every planned immutable Secret's name, generation, class, revision, and exact
  content commitment.

Every receipt creates a new permanent
`terraform_data.credential_apply_gate_generation` instance. All prior gate
instances remain protected by `prevent_destroy`. The new instance revalidates
the receipt at apply time using the fixed Terraform executable and production
authority. A direct plan lacks the receipt; a direct saved-plan apply cannot
skip the new provisioner; an expired or raced receipt fails.

Capture a value-free identity only through the authority:

```bash
scripts/secret_migration_guard.py capture-state \
  "$PRIVATE_STATE_JSON" "$PRIVATE_IDENTITY_RECEIPT" \
  --terraform-root workloads \
  --source-commit "$(git rev-parse HEAD)"
```

## Immutable generation and consumer sequence

Payload, ledger, PAT pepper, route attestor, customer-storage cipher,
customer-storage name, database, PAT audiences, admin, registry, Grafana,
reference-data, scientific-artifact, PostgreSQL backup, and operator-handoff
lineages remain separate. Never overwrite a fixed generation or reuse an ID or
fingerprint across a retained generation or PAT audience.

The only allowed source sequence is:

1. Import and bind every existing generation and live Secret.
2. Plan only additive immutable versioned Secrets in `secret-stage`.
3. After a separately authorized apply, capture new authority bindings.
4. Capture `predecessor-ready` evidence from all 19 integrated class adapters.
5. Plan the `dual-read` consumer rollout, binding the exact readiness receipt.
6. After independently observed readiness, capture `dual-read-ready` evidence.
7. Plan `current-write` with that later receipt.
8. Capture `current-write-ready` evidence and retain all predecessors.

```bash
scripts/secret_migration_guard.py capture-consumer-readiness \
  "$PRIVATE_IDENTITY_RECEIPT" "$PRIVATE_READINESS_RECEIPT" \
  --phase predecessor-ready
```

The readiness receipt must cover exactly the 19 active contracts in
`security/credential-consumer-contracts.json`. The two PostgreSQL backup
contracts remain recorded but pending SAI-06 integration. Each result must name its reviewed
adapter, authority, consumers, readiness probe, exact live-binding hash, unique
provider evidence ID, and observation time. A generic ready flag cannot
authorize a rollout.

The request now sends the complete value-free live Secret bindings
(namespace/name/UID/resourceVersion/content commitment/generation/immutability)
and their canonical hash to each adapter. No production class adapter is
accepted yet, so readiness capture intentionally fails. Once accepted, exact binding, authority evidence, receipt hash, and rollout step are hashed
into the affected Pod templates alongside per-class generation metadata. Helm
is atomic and waits for Jobs and workload readiness. Database, admin, PAT,
cryptographic, registry, ModelExpress, DCGM, Grafana, object-storage, and backup
consumers must converge before the next step. There is no disable, revoke, or
delete transition in `scripts/credential_rotation.py`; its journals are
append-only hash chains and it only adopts a separately staged successor,
reconciles, and proves dual-read/current-write behavior.

## Customer-storage compatibility

`CustomerStorageCrypto` is the sole runtime crypto boundary. Encrypt, decrypt,
migration, and rollback all derive AAD through
`PayloadCipher.customer_storage_aad(tenant_id, principal_id)`. Cipher and name
keys have independent keyrings and active generations. Old and new generation
decrypt, rollback decrypt, and tenant/principal cross-binding failures are
required tests.

The source now wires opt-in same-principal append/disclosure request paths and
conditional keyring mounts through this boundary. The chart defaults the
feature off and Terraform refuses enablement without post-v1 key generations
and an exact current-write receipt. These changes were not dynamically tested,
are not SAI-08 integration evidence, and are not deployable until an accepted
SAI-08 successor is semantically reconciled. No payload or storage predecessor may be removed
until deployed customer-storage and request-debug ciphertext inventories are
readable and usage-zero evidence exists; under this procedure they are retained
even after that proof.

## Viewer handoff and CIDRs

A handoff key must have provider-enforced expiry. This repository's handoff tool
cannot issue a key or create a provider profile: it only acknowledges an
externally issued receipt and asks the fixed authority to derive the key's
project, service account, group, sole membership, exact viewer permit, lineage,
generation, public-key fingerprint, and expiry from complete provider
inventory. Delivery has a recipient, key ID, expiry, and write-once receipt.
Issuance stays blocked until a separately reviewed externally journaled service
exists. Authority reads additionally require one non-root automation client UID
and a provider-observed service account, credential, group, membership, and sole
project-viewer permit whose enforceable expiry is no more than 24 hours away.
No such production identity was issued or inspected here.

Verification permits inventory and comprehensively rejects mutation,
escalation, impersonation, exec/attach/port-forward, and Secret
get/list/watch/write. The old cluster-admin handoff remains recorded and
retained because revocation is irreversible; acceptance cannot close until a
future authorized process performs and proves that revocation.

Control-plane allowlists accept canonical IPv4 `/32` and IPv6 `/128` hosts
only. Default routes, broad networks, and complementary `/1` pairs are rejected.
Live acceptance normalizes the provider result and requires exact equality to
the approved egress set.

## Scoped credential delivery

There is no aggregate credential file. `inference-stack output` requires one
explicit `general-access` or `scientific-access` kind, a new destination in an
owner-only directory, and a server-enforced PAT expiry no later than the
requested TTL. It emits a mode-0600 credential and a separate value-free
receipt without printing the token. Admin bootstrap and Grafana credentials are
not exportable.

## Authoritative legacy-artifact inventory

Local scans prove containment only. The root authority owns the global scope,
which must include all configured platform state families and secure-handoff
locations. It inventories Terraform state/backups, binary plans, `*.plan.json`,
cookies, scoped handoffs, and unknown sensitive artifacts. Every entry contains
a stable ID, path and content commitments, classification, owner, purpose,
bounded expiry, readers, storage, provider version, audit event, local-presence
flag, and disposition.

```bash
scripts/secret_migration_guard.py capture-global-state \
  "$ENCRYPTED_RECEIPT_DIR/artifacts.json"

scripts/secret_migration_guard.py global-state --retired \
  --artifact-manifest "$ENCRYPTED_RECEIPT_DIR/artifacts.json"
```

Retirement succeeds only when the same authority configuration returns exactly
the same artifact IDs, each is provider-verified `encrypted-rewrap`, and none is
present locally. This repository provides no secure-retire or removal command.
Existing plaintext artifacts remain contained until rewrap is separately
authorized; containment must not be reported as retirement.

## Integration, rollback, and current stop condition

`security/sai-10-integration-dependencies.json` makes consumer rollout fail
closed until every dependency is an exact independently accepted commit/tree
ancestor. SAI-06 `8b48f3467f49bc523146dd86da56c36ef29ca951` / tree
`3081ba082ebb5ee2db8b92b3e8b01a79fa7dd30d` has static SOURCE GO only; its four
PostgreSQL backup keys and four Secret consumers are recorded as a pending
integration surface, not as present resources. SAI-08 and SAI-09 still have no
accepted source successor recorded here. None of SAI-06/08/09 is integration or
live accepted. Pending dependencies and lack of deployed authority/live
evidence mean no integration or live rollout is authorized.

Rollback is forward-only: add a new immutable bundle that selects a previously
verified retained writer while retaining every admitted read key. Never restore
an exposed local file, overwrite a generation, discard evidence, or select a
bundle unable to read newer ciphertext. This procedure does not authorize
deployment, credential revocation, or cleanup.
