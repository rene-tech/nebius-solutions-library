# Additive customer-storage compatibility-v3 reconciler

This chart is deliberately separate from the shared control-plane release. It
adds a generation-named reconciler beside the retained fixed predecessor and
never updates the predecessor Deployment selector. Its component label is
`storage-reconciler-v3`, which is outside the deployed fixed and retained v2
VAP matches. Every
object carries Helm's `keep` policy; raw rollback or uninstall is not an
authorized retirement mechanism.

The release name is derived from the signed contract and a rollout generation
whose suffix hashes the image, signed release-values digest, authority,
contract and accepted commit/tree/review custody. Never upgrade an existing release to new content. Add the exact new
generation-named release and retain every predecessor Deployment. No source
action deletes a retained release or customer object; live Pod scale-down is
separately prohibited while the no-delete constraint remains active.

The chart consumes the v12 Kubernetes handoff and the provider authority
handoff. The pod is pinned to the dedicated Nebius node group/security group,
and its init container proves the effective union of every NetworkPolicy that
selects its complete label set, including the controller-assigned actual
`pod-template-hash`. A retained broad policy therefore prevents
readiness. Provider VPC enforcement remains authoritative after readiness.
The chart creates only the generation-named ServiceAccount and Deployment;
the NetworkPolicy inventory Role and RoleBinding are pre-created and retained
by the external security-owner root.

Deployment is fail-closed until an independently accepted SAI-10 custody commit,
tree and review receipt, a separately anchored prior-head receipt, exact
provider IAM inventory, and fresh target-cluster RBAC inventory receipt are
supplied by the signed provider handoff. Historical rejected SAI-10 ancestry is
superseded only by the exact accepted commit/tree/review custody; descendant
status alone is not acceptance evidence. No install, rollback or uninstall is
authorized under the current no-delete constraint.

The generation starts passive. Its 180-second termination grace and pre-stop
drain gate exceed the bounded 120-second provider action timeout. The gate
returns only after the signed drain intent is durable and the storage-only
database reports zero nonterminal provider operations. Requested but unstarted
actions remain durably counted for the successor rather than deadlocking the
drained predecessor. The external cutover fence can then scale the retained
predecessor to zero under separate authorization; it never deletes the
Deployment or customer data.
