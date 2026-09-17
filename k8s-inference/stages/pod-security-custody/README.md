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
versioned state object read-only, and derives a complete per-instance
projection: exact count/for_each address, API identity, Terraform attribute
digest, UID/resourceVersion where the provider records them, and canonical
security-relevant desired semantics. Dynamic keys must equal their raw object
names. The signed bundle must bind that projection and immediate live reads
must match the same desired semantics plus UID/resourceVersion/full-object
hashes.
The external field manager owns no fields on those retained objects. It may
only server-side apply a new immutable, content-bound acknowledgement object
that raw platform state and the live pre-read both prove absent. Immediate
post-SSA reads must prove every retained UID/resourceVersion/full-object hash is
unchanged. No second Terraform state imports anything and the platform state
never forgets an address. The current v3 trust lock is blocked: no external
SSA, provider boundary, or custody activation is authorized by this commit.

`collect_sai07_authoritative_custody_evidence.py` is the source-pinned,
provider-native read-only adapter. It exhaustively paginates the tenant and
every project returned under it, including service accounts, groups,
bidirectional direct/transitive memberships and access permits through the
Nebius SDK; it retains every RPC request/trace ID. It deliberately calls no
credential enumeration API. In particular, an access-key list response can
contain the key secret before client-side filtering, while enumerating another
credential class would not prove the identity that signed an S3 request.
Instead, each activation is bound to one unique active service-account epoch:
Profile Get proves the current caller and its project/tenant lineage, the
complete group/permit graph must exactly equal the contract, and every prior
epoch service account remains present with zero direct or inherited authority.
Prior credentials are preserved, but their principals cannot inherit the
current epoch's permissions. Successful S3 reads must also pass the exact
singleton-principal native/S3 policy boundary. The collector captures native
bucket state plus S3 ACL, policy, encryption, versioning,
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
is a distinct entrypoint. Immediately before and after its additive writes, it
runs SelfSubjectReview, the exhaustive SelfSubjectAccessReview matrix and a
complete SelfSubjectRulesReview for every signed namespace using one
ten-minute, Kubernetes-API-audience epoch token received only through an
inherited descriptor. The repository binds its issuer and JWT subject to the
unique provider service-account epoch; the authenticator must expose the exact
signed username/groups and JTI. No stable owner kubeconfig is accepted. Every
live read, token-anchor operation and acknowledgement SSA uses that token. Secret reads
use only PartialObjectMetadataList. The empty immutable token anchor is created
by atomic typed POST whose response must be PartialObjectMetadata, behind the
signed fail-closed exact-shape admission policy. That policy evaluates every
write by the external execution identity, so its namespace-wide CREATE/PATCH
RBAC cannot be used for an arbitrary ConfigMap or Secret; only the exact anchor
or generation acknowledgement is admitted. The executor then invokes the
external phase-ledger consumer, creates only the immutable acknowledgement
through non-forcing SSA, performs immediate before/after reads, and emits a
signed acknowledgement. Its name is generation-addressed, its exact field set
is hashed, and it is absent from the complete raw platform state. The platform
rollout gate has a retained `terraform_data` freshness clock whose `timestamp()`
input updates in place on every attempt. The acknowledgement data source
depends on that pending update and therefore runs during apply even for an
unchanged-phase saved plan. The create/replace-only provisioner is defense in
depth, not the ongoing freshness boundary. Every baseline-label owner in the
foundation, scientific, academic, ModelExpress, and reference-data paths is
ordered after that verified output.

The platform state retains every original address, including the count-indexed
admission objects. Active `prevent_destroy` declarations replace the prior
comment-only gap; no `removed`, state-forget, import, or second-state ownership
exists. The raw state receipt binds its complete byte hash plus all managed
address count/digest and the complete derived object-semantic aggregate. Any
resource using the retention-only provider that is not in the exhaustive
static/dynamic custody inventory fails closed. Provider exclusion is computed
from mandatory categories rather than an optional caller list: tenancy,
backend group/bucket, epoch identities, identity groups and permit targets,
all signer principals/signing resources, and external Kubernetes authorization.
The tokenless Secret-metadata reader and its exact list/TokenRequest RBAC are
additive platform-state resources created before the retained custody boundary;
the manifest contract accepts them absent only during that first preparation
and requires their exact state/live identities thereafter. The executor has no
RBAC mutation authority.

The repository lock deliberately contains no activation identities, keys,
bucket, state, epoch, dependency acceptance, or cluster facts. SAI-03 and
SAI-04 are both explicitly unaccepted dependencies. A later independently
reviewed deployment-bound commit is still required. Until then SAI-07 remains
SOURCE/INTEGRATION/LIVE NO-GO.

`custody-source-lock-v3.json` closes the executable dependency set used by the
external executor: raw-state semantic reconstruction, v1/v2/v3 manifest
validation, v2/v3 trust verification, retained-state preflight, and the
metadata-only Secret transport each have a fixed path and content digest. The
main trust lock pins the source-lock digest. Runtime activation must therefore
match this reviewed source graph as well as the deployment facts.

The no-delete source contract retains both legacy and exception OTel, DCGM,
node-exporter and GPU-observer generations in every phase. Their releases,
DCGM registry Secrets and immutable configuration are destruction-protected.
Baseline enforcement therefore remains blocked while any incompatible legacy
object is present; source does not manufacture closure by disabling or
uninstalling a predecessor.

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
   Under the source-pinned external executor, authenticate the actual owner by
   SSR/SSRR/SSAR, create the empty immutable token anchor with an atomic typed
   POST whose response is metadata-only, run the acknowledgement SSA without
   force, reread all retained objects and the anchor metadata, and issue an
   exact signed acknowledgement for the same raw-state version.
4. The platform root retains every state address in this revision through
   active, count-correct `prevent_destroy` declarations. The archived `removed`
   design is inactive. No relinquishment, partial handoff or count-flattening
   is authorized.
5. Authorization and acknowledgement are separate custody-pipeline actions.
   The platform root never advances the ledger or mints custody tokens.

No operation in this task executed this root. The current no-delete constraint
also prohibits using any cleanup, destroy, replacement, state-forgetting, or
rollback command. SAI-03 remains an unresolved integration dependency and is
not represented here as accepted.
