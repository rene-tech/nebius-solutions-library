# SAI-32 privileged-helper boundary

This source candidate separates the node-local GPU allocation observer from
application namespaces and removes its authority to mutate workload Pods.
It also stops placing PostgreSQL DSNs in container environment variables.

The candidate was prepared from commit
`83bcb2d6c7f4dc112e414e00596e0d6b03e22712` (tree
`84f89363a90062432aaa04e23aaf66f69986acfb`). Local `main` had that same
commit and tree at the source audit. SAI-07 candidates
`1351cb2c55b7bc607b55775ae8003a29acfa84c3` and
`e08c904905979cc9d0472fa5e7ecf3f7c3b6e17b` independently introduce the same
namespace boundary but retain Pod patch authority; they are not ancestors of
this candidate and must be reconciled rather than overlaid during integration.

## GPU allocation observer

The observer runs in the Terraform-owned `fs2-node-observability` namespace.
That namespace is the explicit Pod Security Admission exception for the
read-only kubelet device-plugin checkpoint hostPath; application namespaces do
not receive that exception. The container remains uid 0 only because the
kubelet checkpoint is root-owned, while privilege escalation, Linux
capabilities, a writable root filesystem, and the default service-account
mount remain disabled.

In model and scientific workload namespaces its ServiceAccount can only
`get` and `list` Pods. The node-local Pod UID to GPU UUID observation is stored
in a size- and cardinality-bounded ConfigMap in `fs2-node-observability`.
Observer writes and control-plane reads are namespaced there. The serving and
scientific lifecycle readers join the observation by exact Pod UID and node
name. They retain read-only support for legacy Pod annotations during a staged
rollout or rollback, but the new observer never patches a workload Pod.

The observer NetworkPolicy has no ingress. Egress is limited to cluster DNS
and an observer-specific list of exact `/32` or `/128` Kubernetes Service and
ready API endpoint destinations. The chart rejects the IPv4 IMDS address
`169.254.169.254/32`, broader API CIDRs, disabled policy, and an empty API
allowlist when the observer is enabled. The existing, broader control-plane
API egress contract is not inherited by this privileged helper.

This is a source review of the intended pod-network boundary, not live proof.
CNI enforcement, node-local bypass behavior, and actual IMDS reachability must
be verified in the integration rollout before SAI-32 can be accepted live.

## Database DSN delivery

Runtime, schema-wait, migration, bootstrap, model-controller, and maintenance
containers receive `FS2_DATABASE_URL_FILE` only. Each mounts the appropriate
existing Secret key as `/var/run/secrets/fs2-serve/database/url` with mode
`0400`. `Settings` reads at most 16 KiB from that file and validates the same
PostgreSQL scheme before any database connection is attempted. The legacy
`FS2_DATABASE_URL` setting remains accepted for non-chart rollback
compatibility, but the chart no longer injects the DSN as an environment
variable.

## Integration and rollback

This task is a static source candidate. No Helm, Terraform, cluster, cloud,
database, registry, credential, build, test, or live probe was executed.
Integration must reconcile the overlapping SAI-07 namespace/PSS work first.
The staged rollout order is foundation namespace, control-plane image and
chart, then observer. Rollback may restore the prior image/chart and legacy
annotation reader; do not remove the dedicated namespace or observation
ConfigMaps until the rollback has settled and independent acceptance permits
cleanup.

The node-pull project `viewer`, worker security-group Internet egress, and the
upstream DCGM exporter's root/`SYS_ADMIN` posture were evidence recorded by the
review but are not changed by the report's exact SAI-32 remediation. They
remain explicit integration-review items and must not be inferred closed by
this candidate.

## Authored verification

Regression coverage in `components/control-plane/tests/test_helm_chart.py` and
`test_runtime_lifecycle_attribution.py` asserts the dedicated namespace,
read-only workload Pod RBAC, isolated publication contract, IMDS-excluding
NetworkPolicy, file-mounted DSNs, settings loading, and serving lifecycle
consumption. The parent coordinator prohibited executing tests or renderers;
an independent integration worker must run the focused tests, Helm lint/render,
Terraform formatting/validation/plan review, security checks, and staged live
positive/negative probes.
