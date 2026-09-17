# Pod-security custody root

This standalone Terraform root is the only owner of SAI-07 admission, custody
RBAC, token-anchor, ledger, and retained-quarantine objects. It intentionally
accepts one custody-owner kubeconfig and does not accept the platform,
receipt-operator, or metadata-reader credentials. Its state/backend and CI
principal must be administered outside the platform Terraform trust domain.

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
2. Import/adopt all existing custody objects into this state, then plan. The
   plan must contain no delete, replacement, or unreviewed object.
3. Apply only in a serialized rollout slot, collect exact UID/resourceVersion
   and projected-spec hashes, and issue the signed adoption handoff.
4. Only then may the platform root consume that handoff and execute its
   `removed { destroy = false }` state transfer. The Kubernetes objects remain.
5. Authorization and acknowledgement are separate custody-pipeline actions.
   The platform root never advances the ledger or mints custody tokens.

No operation in this task executed this root. The current no-delete constraint
also prohibits using any cleanup, destroy, replacement, or rollback command.
