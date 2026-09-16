# Customer-storage egress security boundary

This is the Kubernetes defense-in-depth companion to the separate Nebius
provider authority in `security/customer-storage-egress-authority`. It is not
a child module of the ordinary workloads stage and must use a different
kubeconfig/credential. It owns immutable, generation-named public trust,
signed contract, and NetworkPolicy objects plus a secondary validating policy.
The provider VPC security group and dedicated tainted node group remain the
canonical boundary even if Kubernetes admission is bypassed.

The inputs are append-only. Contract, trust, and boundary generation entries
must never be removed or edited after apply. A rotation adds a new generation,
applies it with the security-owner identity, and passes `current_handoff` to a
later workloads plan. Old resources remain protected by `prevent_destroy` and
old admission bindings remain active. This overlap is intentional: no rollout
or rollback requires deleting or replacing a security object.

Every boundary generation rejects deletion and admits protected changes only
for authenticated members of
`fs2:customer-storage-egress-security-owner`. The workloads and Helm identities
must not be members of that group or have RBAC to update/delete admission
policies or bindings. Every plan performs descriptor-bound `auth whoami` and
`auth can-i` checks for the owner, workloads, release, human, break-glass and
other kubeconfigs. Their exact categorized subject inventory must hash to the
root-owned provider registry; a candidate cannot omit an identity. Only hashes
enter Terraform. Operator approval of that identity split and read-only live
verification of all retained objects are required before any apply. This
source root does not constitute that approval.

The first additive policy uses component `storage-reconciler-v2`. The deployed
fixed predecessor VAP intentionally does not match that value, so its fixed
Deployment, NetworkPolicy, ConfigMap, policy and binding can stay unchanged
while the new generation is created. The v2 reconciler is a distinct resource;
the fixed Deployment selector is never edited. The exact live UIDs and content
digests form a predecessor receipt, and the separately signed provider ledger
must commit that receipt's SHA-256 before this root will add anything.
`capture_predecessor_receipt.py` creates that receipt through exact read-only
`kubectl get` calls and descriptor-bound kubeconfig access; it reads no Secret.
The capture must occur before the provider ledger is independently signed.

Do not target resources, remove state entries, use `-replace`, or apply a plan
that destroys or replaces any existing generation. The current remediation is
source-only; no live action is authorized.
