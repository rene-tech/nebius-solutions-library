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
result must equal the fresh independently signed target-cluster RBAC receipt.
Every User and Group subject must resolve to an authenticated identity and its
exact signed group set; every ServiceAccount subject must resolve to the signed
ServiceAccount inventory. Provider access and mutation sets come from the
signed provider-native effective-authority graph, including inherited,
federated and external principals, rather than candidate declarations.
`capture_kubernetes_rbac_inventory.py` emits only the canonical unsigned body
through descriptor-bound, read-only Kubernetes calls; the separate checkpoint
owner signs and installs it on the authority's read-only anchor.

Admission matching evaluates empty, partial and negative selectors against the
exact current Pod generation with Kubernetes semantics. The controller-added
`pod-template-hash` is conservatively treated as present with an unknown
value. The policy permits only the exact content-bound NetworkPolicy to be
created by the security owner; updates, deletion, and later widening selecting
policies are denied at admission, closing the post-init race. This
admission layer remains defense in depth: the target-cluster node group's
provider VPC security group and exact provider IAM inventory are the canonical
boundary.

A second content-bound policy covers Pods, ServiceAccounts, Deployments,
ReplicaSets, DaemonSets, StatefulSets, Jobs and CronJobs. The release identity
can create only the exact token-blind ServiceAccount and Deployment. Generated
Pods must keep the signed image, generation labels, protected node target,
Secret and image-pull-secret allowlists; projected Secrets, host paths, PVCs,
CSI volumes and additional secret-backed environment sources are denied.

The first additive policy uses component `storage-reconciler-v2`. The deployed
fixed predecessor VAP intentionally does not match that value, so its fixed
Deployment, NetworkPolicy, ConfigMap, policy and binding can stay unchanged
while the new generation is created. The v2 reconciler is a distinct resource;
the fixed Deployment selector is never edited. The exact live UIDs and content
digests form a predecessor receipt. A separately mounted prior-head checkpoint
also commits the workloads backend identity, state lineage/serial, Helm release
ID/revision/manifest, predecessor Deployment and NetworkPolicy UIDs/specs, and
the four retained Terraform addresses including `helm_release.control_plane`.
Both Terraform roots compare initialized S3 backend metadata to this signed
custody with descriptor-relative `O_NOFOLLOW` reads. This root will add nothing
unless both custody records name the same predecessor digest.
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

The dependency verifier rejects the rejected SAI-10 commit as an ancestor, not
only as an exact value, for both accepted custody and integration `HEAD`. This
task branch deliberately preserves that history, so the parent must transplant
these additive changes onto a clean accepted lineage before the dependency
record can become `accepted`; rewriting this branch is not authorized.
