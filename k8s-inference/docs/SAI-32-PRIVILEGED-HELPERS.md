# SAI-32 privileged-helper boundary

This source candidate separates the node-local GPU allocation observer from
application and DCGM namespaces, removes its authority to mutate workload
Pods, and gives its remaining publication write an API-enforced object-custody
boundary. It also stops placing PostgreSQL DSNs in container environment
variables.

The first candidate, commit
`b029764894e67f367572d6e86f46e20ca6974668` (tree
`0fd8ebb8447d29399594253544226710406bf172`), is preserved as rejected source.
Independent static review found that its shared namespace and namespace-wide
ConfigMap verbs could affect DCGM objects, that node publications had no
retirement or total bound, that the foundation resource count was stale, and
that endpoint-IP snapshots were not rotation-safe after Service DNAT. This
direct-descendant correction addresses those findings; it does not convert the
rejected commit into positive evidence.

Both candidates descend from commit
`83bcb2d6c7f4dc112e414e00596e0d6b03e22712` (tree
`84f89363a90062432aaa04e23aaf66f69986acfb`). SAI-07 candidates
`1351cb2c55b7bc607b55775ae8003a29acfa84c3` and
`e08c904905979cc9d0472fa5e7ecf3f7c3b6e17b` independently introduce
`fs2-node-observability` for DCGM and retain Pod patch authority; they are not
ancestors of this candidate and must be reconciled rather than overlaid during
integration.

## GPU allocation observer

The observer runs alone in the Terraform-owned
`fs2-gpu-allocation-observer` namespace. DCGM remains in the separate
`fs2-node-observability` namespace when SAI-07 is integrated. The observer
namespace is the explicit Pod Security Admission exception for one read-only
host file,
`/var/lib/kubelet/device-plugins/kubelet_internal_checkpoint`; the containing
device-plugin directory and its Unix sockets are not mounted. Application and
DCGM namespaces do not receive this exception from SAI-32. The container
remains uid 0 only because the checkpoint is root-owned, while privilege
escalation, Linux capabilities, a writable root filesystem, and the default
service-account mount remain disabled.

In model and scientific workload namespaces its ServiceAccount can only
`get` and `list` Pods. The node-local Pod UID to GPU UUID observation is stored
in a size- and cardinality-bounded ConfigMap in the exclusive observer
namespace. Raw RBAC grants that ServiceAccount `get/create/update` ConfigMaps
there, while a fail-closed `ValidatingAdmissionPolicy` and binding restrict
those verbs to the exact bound node name and publication schema. The policy
uses the platform's declared Kubernetes 1.35 contract and requires the node
name and same-namespace Pod owner reference to match the node, Pod name, and
Pod UID claims in the bound projected service-account token. It also makes an
existing publication's node binding immutable and
reserves every non-CA ConfigMap in the namespace exclusively for the observer
identity. A compromised observer therefore cannot overwrite DCGM or arbitrary
ConfigMaps and cannot publish for another node.

Each publication is owned by the current observer Pod, has a five-to-300-second
lifetime, and is rejected by serving and scientific readers after expiry. Pod
garbage collection retires publications after node/Pod churn; a ResourceQuota
caps the namespace at 257 ConfigMaps (256 publications plus the namespace CA
ConfigMap) if garbage collection is delayed. Each object name equals its bound
node name, which CEL can compare directly to the projected token claim; the
runtime refuses new objects once that finite ceiling is reached.
Serving and scientific lifecycle readers join an accepted observation by exact
Pod UID and node name. They retain read-only support for legacy Pod annotations
during a staged rollout or rollback, but the new observer never patches a
workload Pod.

The observer Kubernetes `NetworkPolicy` has no ingress and permits only
cluster DNS. A companion Cilium policy permits the rotation-safe
`kube-apiserver` entity on TCP 443. Chart schema, template, settings, and
publisher all require the exact origin
`https://kubernetes.default.svc:443`; an arbitrary host or port cannot be
configured. No endpoint-IP snapshot, broad CIDR, or IMDS route is granted to
the privileged helper, and it does not inherit the existing broader
control-plane API egress contract.

This is a source description of the intended pod-network boundary, not live
proof. Cilium `kube-apiserver` entity behavior, node-local bypass behavior, API
endpoint rotation, and actual IMDS reachability must be verified during a
future authorized integration rollout before SAI-32 can be accepted live.

## Database DSN delivery

Runtime, schema-wait, migration, bootstrap, model-controller, and maintenance
containers receive `FS2_DATABASE_URL_FILE` only. Each mounts the appropriate
existing Secret key as `/var/run/secrets/fs2-serve/database/url` with mode
`0400`. `Settings` opens the path once, verifies the resulting descriptor is a
regular file, and performs a capped read of at most 16 KiB from that same
descriptor before validating the PostgreSQL scheme. A path swap therefore
cannot redirect the later read. The legacy `FS2_DATABASE_URL` setting remains
accepted for non-chart rollback compatibility, but the chart no longer
injects the DSN as an environment variable.

## Integration and rollback

This task is a static source candidate. No Helm, Terraform, cluster, cloud,
database, registry, credential, build, test, or live probe was executed.
Integration must reconcile SAI-07 first and preserve SAI-32's `get/list`-only
workload-Pod RBAC, exclusive observer namespace, publication policy, and PSS
exception. This standalone branch changes the foundation base managed-resource
count from 31 to 32 for its namespace. An integration containing both the
separate SAI-07/DCGM namespace and this namespace must use 33; it must not
coalesce the two namespaces to keep the count at 32.

The future staged rollout order is the foundation namespace and admission
support, the control-plane image and chart, then the observer. Rollback may
restore the prior image/chart and legacy annotation reader. Namespace or
publication retirement is intentionally outside this static task and requires
independent acceptance and the parent program's deletion authority.

The node-pull project `viewer`, worker security-group Internet egress, and the
upstream DCGM exporter's root/`SYS_ADMIN` posture were evidence recorded by the
review but are not changed by the report's exact SAI-32 remediation. They
remain explicit integration-review items and must not be inferred closed by
this candidate.

## Authored verification

Regression coverage in `components/control-plane/tests/test_helm_chart.py`,
`test_runtime_lifecycle_attribution.py`, and `tests/test_deployment_contract.py`
asserts namespace exclusivity, read-only workload Pod RBAC, token-bound
publication custody, owner/expiry/quota bounds, exact API origin and port,
rotation-safe API policy intent, single-file checkpoint mounting,
descriptor-bounded DSN loading, lifecycle refusal of expired publications, and
the corrected foundation count. The parent coordinator prohibited executing
tests or renderers; an independent integration worker must run the focused
tests, Helm lint/render, Terraform formatting/validation/plan review, security
checks, and staged live positive/negative probes.
