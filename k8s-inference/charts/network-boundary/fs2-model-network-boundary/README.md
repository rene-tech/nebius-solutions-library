# Model-network boundary authority

This chart is a Platform Security release, not a subchart of
`fs2-serve-control-plane`. It must be installed and accepted before any SAI-03
`prepare` apply. The control-plane chart hard-disables its preserved embedded
copy.

Release requirements:

- use namespace `fs2-network-security` and a dedicated
  `*/network-boundary-authority@sha256:...` image whose repository and digest
  differ from the control-plane image; build it only from
  `components/control-plane/Dockerfile.network-boundary`, whose dedicated
  entrypoint does not import the general control-plane CLI;
- use Platform-Security-issued X.509 authorizer and transition credentials;
  neither credential may use Kubernetes impersonation;
- bind the transition identity to the exact control-plane Helm rollback
  permissions outside this chart, with no ordinary runtime or model-controller
  subject in that binding;
- keep `fs2-system/fs2-catalog-acquisition` as the sole subject of the narrow
  acquisition Job writer RoleBinding; use a short-lived TokenRequest credential
  for that identity rather than the control-plane runtime or transition
  kubeconfig;
- render `kubernetesApiCidrs` only from canonical `/32` or `/128` API endpoints;
- wait for both replicas, certificate injection, and all nine fail-closed
  hooks, then capture every labeled authority/RBAC/certificate object into the
  v1 custody receipt;
- sign the exact JSON receipt bytes with the offline key whose public-key
  SHA-256 is pinned in `deployment.models.network_policy`; publish the detached
  signature and public key separately from both kubeconfigs.

The supported `inference-stack` wrapper verifies the detached signature,
receipt validity window, kubeconfig hashes and X.509/non-impersonation claims,
live UID/resourceVersion/semantic hashes, dedicated acquisition identity,
digest-pinned authority image, and the exact bounded Helm/Lease/control-plane
hooks before it acquires the transition Lease. The TLS Secret content remains
under Platform Security custody and is never readable by either transition
credential.

Normal control-plane Helm rollouts happen before this authority is bootstrapped.
Once installed, the webhook permanently freezes both Helm release storage and
every object labeled `app.kubernetes.io/instance=fs2-serve-control-plane`, even
when the transition Lease is idle. `prepare`, `inventory`, `enforce`, and deny
removal therefore require a no-op control-plane Helm plan and cannot contain the
old precheck-to-fence race. After a deny-absent receipt, `rollback-helm`
deliberately switches only that Helm provider to the externally custodied
transition credential; the webhook independently requires its unexpired random
Lease holder and rechecks that `fs2-models/default-deny` is absent before
allowing release mutation.

All nine `failurePolicy: Fail` hooks are bounded by an exact namespace and/or
object selector. They cover only profiled `fs2-models` workloads, labeled
SAI-03 policies/markers, the retained transition Lease, the exact control-plane
release, and labeled authority/RBAC/admission objects. An authority outage does
not intercept unrelated namespaces, generic ConfigMaps, generic Leases, or
unrelated admission policies.

The model-policy hook intentionally covers every `NetworkPolicy` only inside
`fs2-models`, without an object selector: a policy author cannot bypass custody
by omitting the boundary label. The marker uses a separate exact-name hook so
ordinary ConfigMaps in that namespace remain outside the authority failure
domain.
