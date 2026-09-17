# Customer-storage egress security boundary

This is the Kubernetes defense-in-depth companion to the separate Nebius
provider authority in `security/customer-storage-egress-authority`. It is not
a child module of the ordinary workloads stage and must use a different
kubeconfig/credential. It owns immutable, generation-named public trust,
signed contract, and NetworkPolicy objects plus a secondary validating policy.
The provider VPC security group and target-cluster tainted node group remain
the canonical boundary even if Kubernetes admission is bypassed.

The inputs are append-only. Contract, trust, and boundary generation entries
must never be removed or edited after apply. A rotation adds a new generation,
applies it with the security-owner identity, and passes `current_handoff` to a
later workloads plan. Old resources remain protected by `prevent_destroy` and
old admission bindings remain active. This overlap is intentional: no rollout
or rollback requires deleting or replacing a security object.

Every boundary generation is create-only and admits additions only
for authenticated members of
`fs2:customer-storage-egress-security-owner`. The workloads and release
identities must not be members of that group or mutate admission generations.
Every exact kubeconfig is root-owned on a read-only filesystem; its
descriptor-read digest, authenticated subject, category and provider principal
must match the signed exhaustive inventory. Permission probes cover Secrets,
exec/attach/port-forward, ephemeral containers, service-account tokens, CSR
approval/signing, workload and service-account mutation, impersonation, RBAC
bind/escalate/delegation and webhook mutation. The v2 Helm chart is wired here
as an append-only, generation-named release using the separately inventoried
release identity and ConfigMap Helm driver; no operator-side install is
canonical. The dependency gate also rejects the plan unless
`HELM_DRIVER=configmap`, so this requirement cannot remain a comment-only
operator convention.
The owner also hashes every live Role, RoleBinding, ClusterRole and
ClusterRoleBinding, including subjects, rules, UIDs and resource versions; the
result and the derived binding-to-role effective-authority graph must equal the
fresh independently signed target-cluster RBAC receipt. Namespace-scoped
permission probes enumerate every observed namespace separately; they do not
use `--all-namespaces` as an authority shortcut. Impersonation probes use the
core `users`, `groups`, and `serviceaccounts` resources and also cover Pod
binding/eviction, node proxy/status, TokenRequest, RBAC delegation, and
workload-controller pivots.
Every User and Group subject must resolve to an authenticated identity and its
exact signed group set; every ServiceAccount subject must resolve to the signed
ServiceAccount inventory. Provider access and mutation sets come from the
signed provider-native effective-authority graph, including inherited,
federated and external principals, rather than candidate declarations.
Each signed ServiceAccount and Kubernetes-native User/Group also carries the
digest of its direct-plus-group rule closure and the exact expanded dangerous
capability set. Wildcards and resource names are evaluated semantically for
Secret, ConfigMap, Pod/exec/attach/binding, TokenRequest, node, workload,
NetworkPolicy, RBAC bind/escalate, impersonation, admission and CSR pivots;
matching the cluster-wide RBAC hash alone is insufficient.
`capture_kubernetes_rbac_inventory.py` emits only the canonical unsigned body
through descriptor-bound, read-only Kubernetes calls; the separate checkpoint
owner signs and installs it on the authority's read-only anchor.

Admission matching evaluates empty, partial and negative selectors against the
exact current Pod generation with Kubernetes semantics. The controller-added
`pod-template-hash` is read from each actual Pod and supplied to both the
readiness verifier and the post-rollout workloads proof; `Exists`, `In`,
`NotIn`, and negative selectors therefore cannot hide a widening policy. The
policy permits only the exact content-bound NetworkPolicy to be
created by the security owner; updates, deletion, and later widening selecting
policies are denied at admission, closing the post-init race. This
admission layer remains defense in depth: the target-cluster node group's
provider VPC security group and exact provider IAM inventory are the canonical
boundary.

Every retained v3 policy and Deny binding is listed with its full canonical
spec in the separately signed prior boundary checkpoint. Before a successor is
created, both this root and the workloads root read every listed live object
and require exact equality. A drifted older generation therefore cannot hide
behind Terraform `ignore_changes`.

A second content-bound policy covers Pods, ServiceAccounts, Deployments,
ReplicaSets, DaemonSets, StatefulSets, Jobs and CronJobs. The release identity
can create only the exact token-blind ServiceAccount and Deployment. Only the
signed Deployment controller may create its exact ReplicaSet child, and only
the signed ReplicaSet controller may create the corresponding Pods. Generated
Pods must keep the signed image, generation labels, protected node target,
Secret and image-pull-secret allowlists; projected Secrets, host paths, PVCs,
CSI volumes, `spec.nodeName`, and additional secret-backed environment sources
are denied.
The workload policy also has a cluster-wide guard branch: outside the exact
storage and Kubernetes system namespaces it denies direct `nodeName`, the
generation's node selector/taint, and blanket `Exists` tolerations. Retained
policies protect their own retained node groups without selecting later exact
storage workloads in `fs2-system`.

The ConfigMap Helm driver is not a namespace-wide exemption. RBAC may grant
the release identity namespace-scoped create only because Kubernetes cannot
resource-name-restrict create; update/patch must be restricted to the exact
generation's Helm v1 record. The active policy matches every ConfigMap request
from that identity and admits only that record's canonical labels and sole
`release` data key. Contract/trust ConfigMaps and all other namespace config
remain unreachable, and delete is never admitted.

The retained first additive policy uses component `storage-reconciler-v2`.
Compatibility-v3 uses disjoint `fs2-storage-v3-*` names, the
`storage-reconciler-v3` component, and a content-derived release identity. The
fixed predecessor and retained v2 VAPs therefore do not select or authorize v3
objects. Each v3 boundary and workload policy map entry stores its own exact
canonical policy spec and digest, so retained generations protect themselves
without matching a later rotation. The fixed Deployment selector is never
edited. The exact live UIDs and content
digests form a predecessor receipt. A separately mounted prior-head checkpoint
also commits the workloads backend identity, state lineage/serial, Helm release
ID/revision/manifest, predecessor Deployment and NetworkPolicy UIDs/specs, and
the four retained Terraform addresses including `helm_release.control_plane`.
Both Terraform roots compare initialized S3 backend metadata plus the actual
remote state lineage, serial, snapshot bytes, exact non-empty address set, and
object-store version ID to signed custody. The object version is obtained only
through a fixed root-owned, read-only, digest-bound provider adapter. This root
will add nothing unless both custody records name the same predecessor digest.
The generation-named NetworkPolicy inventory Role and RoleBinding are created
and retained by the security owner, not the Helm release identity, so that
identity has neither `bind` nor `escalate` authority.
`capture_predecessor_receipt.py` creates that receipt through exact read-only
`kubectl get` calls and descriptor-bound kubeconfig access; it reads no Secret.
The capture must occur before the provider ledger is independently signed.

The workloads root keeps the three direct predecessor addresses plus the
control-plane Helm release with `prevent_destroy`; the direct objects also use
`ignore_changes = all`. It does not use `removed` blocks
or post-forget custody. Do not target resources, remove state entries, use `-replace`, or apply a plan
that destroys or replaces any existing generation. The current remediation is
source-only; no live action is authorized.

The only future execution entry point is
`security/apply_custodied_additive_plan.py`. It descriptor-binds the exact
root-owned saved-plan bytes, externally signed approval under a fixed
read-only public key/execution profile, clean source commit/tree, accepted
SAI-10 ancestry and predecessor state; rejects every action other than
create/read/no-op; applies those same bytes; then proves the expected successor
lineage, minimum serial and exact address set. It has no plan-generation or
cleanup mode.

The dependency verifier rejects the rejected SAI-10 commit as an ancestor, not
only as an exact value, for both accepted custody and integration `HEAD`. This
task branch deliberately preserves that history, so the parent must transplant
these additive changes onto a clean accepted lineage before the dependency
record can become `accepted`; rewriting this branch is not authorized.
