# Customer-storage provider egress authority

This root is the canonical boundary for the additive customer-storage
reconciler. It creates a dedicated tainted node group on the exact target
cluster; the group's sole network interface carries a Nebius VPC security
group with no default-route egress.
The ordinary workloads Kubernetes identity does not own these resources.

The root refuses candidate-supplied trust. It descriptor-reads the approval
registry from `/etc/fs2-security-ro/authority/` and the signed prior-head
checkpoint from `/var/lib/fs2-security-checkpoints-ro/`. Both must be private,
root-owned, `O_NOFOLLOW` files on distinct read-only filesystems. Independent
keys sign the manifest and checkpoint. The new manifest must extend the
checkpoint's prior manifest and generation heads plus exact live/provider-state
custody digests. The first generation therefore cannot restart its predecessor
at `null`. The root also descriptor-reads the initialized backend metadata and
requires its complete S3 configuration digest, lock setting, backend lineage,
state lineage/serial/version, snapshot digest and exact managed-address set to
match the separately anchored receipt; an alternate local state cannot start
another history.

The signed manifest fixes provider version `0.5.232`, the exact target cluster,
complete image/storage release values, content-bound NetworkPolicy and
workload admission specs, accepted SAI-10 commit/tree/review custody, exact
project IAM inventory, a provider-native effective-authority graph covering
inherited/federated/external principals, the fresh signed target-cluster RBAC
inventory and its complete subject closure, and every content-named
route/node generation. Canonical state uses a locked,
versioned remote backend; local or omitted state is never an authority source.

Before provider evaluation, a paginated read-only Nebius CLI preflight proves that the
named profile resolves to the exact active service account in the registry,
that its authority group has exactly that one member, that the group's permits
equal the non-admin registry set, and that its exact public-key inventory is
unexpired and bounded to 90 days. It also inventories every project group,
membership, service account, public key and principal access permit and
requires exact equality with the independently signed project receipt. The
separate provider-native authority-graph receipt, rather than candidate
declarations, must derive the exact cluster-access and mutating principal sets;
the singleton authority group must be the only mutator.
`capture_provider_iam_inventory.py` emits the canonical unsigned receipt body
using only paginated read-only API calls; the separate checkpoint owner signs
and installs that body on the read-only authority anchor.

Every security group, rule, and node group has `prevent_destroy`. No target,
replace, state removal, destroy, or generation removal is permitted. A route
change adds a signed generation and keeps every predecessor. Under the current
hard no-delete rule this root is source-only and must not be planned or applied.
The first apply also requires independent approval of the external registry and
a provider-native read-only proof that the owner is the only identity able to
mutate the security project.

A future owner-approved plan must be exported to JSON and accepted by
`security/verify_additive_plan.py`; only `create`, `read`, and `no-op` actions
are valid. No state-forget exception exists.
