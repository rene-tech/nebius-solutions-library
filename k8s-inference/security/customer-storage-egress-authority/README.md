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
at `null`. The signed prior state carries an explicit installed-generation
hash chain whose final content digest must equal the separately anchored prior
generation head. A successor-only manifest begins from that exact digest; no
lexical ordering of content-derived names is treated as chronology. The root
requires the checkpoint to retain each installed or partially installed
generation's complete content-bound payload. Every provider resource iterates
the union of those retained payloads and the new signed payloads at the same
state addresses; installed entries use `ignore_changes = all` plus
`prevent_destroy`, while a successor is still created from its exact signed
payload. Omitting a retained key therefore cannot turn a rotation into a
destroy proposal or a `prevent_destroy` dead end.
The same checkpoint carries the canonical spec/digest and Deny binding for
every retained legacy and v3 admission generation. Both the security root and ordinary
workloads consumer re-read all of them live; state ownership or
`ignore_changes` is never accepted as live-equality evidence. The root
also descriptor-reads the initialized backend metadata and requires its
complete S3 configuration digest, lock setting, actual remote lineage,
serial/snapshot bytes, exact non-empty managed-address set, and remote object
version to match the separately anchored receipt. The object version comes
from a fixed root-owned, read-only, digest-bound adapter, so an alternate local
state cannot start another history.

The signed manifest fixes provider version `0.5.232`, the exact target cluster,
complete image/storage release values, content-bound NetworkPolicy and
workload admission specs, accepted SAI-10 commit/tree/review custody, exact
project IAM inventory, a provider-native effective-authority graph covering
inherited/federated/external principals, the fresh signed target-cluster RBAC
inventory, its complete subject closure and its derived binding-to-rule
effective-authority graph, deterministic Kubernetes-native groups, exact
provider-bound controller identities, the exact signed OTel/GPU compatibility
observer inventory, the retained filesystem CSI/Prometheus node-exporter/OTel
node-agent inventory, the activated protected-node name and complete live
scheduling-label projection, and every
content-named route/node generation. Every new
node generation uses a separately signed lane-unique selector/taint key. The
lane ID is independent of the final content-hashed authority generation so
observer UIDs can be captured before that generation is sealed. Retained resources
keep their original key under `ignore_changes`, so source never proposes their
replacement. The signed workload inventory includes exact namespaces, names,
live UIDs, canonical DaemonSet specs and non-system release owners. The provider
generation is invalid until both additive observers exist and the three
retained critical node agents have been read exactly. A successor node group is
created with autoscaling bounded to zero through one node, then exactly one
non-credentialed node is activated. Its canonical Kubernetes name and complete
`.metadata.labels` map are added to the independently signed generation before
admission is installed. Admission can therefore evaluate `nodeSelector` and
required node-affinity term OR / requirement AND semantics against the exact
node rather than treating one permissive expression as proof. The
credential-bearing storage release remains blocked until that
admission boundary is live. Canonical state uses a locked,
versioned remote backend; local or omitted state is never an authority source.

Before provider evaluation, a paginated read-only Nebius CLI preflight proves that the
named profile resolves to the exact active service account in the registry,
that its authority group has exactly that one member, that the group's permits
equal the non-admin registry set, and that its exact public-key inventory is
unexpired and bounded to 90 days. It also inventories every project group,
membership, service account, public key and principal access permit and
requires exact equality with the independently signed project receipt. The
separate provider-native authority-graph receipt, rather than candidate
declarations, must derive the exact cluster-access and mutating principal sets.
A fixed root-owned, read-only, digest-bound provider adapter independently
re-derives that graph at evaluation time, including inherited, federated and
external principals; the singleton authority group must be the only mutator.
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

A future owner-approved saved plan must be exported to JSON and pass
`security/verify_additive_plan.py`; only `create`, `read`, and `no-op` actions
are valid. Execution must then use `security/apply_custodied_additive_plan.py`,
which has fixed public-key and execution-profile paths on root-owned read-only
filesystems. That profile digest-binds descriptor-open Terraform and Git
binaries, the CLI config, a read-only plugin/data directory, and a minimal
environment whose credential files are separately hashed. No inherited
`PATH`, `TF_*`, plugin or credential environment is used. The wrapper keeps
the backend metadata and dependency-lock descriptors stable across the exact
saved-plan apply, verifies clean source and rejected-SAI-10 ancestry, and
proves predecessor and successor remote-state identities. It has no plan,
destroy, replace, state-forget, or cleanup mode.

## v10 provisioning/attestation split

The v10 successor supersedes the earlier five-role and precomputed-label
description above without removing its historical record. Stable provider
provisioning now lives in `security/customer-storage-lane-provisioning`, under
its own locked remote state and signed provider-input-only manifest. Its
content-bound `p...` generation creates the lane key, taint, security group and
NodeGroup before any Node identity exists. It outputs only a receipt seed; a
separate owner verifies remote-state custody and signs the resulting exact
security-group and NodeGroup IDs.

Only then may this authority ledger append a `g...` attestation generation. It
binds that provisioning receipt, exact Node name/UID/observed resourceVersion,
full labels and taints, and the audit-derived controller `userInfo` username,
UID and deterministic groups. No `system:controller:*` role label is accepted
as an authenticated actor. The signed RBAC receipt also contains the complete
cluster-wide inventory of every DaemonSet with a keyless blanket
`Exists`/`NoSchedule` toleration; the admission inventory must equal it exactly,
so CNI, kube-proxy, GPU, storage and telemetry agents are not represented by a
fixed source allowlist.

## v11 bootstrap and live-custody successor

The v11 source supersedes the v10 zero-minimum bootstrap without removing the
v10 record. A newly signed provider-only `p...` generation is fixed at one
bootstrap node (`min=1`, `max=1`); it no longer depends on a DaemonSet to wake a
zero-sized NodeGroup. `capture_provisioning_receipt.py` invokes only the pinned,
root-owned read-only custody adapter. The resulting externally signed v3
receipt binds the fresh remote backend lineage, serial, version and full address
set to a fresh provider read of the exact security group, NodeGroup and member
provider IDs. The authority verifier re-invokes that exact digest-pinned adapter
and requires byte-equivalent provider/backend custody before accepting the new
generation. The Kubernetes Node attestation must carry a `spec.providerID`
present exactly once in that signed NodeGroup membership.

Controller identities now come from a separately signed, fresh audit artifact
at the fixed authority path. A separately pinned root-owned read-only audit
adapter re-fetches the exact audit IDs from the cluster audit backend and must
return byte-equivalent normalized event bodies. The artifact binds the real
authenticated username, UID and groups for Deployment, ReplicaSet, DaemonSet,
scheduler and node-health events; the RBAC collector no longer accepts
caller-provided controller JSON. Critical
DaemonSet maintainers likewise require a successful exact-name update/patch
audit event and resourceName-fenced RBAC. Wildcard DaemonSet mutation remains
forbidden.

DaemonSet discovery records a complete list resourceVersion and full-list
digest, then repeats the read at that exact resourceVersion. The admission root
repeats the complete double read at the activation boundary. The v12 successor
below moves both reads ahead of binding activation and relies on continuous
external enforcement afterward. This closes missing, newly added and
single-read blanket-agent gaps while preserving all inventoried CNI,
kube-proxy, GPU, storage and telemetry agents.

## v12 continuous-agent fence and retained generation overlap

The v12 source removes the false claim that a generation-local exact
DaemonSet spec can support upgrades while every retained Deny binding remains
active. Before an ordinary boundary binding may be created, a separately
owned, continuously enforced admission fence must already be live. A
root-owned read-only registry pins its adapter, enforcer artifact and prior
snapshot-ledger anchor. Source code defines the exact policy and binding
semantics. A fresh signed receipt carries their full live normalized specs,
UIDs and resourceVersions plus the complete double-read DaemonSet inventory
and full append-only ledger. The verifier recomputes both spec digests, every
snapshot digest, predecessor link, content generation and the final head, then
requires the adapter's independent live read to return the same objects. Each critical DaemonSet
and controller-created Pod carries a content generation and snapshot digest;
the external fence, not a generation-local exact-spec predicate, decides
whether the transition is present in that ledger. Retained fence-aware
policies therefore compose across old and new snapshots. The verifier rejects
any retained pre-fence generation that would conjunctively deadlock such an
upgrade. That rejection is intentional: this branch must be transplanted onto
an accepted pre-activation lineage rather than weakening, editing, or removing
an existing Deny binding.

The provisioning receipt is now semantic, not merely a byte-equal echo. Its
remote-state managed-address set must equal the seven expected addresses for
every retained provisioning generation. The live security group must have the
exact signed labels and exactly four uniquely identified rules, with the exact
private ingress, DNS, database, and provider/API CIDRs, protocols, directions,
ports, priorities, and no extra route. The NodeGroup must carry only that
security group, exact labels/template label/taint, and one exact member.

Each provisioning generation is a one-member NodeGroup declared
`GENERATIONAL_SINGLETON_RETAIN_PREDECESSOR`: `min=1`, `max=1`,
`max_surge=0`, `max_unavailable=0`. Admission denies an in-place second member,
replacement or deletion for that generation. The only permitted source action
is to prepare a new content-bound provisioning generation with a new lane ID,
security group, NodeGroup, attested Node and admission generation while the
predecessor stays retained. Preparation is not repair, cutover or retirement.
A customer-safe quiesce and drain must precede application cutover. The active
no-delete rule does not authorize Pod eviction, workload teardown or
predecessor retirement, so execution stops at source preparation until an
independently reviewed lifecycle protocol exists. Provider behavior that cannot
honor frozen per-generation membership is a deployment blocker.
