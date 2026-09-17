# Pod-security custody root

This standalone Terraform root is intended to become the only owner of SAI-07
admission, custody RBAC, token-anchor, ledger, and retained-quarantine objects.
It is deliberately blocked today by `custody-trust-lock.json`; no ownership or
state transfer is authorized by this source revision. It intentionally
accepts one custody-owner kubeconfig and does not accept the platform,
receipt-operator, or metadata-reader credentials. Its state/backend and CI
principal must be administered outside the platform Terraform trust domain.

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
