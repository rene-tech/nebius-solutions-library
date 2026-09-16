# Additive customer-storage reconciler v2

This chart is deliberately separate from the shared control-plane release. It
adds a generation-named reconciler beside the retained fixed predecessor and
never updates the predecessor Deployment selector. Its component label is
`storage-reconciler-v2`, which is outside the deployed fixed VAP match. Every
object carries Helm's `keep` policy; raw rollback or uninstall is not an
authorized retirement mechanism.

The release name is derived from the signed contract and a rollout generation
whose suffix hashes the image, authority, contract and accepted custody
receipt. Never upgrade an existing release to new content. Add the exact new
generation-named release and retain every predecessor Deployment and Pod until
a separate deletion authority exists.

The chart consumes the v2 Kubernetes handoff and the provider authority
handoff. The pod is pinned to the dedicated Nebius node group/security group,
and its init container proves the effective union of every NetworkPolicy that
selects its complete label set. A retained broad policy therefore prevents
readiness. Provider VPC enforcement remains authoritative after readiness.

Deployment is fail-closed until an independently accepted SAI-10 custody commit
and review receipt are supplied. The rejected SAI-10 ancestry in the current
SAI-08 branch is not acceptance evidence. No install, rollback or uninstall is
authorized under the current no-delete constraint.
