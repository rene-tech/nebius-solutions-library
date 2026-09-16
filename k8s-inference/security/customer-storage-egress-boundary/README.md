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
`capture_kubernetes_rbac_inventory.py` emits only the canonical unsigned body
through descriptor-bound, read-only Kubernetes calls; the separate checkpoint
owner signs and installs it on the authority's read-only anchor.

Admission matching evaluates empty, partial and negative selectors with
Kubernetes semantics. Any NetworkPolicy that can select any v2 generation is
create-only behind the security-owner group, closing the post-init race. This
admission layer remains defense in depth: the target-cluster node group's
provider VPC security group and exact provider IAM inventory are the canonical
boundary.

The first additive policy uses component `storage-reconciler-v2`. The deployed
fixed predecessor VAP intentionally does not match that value, so its fixed
Deployment, NetworkPolicy, ConfigMap, policy and binding can stay unchanged
while the new generation is created. The v2 reconciler is a distinct resource;
the fixed Deployment selector is never edited. The exact live UIDs and content
digests form a predecessor receipt. A separately mounted prior-head checkpoint
also commits the workloads backend identity, state lineage/serial and the three
retained Terraform addresses. This root will add nothing unless both custody
records name the same predecessor digest.
The generation-named NetworkPolicy inventory Role and RoleBinding are created
and retained by the security owner, not the Helm release identity, so that
identity has neither `bind` nor `escalate` authority.
`capture_predecessor_receipt.py` creates that receipt through exact read-only
`kubectl get` calls and descriptor-bound kubeconfig access; it reads no Secret.
The capture must occur before the provider ledger is independently signed.

The workloads root keeps all three predecessor addresses with
`prevent_destroy` and `ignore_changes = all`; it does not use `removed` blocks
or post-forget custody. Do not target resources, remove state entries, use `-replace`, or apply a plan
that destroys or replaces any existing generation. The current remediation is
source-only; no live action is authorized.
