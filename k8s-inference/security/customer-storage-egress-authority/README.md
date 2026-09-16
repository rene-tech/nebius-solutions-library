# Customer-storage provider egress authority

This root is the canonical boundary for the additive customer-storage
reconciler. It creates a dedicated tainted node group whose sole network
interface carries a Nebius VPC security group with no default-route egress.
The ordinary workloads Kubernetes identity does not own these resources.

The root refuses candidate-supplied trust. It reads the exact root-owned,
mode-private registry at
`/etc/fs2-security/customer-storage-egress-authority.json` through
descriptor-relative `O_NOFOLLOW` I/O. That registry pins the external owner,
the workloads, release and human identity inventory, the Ed25519 trust key,
and the one approved signed manifest digest. The manifest is an append-only,
hash-chained ledger; every generation name is bound to its complete rule and
node-shape content. Changing state, using another backend, or omitting a map
entry cannot authorize a different provider object.

Before provider evaluation, a read-only Nebius CLI preflight proves that the
named profile resolves to the exact active service account in the registry,
that its authority group has exactly that one member, that the group's permits
equal the non-admin registry set, and that its exact public-key inventory is
unexpired and bounded to 90 days. The resulting identity digest is carried in
the provider handoff.

Every security group, rule, and node group has `prevent_destroy`. No target,
replace, state removal, destroy, or generation removal is permitted. A route
change adds a signed generation and keeps every predecessor. Under the current
hard no-delete rule this root is source-only and must not be planned or applied.
The first apply also requires independent approval of the external registry and
a provider-native read-only proof that the owner is the only identity able to
mutate the security project.

A future owner-approved plan must be exported to JSON and accepted by
`security/verify_additive_plan.py`; only `create`, `read`, and `no-op` actions
are valid for this root. The workloads retention plan may additionally forget
only the three exact addresses declared in `customer_storage_retention.tf`.
