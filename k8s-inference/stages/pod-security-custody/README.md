# Pod-security custody root

> **Retained rejected v2 root.** The Terraform resources in this directory are
> preserved as predecessor evidence and remain unreachable because
> `custody-trust-lock.json` is blocked. They must not be initialized, planned,
> imported or applied. Importing the same live objects into this state while
> the platform state retains them would create dual Terraform ownership.

The canonical successor is the non-state-forgetting v3 preflight plus external
executor: `scripts/run_sai07_retained_state_custody_v3.py` and
`scripts/run_sai07_external_execution_v3.py`. The v3 design leaves every
existing address in the platform state, independently downloads the exact
versioned state object read-only, derives its complete custody address set,
and binds that set to immediate live UID/resourceVersion/full-object reads.
The external field manager owns no fields on those retained objects. It may
only server-side apply a new immutable, content-bound acknowledgement object
that raw platform state and the live pre-read both prove absent. Immediate
post-SSA reads must prove every retained UID/resourceVersion/full-object hash is
unchanged. No second Terraform state imports anything and the platform state
never forgets an address. The current v3 trust lock is blocked: no external
SSA, provider boundary, or custody activation is authorized by this commit.

`collect_sai07_authoritative_custody_evidence.py` is the source-pinned,
provider-native read-only adapter. It exhaustively paginates the tenant and
every project returned under it, including principals, groups, bidirectional
memberships, access permits and non-secret credential metadata through the
Nebius SDK; it retains every RPC request/trace ID and never requests credential
secrets. It deliberately does not call the access-key list API because that
response can contain the key secret before client-side filtering. No safe
server-side metadata projection exists in the pinned provider API, so the lock
records that concrete blocker and the collector refuses activation before any
provider call. A later reviewed source revision must implement and pin such an
endpoint. The exact singleton IAM group and native/S3 policies remain
defense-in-depth controls; they are not treated as S3 caller-identity proof. It
captures native bucket state plus S3 ACL, policy, encryption, versioning,
Object Lock, retention, legal hold and an exact version-fenced platform-state
download. Generation outputs are bounded, mode 0600, O_EXCL and retained.
`verify_sai07_custody_trust_v3.py` opens those raw files, independently
reconstructs IAM/backend/state semantics, and requires three distinct pinned
provider/backend/manifest authorities. Every required permit is bound to its
claimed identity or signer, and the platform is excluded from the full
tenant/project/bucket inheritance chain. Caller-supplied summaries and hashes
do not establish any fact. The retained-state preflight requires two fresh,
distinct signed collections and exact equality of their reconstructed IAM,
backend-control and versioned-state projections before emitting a handoff.

The preflight performs no Terraform or Kubernetes mutation. The pinned executor
is a distinct entrypoint: it invokes the external phase-ledger consumer, creates
only the immutable acknowledgement through non-forcing SSA, performs immediate
before/after reads, and emits a signed acknowledgement consumed offline by the
platform rollout gate. Its name is generation-addressed, its exact field set is
hashed, it is absent from platform state, and an existing exact object is only
resumed. The repository lock deliberately contains no activation identities,
keys, bucket, state, or cluster facts; a later independently reviewed
deployment-bound commit is still required. Until then SAI-07 remains
SOURCE/INTEGRATION/LIVE NO-GO, and SAI-03 remains an unaccepted dependency.

The rejected v2 proposal intended this standalone Terraform root to become the
only owner of SAI-07 admission, custody RBAC, token-anchor, ledger, and
retained-quarantine objects. It remains deliberately blocked by
`custody-trust-lock.json`; no ownership or state transfer is authorized by this
source revision. The preserved v2 design accepted one custody-owner kubeconfig
and did not accept the platform, receipt-operator, or metadata-reader
credentials. Its separate state/backend and CI principal were never activated.

The root uses a partial encrypted, lock-enabled S3 backend. A repository-pinned
trust lock and two canonical Ed25519-signed external receipts must prove the
provider/IAM identities, group exclusion, remote backend ownership, state
lineage/serial/object version and immutable retention before the manifest
bundle is considered. None of those facts is accepted from ordinary Terraform
variables. The checked-in lock remains `activation=blocked` until an independent
custodian commits reviewed provider evidence; changing caller inputs cannot
activate it.

The input bundle is canonical, Ed25519-signed, cluster-bound, short-lived, and
binds every desired manifest to its prior live UID, resourceVersion, and spec
hash. It also binds an independently issued IAM receipt proving the owner group
and platform group are distinct and that platform identities cannot manage this
root, its backend, signing key, provider credential, or CI environment.
The predecessor `fs2-pod-security-custody-boundary` policy and binding are
adopted only to preserve their live objects under the no-delete rule. They are
not treated as self-protection; the external IAM/backend/provider boundary is
the preventive control.

Safe non-destructive order:

1. Independently establish the owner/IAM boundary and sign the exact manifest
   bundle. Never use the platform kubeconfig for this root.
2. Enumerate the exact platform backend state lineage and serial. Every static
   address and every dynamic NetworkPolicy, ServiceAccount, and DaemonSet
   instance must have a one-to-one signed manifest entry. Missing, additional,
   duplicate, or unsupported addresses fail closed.
3. Immediately reread every object under the authenticated owner identity and
   compare its UID, resourceVersion, and canonical full-object hash before SSA.
   Create the empty immutable token anchor with an atomic typed POST (never an
   SSA PATCH), import/adopt the complete predecessor set, run SSA without force,
   reread all objects, collect the anchor through PartialObjectMetadata, and
   issue an exact signed acknowledgement for the same set and backend version.
4. The platform root retains every state address in this revision. The archived
   `removed` design is inactive. Relinquishment requires a later reviewed source
   commit after exact adoption is independently accepted; there is no partial
   or count-based handoff.
5. Authorization and acknowledgement are separate custody-pipeline actions.
   The platform root never advances the ledger or mints custody tokens.

No operation in this task executed this root. The current no-delete constraint
also prohibits using any cleanup, destroy, replacement, state-forgetting, or
rollback command. SAI-03 remains an unresolved integration dependency and is
not represented here as accepted.
