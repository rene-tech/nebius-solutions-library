# Model-network boundary authority

This is a separately released Platform Security chart. It must be accepted
before any SAI-03 `prepare` apply. The control-plane chart's historical embedded
authority is hard-disabled.

## Independent custody

The authority image must be an immutable
`*/network-boundary-authority@sha256:...` artifact that differs from the
control-plane image. Install the chart in `fs2-network-security` with distinct,
externally issued Nebius IAM identities for custody, recovery, audit,
authorization, transition, and maintenance. The values bind their exact
Kubernetes usernames and group sets to provider principal IDs and a trust
domain; neither a shared deployment kubeconfig nor an in-cluster ServiceAccount
is a valid substitute.

Two API-server-native `ValidatingAdmissionPolicy` objects provide the static
boundary. They do not call this chart's Service:

- `fs2-model-network-static-custody` makes authority resources update-only and
  admits repair only from the external custodian/recovery identities. Exact
  cert-manager Secret/Certificate/Issuer rotation and cainjector caBundle-only
  updates are explicit exceptions.
- `fs2-model-network-impersonation-guard` prevents new grants for
  `impersonate`/`*` over users, groups, serviceaccounts, uids, userextras, or
  `*`, and rejects bindings to receipt-enumerated impersonation-capable roles.

The signed v2 receipt must be no more than 15 minutes old. It binds every
protected object's UID, resourceVersion and complete semantic hash; the full
Role/ClusterRole and binding census; all impersonation-capable roles and the
absence of bindings to them; exact credential username/group/extra/provider
claims; the distinct acquisition and scientific writers; the live authority
image ID; both TLS Secret byte hashes; and the exact webhook CA bundle. The
wrapper re-reads all of that live with a distinct read-only auditor credential
before a phase Lease can be acquired.

Break glass is non-destructive: the external recovery identity may update the
static guard, certificate, Deployment, Service, RBAC, or webhook configuration
in place. It cannot delete a protected authority object. The eight in-cluster
webhooks invoke their release-writer hooks only for the separate transition and
maintenance identities; the recovery and cert-controller identities are
authorized directly by API-server-native custody, so an authority outage does
not deadlock repair or certificate renewal.

## Runtime and maintenance boundary

The eight `failurePolicy: Fail` hooks cover only profiled `fs2-models`
workloads, all `fs2-models` NetworkPolicies, the exact marker, both retained
phase Leases, exact control-plane Helm storage/release objects, and mutations
made by the two external release-writer identities. They do not intercept
unrelated namespaces or generic cluster resources.

The normal `maintenance` phase keeps the enforcement marker, apply fence,
finite allow profiles, and `fs2-models/default-deny` live. A distinct random
maintenance Lease holder admits the exact control-plane Helm release only;
transition and maintenance Leases cannot be active together. Rollback uses the
separate transition identity and may change Helm only after a signed receipt
and live API check prove default deny absent.

The authority runs at least two replicas with preferred hostname anti-affinity
and hostname/zone topology spread. Readiness verifies its projected reader
credential, both retained Leases, CA material and serving keypair. A serving
certificate change fails liveness so a rolling restart reloads the new keypair.

`fs2-system/fs2-catalog-acquisition` remains the sole public-acquisition Job
writer. Internal scientific Job/JobSet mutation uses the separate
`fs2-system/fs2-scientific-job-writer` ServiceAccount behind a bounded proxy;
the general runtime ServiceAccount is read-only for workload observation.
