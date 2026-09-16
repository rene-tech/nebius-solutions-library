# Customer-storage egress security boundary

This is a separate security-owner Terraform root. It is not a child module of
the ordinary workloads stage and must use a different kubeconfig/credential.
It owns the validating admission boundary plus immutable, generation-named
public trust, signed contract, and NetworkPolicy objects.

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
`auth can-i` checks for both kubeconfigs; only subject hashes enter Terraform.
Operator approval of that identity split and read-only live verification of all
retained objects are required before any apply. This source root does not
constitute that approval.

Do not target resources, remove state entries, use `-replace`, or apply a plan
that destroys or replaces any existing generation. The current remediation is
source-only; no live action is authorized.
