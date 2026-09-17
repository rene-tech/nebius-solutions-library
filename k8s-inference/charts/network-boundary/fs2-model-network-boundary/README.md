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

Two API-server-native `ValidatingAdmissionPolicy` objects cover Kubernetes
resource kinds the API server permits them to match. They do not call this
chart's Service, but cannot match or protect VAP/VAPBinding/VWC objects
themselves:

- `fs2-model-network-static-custody` makes matching authority resources
  update-only, selects protected labels from old **or** new objects, and admits
  repair only from external custodian/recovery identities. cert-manager may
  rotate only two exact labeled TLS Secrets and exact status subresources. No
  in-cluster cainjector may mutate the VWC.
- `fs2-model-network-impersonation-guard` prevents new grants for
  impersonation, RBAC bind/escalate, CSR approve/sign, ServiceAccount token,
  Secret, or other credential-mint authority, and rejects new principals or
  bindings to receipt-enumerated identity-mint roles. Unchanged pre-existing
  wildcard roles remain operable, while the provider assertion and full RBAC
  census bind their exact live grants and bindings.

Before installation, the external gateway implementation in
`components/control-plane/provider-custody/` must run on at least two
provider-owned hosts behind its mTLS NGINX boundary. Those hosts' exact sorted
`/32` or `/128` egress routes must be the complete managed-cluster public API
allowlist. Its digest-pinned policy denies mutation of the signed full
inventory before kube-apiserver and rejects unresolvable or collection-wide
writes. The provider/IAM v3 assertion binds the live policy digest, gateway,
firewall and endpoint-access resource IDs, cluster resourceVersion, host
routes, six certificate principals, and status-challenge response. The
in-cluster objects remain defense in depth and detective equality, never a
self-custody claim.

After the authority is installed and ready, and before either receipt snapshot,
the provider must activate the assertion's bounded mutation transaction. Its
protected-resource inventory is exactly the complete authority receipt
inventory, its allowed-principal list is empty, and CREATE, UPDATE, DELETE, and
REPLACE are all denied until after the saved-plan operation. The receipt binds
the transaction ID, expiry, and resource-set digest. This makes the two reads
and immediate pre-apply revalidation one externally serialized snapshot rather
than a cooperative in-cluster convention.

The signed authority v3 receipt must be no more than 15 minutes old. It binds every
protected object's UID, resourceVersion and complete semantic hash; the full
Role/ClusterRole and binding census; every role able to mint identity, read
Secrets, read or mutate workloads, or use exec/attach/port-forward paths and
every binding to those roles; exact credential username/group/extra/provider
claims; the distinct acquisition and scientific writers; the exact run-scoped
JobSet controller; the live authority image; both TLS Secrets; the exact webhook
CA; the Service and Endpoints identity; and every distinct-node Pod's live
readiness/served-certificate hash. The wrapper takes two identical projections
and repeats full verification immediately before applying a saved plan.
The chart does not provision the provider hosts or change cluster endpoint
access. Independent review must validate the repository gateway source and the
exact deployed resource IDs, policy digest, endpoint resourceVersion, host
routes, mTLS status challenge, and direct-access denial. A locally authored
JSON assertion, even if schema-valid, is not rollout evidence.

Outside an active mutation transaction, break glass is non-destructive: the
external recovery identity may update the
static guard, certificate, Deployment, Service, RBAC, or webhook configuration
in place under the provider policy. It cannot delete or replace a protected
guard. The eight in-cluster webhooks do not match their own authority
Deployment, Service, RBAC, or certificate objects. One release hook matches
every exact signed tuple regardless of labels/caller, and a second matches every
write from the three release identities. Recovery and TLS Secret rotation use
the API-server policy/provider path, so an authority outage does not create a
self-repair deadlock.

## Runtime and maintenance boundary

The eight `failurePolicy: Fail` hooks cover only profiled `fs2-models`
workloads, all `fs2-models` NetworkPolicies, the exact marker, both retained
phase Leases, exact control-plane Helm storage/release objects, and mutations
made by the three external release identities. They do not intercept
unrelated namespaces or generic cluster resources.

The API-server-native profile bindings additionally cover the entire
`fs2-models` namespace so a controller cannot evade the webhook by omitting a
profile. Every profiled parent and Pod must be token-free, non-root, use
RuntimeDefault seccomp, disable hostNetwork/hostPID/hostIPC and hostPort,
disable privilege escalation, drop `ALL` capabilities, and add no capability
except reviewed ModelExpress `IPC_LOCK`. `hostPath` is rejected except for the
single published `/mnt/fs2-reference-data/data` `Directory`, which must be
mounted read-only without propagation; the immutable scientific execution map
further binds each reference-data subPath. The inventory verifier checks the
same envelope before default deny can be enforced.

The transition and maintenance Leases are retained provider-precreated objects,
not members of the zero-principal frozen inventory. They have a separate
gateway channel: only the corresponding principal can submit exact JSON Patch,
with one resourceVersion CAS and a bounded holder/duration. CREATE, DELETE,
PUT, merge/strategic patches and unrecognized fields are denied. The phase
wrapper fails closed if either Lease is absent and re-reads its resourceVersion
before releasing the holder.

The normal `maintenance` phase keeps the enforcement marker, apply fence,
finite allow profiles, and `fs2-models/default-deny` live. A distinct random
maintenance Lease holder admits the exact control-plane Helm release only;
transition and maintenance Leases cannot be active together. Rollback uses the
separate transition identity and may change Helm only after a signed receipt
and live API check prove default deny absent.

The authority runs at least two replicas with required hostname anti-affinity,
`DoNotSchedule` hostname/zone topology spread, `minReadySeconds`, and a PDB that
requires two available replicas. Readiness verifies its projected reader
credential, both retained Leases, CA material, serving keypair, and currently
loaded certificate. Certificate projection changes are hot-loaded into the
serving SSL context; no simultaneous liveness restart is required.

`fs2-system/fs2-catalog-acquisition` remains the sole public-acquisition Job
writer. Internal scientific Job/JobSet mutation uses the separate
`fs2-system/fs2-scientific-job-writer` ServiceAccount behind a bounded proxy;
the general runtime ServiceAccount is read-only for workload observation.
