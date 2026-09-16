# Operator access and credential migration

This platform treats operator handoff, key rotation, and Terraform-state
retirement as gated migrations. A credential generation is immutable after it
is named. Roll forward by adding a generation; never put new bytes behind an
existing generation ID or restore a disclosed local file during rollback.

## Control-plane source allowlist

`deployment.cluster.control_plane_allowed_cidrs` is the approved egress set,
not a general network allowlist. It accepts one to eight canonical IPv4 `/32`
or IPv6 `/128` entries. This semantic host-only rule rejects default routes,
large subnets, and combinations such as complementary `/1` networks.

Before applying infrastructure, keep the current session open, test a second
connection from every approved address, and review that the plan changes only
the intended endpoint. Live acceptance must compare the normalized provider
value with the configured set for exact equality.

## Expiring viewer handoff

Terraform creates a dedicated service account with only a project `viewer`
permit. Private key material is issued outside Terraform and never enters a
plan or state file. Use `scripts/operator_handoff.py` from an owner-only host:

```bash
scripts/operator_handoff.py --directory "$HANDOFF_DIR" issue \
  --service-account-id "$SERVICE_ACCOUNT_ID" \
  --project-id "$PROJECT_ID" --cluster-id "$CLUSTER_ID" \
  --expires-at 2026-10-01T00:00:00Z \
  --predecessor-public-key-id "$OLD_PUBLIC_KEY_ID" \
  --predecessor-service-account-id "$OLD_SERVICE_ACCOUNT_ID" \
  --predecessor-project-id "$OLD_PROJECT_ID"

scripts/operator_handoff.py --directory "$HANDOFF_DIR" \
  acknowledge-delivery --recipient "$RECIPIENT_ID"

scripts/operator_handoff.py --directory "$HANDOFF_DIR" verify \
  --approved-egress 192.0.2.8/32

scripts/operator_handoff.py --directory "$HANDOFF_DIR" \
  revoke-old --confirm-predecessor-public-key-id "$OLD_PUBLIC_KEY_ID"
```

The verifier requires successful namespace inventory, denial of pod creation,
denial of Secret reads, and exact equality between live and approved API CIDRs.
Issuance reads the predecessor from Nebius and binds its key ID, service
account, and project into the receipt. It also reads the new key back and
requires the provider's expiry to equal the requested instant; if that proof
fails, the new key is immediately revoked and no receipt is written. The old
handoff key cannot be revoked until delivery and verification are recorded.
`revoke-old` accepts the bound predecessor ID only as an explicit confirmation:
it revalidates the receipt-bound identity, deletes exactly that key, then proves
both authoritative `get` absence and absence from the complete project
inventory while retaining the verified successor. Repeating the operation or
supplying a different ID fails without another delete. Verification parses the
complete SelfSubjectRulesReview and rejects every mutation/escalation verb and
every Secret read rule, in addition to explicit negative probes. The receipt never
contains a private key, bearer token, Kubernetes Secret, or customer payload.

## Immutable generations and keyrings

Generation 1 uses the existing Terraform resource addresses and exact stored
bytes. It remains protected while those values are imported into encrypted,
access-logged escrow. Do not plan a delete or replacement of these addresses.
Before the next workloads plan, create one write-once, mode-0600 value-free
identity receipt from the current state JSON inside the owner-only run root:

```bash
terraform -chdir=stages/workloads show -json "$RUN_ROOT/workloads.tfstate" | \
  scripts/secret_migration_guard.py capture-state - \
    "$RUN_ROOT/fixed-v1-identity.receipt.json" \
    --source-commit "$(git rev-parse HEAD)"
```

The receipt contains only resource addresses and SHA-256 fingerprints. The
wrapper requires it whenever protected state exists, compares every current v1
source and Secret identity to it, rejects update/replacement/deletion, and runs
the guard after every plan and again from the exact saved plan immediately
before every workloads apply. For an independently generated plan, run the same
guard explicitly:

```bash
terraform show -json workloads.tfplan | \
scripts/secret_migration_guard.py plan - \
  --identity-receipt "$RUN_ROOT/fixed-v1-identity.receipt.json"
```

Payload AEAD, ledger HMAC, PAT pepper, and route-attestor generations advance
independently. For generation `N > 1`, supply the corresponding complete
keyring through one of these environment variables:

- `FS2_PAYLOAD_KEYRINGS_JSON`
- `FS2_LEDGER_KEYRINGS_JSON`
- `FS2_TOKEN_PEPPER_KEYRINGS_JSON`
- `FS2_ROUTE_ATTESTOR_SETS_JSON`

Each environment value is a JSON object keyed by generation number. The first
three documents must contain every immutable ID from `v1` through their own
generation, select that generation for writes, and reproduce the imported `v1`
bytes exactly. Each attestor document retains all public keys through its own
generation. `active` selects the consumer Secret while `retained` remains a
contiguous append-only history, so rollback can select an older Secret without
deleting a newer generation. Terraform uses `data_wo`; it never overwrites the
fixed generation-1 Secret.

Admin and bootstrap PAT values use the same append-only rule through
`credential_generation_history`. Supply their generation-keyed JSON maps via
`FS2_ADMIN_TOKENS_JSON`, `FS2_BOOTSTRAP_ACCESS_TOKENS_JSON`, and, when enabled,
`FS2_SCIENTIFIC_ACCESS_TOKENS_JSON`. A new generation creates a new Secret and
PAT ID; it does not rewrite the generation-1 Secret. Keep both PATs active for
the acceptance overlap, then revoke the predecessor through the audited admin
API. Admin-token cutover is readiness-gated; its predecessor is retained for
rollback evidence but must not be reactivated after confirmed disclosure.
All later PAT IDs must be unique across every generation and both general and
scientific audiences, and must differ from both fixed generation-1 IDs. The
wrapper rejects duplicate external IDs before planning and Terraform rechecks
the complete set against the persisted v1 resources.

Before cutover, create a private mode-0600 token map containing generation 1
and every retained generation by audience. Create its value-free receipt, then
prove both old and new against a semantic authenticated endpoint:

```bash
scripts/pat_rotation_guard.py map --tokens-file "$PAT_MAP" \
  --receipt "$PAT_MAPPING_RECEIPT"
scripts/pat_rotation_guard.py prove-overlap --tokens-file "$PAT_MAP" \
  --mapping-receipt "$PAT_MAPPING_RECEIPT" --audience general \
  --old-generation 1 --new-generation 2 --endpoint "$MODELS_ENDPOINT" \
  --receipt "$PAT_OVERLAP_RECEIPT"
```

After the audited admin API revokes generation 1, run `prove-revocation` with
the same arguments and a new receipt. It requires old `401/403` and new
semantic success. Receipts contain PAT IDs, SHA-256 fingerprints, response
hashes, and status only—never token values or bodies.

Database credentials use a distinct overlap contract. Supply
`FS2_DATABASE_PASSWORDS_JSON` as a JSON object keyed by every retained
generation greater than one; each generation contains `owner`, `runtime`,
`maintenance`, `activation`, `restore_verifier`, `reporting`, and `monitoring`
passwords. A generation creates new login roles and immutable account Secrets
while generation 1 remains untouched. CloudNativePG must first report the
expanded role set healthy; only then are the write-only consumer Secrets
updated and the generation-triggered Helm rollout allowed to proceed. The old
login remains valid during the readiness window and rollback.

The runtime already uses the envelope key ID for payload decryption, the
stored HMAC key ID for ledger replay, and the stored pepper ID for PAT
verification. New writes use only the active key. Route attestations remain
valid while their public key is retained. Non-secret generation metadata is
hashed into the pod/job templates for runtime, database migrations,
maintenance, token bootstrap, model controller, and Grafana. Helm is atomic,
waits for readiness, and updates Secrets before their consumers.

Payload-key retirement has an additional gate. Inventory and decrypt every
stored operation payload, request-debug exchange, and customer-storage secret;
rewrap each retained row under the new active key; then independently prove the
old key ID has zero references. Until that evidence exists, the old payload key
stays in the ring. Payload, ledger, pepper, and attestor retirement evidence is
separate; success for one class does not authorize another.

## Scoped credential delivery

There is no aggregate credential export. `output` requires one explicit kind:

```bash
./inference-stack output --var-file terraform.tfvars \
  --credential-kind general-access \
  --credential-expires-in-seconds 3600 \
  --credential-file "$HANDOFF_DIR/general-access.json"
```

Only `general-access` and `scientific-access` can be delivered, and only when
the PAT has a server-enforced expiry no later than the requested handoff TTL.
The wrapper reads only that selected live Secret, writes a mode-0600 delivery
file, and writes a separate mode-0600 value-free receipt. It prints only receipt
metadata. Admin and Grafana source credentials are never copied: their apparent
file expiry would not expire the underlying credential. A future privileged
handoff must mint a server-enforced TTL admin session or Grafana service-account
token with independently verified revocation.

## Legacy state retirement

Local state is retained temporarily because it contains the only authoritative
generation-1 values. Keep the run root mode 0700, every file mode 0600, and
reject all symlinks. Do not copy plaintext state into tickets, logs, CI
artifacts, or ordinary backups.

After the exact generation-1 values have been imported and checked in the
encrypted, access-logged state/escrow target:

1. migrate Terraform state with the backend's supported `init -migrate-state`
   workflow and verify the remote object version/audit event;
2. prove a no-op plan against the remote state and run the protected-address
   plan guard;
3. capture the exact pre-retirement inventory outside the run root, retain the
   encrypted version needed for rollback, and securely retire every
   local state, backup, `*.tfplan`, `*.plan.json`, admin-cookie, scoped
   credential export, expired credential handoff, and unknown artifact;
4. run the absence canary:

   ```bash
   scripts/secret_migration_guard.py capture-run-root "$RUN_ROOT" \
     "$ENCRYPTED_RECEIPT_DIR/run-root-artifacts.receipt.json"
   scripts/secret_migration_guard.py run-root "$RUN_ROOT" --retired \
     --artifact-manifest "$ENCRYPTED_RECEIPT_DIR/run-root-artifacts.receipt.json"
   ```

The manifest is value-free and binds every known and unknown file by relative
path and SHA-256. Retirement succeeds only when the local run root contains
zero files. During migration, omit `--retired`; the guard reports known,
unknown, and total counts without printing file content.

## Ordered rollout and rollback

Rotate one class at a time. Create the new versioned Secret/keyring, run
pre-existing-data canaries, then update consumers and wait for readiness. For
PATs, prove old and new tokens during overlap and explicitly revoke the old
token only after the new token succeeds. For database credentials, provision a
new login/Secret, wait for the three-instance database to become healthy, prove
the new login, then roll write-only consumer Secrets and wait for every
generation-annotated workload; do not mutate the password behind a mounted
fixed Secret. Retire a superseded login only in a later reviewed generation
after rollback and connection-drain evidence exists.

Rollback switches consumers to the last verified Secret and application
revision. It never deletes a historical key generation, restores permissive
file modes, copies an exposed state backup into service, or makes a disclosed
credential active again. A disclosed value is rotated forward and revoked.
