# External provider custody gateway

This directory is the concrete preventive control used by the SAI-03 network
boundary. It is not installed in Kubernetes. Two or more provider-owned hosts
run the digest-pinned `fs2-provider-custody-gateway` release behind the
same-host mTLS NGINX configuration. The managed-cluster public endpoint
allowlist contains only those hosts' exact `/32` or `/128` egress routes.

The gateway terminates no client TLS itself. NGINX verifies the provider-issued
client certificate, removes every inbound authorization, impersonation, and
forwarding header, and passes only the escaped verified certificate to the
loopback ASGI process. The process reconstructs one of the six signed phase
principals and supplies the corresponding exact Kubernetes username and group
set upstream. Policy, upstream CA, upstream token, and TLS key material are
absolute mode-0600 provider-host files. The policy bytes are pinned by
`FS2_PROVIDER_CUSTODY_POLICY_SHA256`.

During a transition the policy's `mutation_freeze` list is the complete
receipt-bound Kubernetes inventory. Every resolved CREATE, UPDATE, PATCH, or
DELETE of a listed object is rejected before kube-apiserver, for every
principal. Frozen RBAC collection prefixes additionally reject creation,
mutation, or deletion of any Role, RoleBinding, ClusterRole, or
ClusterRoleBinding while the exhaustive census is being sealed. Unresolvable
and collection-wide mutations are also rejected. The
two retained operation Leases are deliberately outside that frozen list and
use a separate channel: only their distinct transition or maintenance
principal may send an exact JSON Patch containing one resourceVersion CAS;
CREATE, DELETE, merge patch, strategic merge patch, PUT, overlong duration,
unknown fields, and another principal all fail closed.

The two Leases must therefore exist before endpoint cutover. Bootstrap creates
them once under the separately reviewed provider procedure with the exact
chart metadata and idle spec. Neither the wrapper nor the gateway can recreate
or delete them. The chart continues rendering the same objects so existing
Helm ownership is preserved; any attempted drift causes the gateway to reject
the release rather than weakening the lock.

The signed provider attestation contains the immutable policy digest, exact
provider gateway/firewall/IAM inventory, the current managed-cluster object,
sorted endpoint host routes, six certificate principal bindings, freeze
transaction, and operation-lock policy. It is not accepted on signature alone.
`inference-stack` independently reads the exact provider project and cluster,
enumerates every project instance, security group, and service account, then
enumerates every declared gateway firewall rule and IAM permit. The declared
members must equal the complete set carrying both fixed custody labels
`security-boundary=model-network-provider-custody` and the exact cluster ID;
each member's full-object digest/resourceVersion, service-account attachment,
public host address, child-rule set, and permit set must equal the signed
projection. The managed-cluster endpoint allowlist must contain exactly those
members' `/32` or `/128` routes, closing undeclared old routes and gateways.

Every declared member is challenged independently. Before the HTTP challenge,
the verifier performs a fresh mTLS connection and compares the actual leaf
certificate DER digest with that member's signed certificate digest. The
nonce-bound response must report the same complete member set, provider
inventory hash, Kubernetes authorization hash, policy hash, freeze transaction,
and exact six-principal certificate map. A stale HA member, policy, leaf
certificate, route, firewall rule, principal, or permit therefore fails closed.

The Kubernetes-side census is the other half of the boundary. It enumerates
every Role/ClusterRole and binding capable of mutating admission, RBAC,
credentials, Services, Secrets, ServiceAccounts, NetworkPolicies, or any
Pod-producing controller/Pod. While custody is asserted, every such binding
must resolve only to one of the six external X.509 usernames. A ServiceAccount,
Group, default cluster-admin/system:masters binding, or other in-cluster writer
fails the gate even if it appears in the signed broad census. This is how the
source closes the `kubernetes.default.svc` path that a public reverse proxy
cannot mediate; the gateway is not claimed to be an API-server deny by itself.

Before every plan, Terraform also refreshes `nebius_mk8s_v1_cluster` and the
named provider resources and requires the live resourceVersion, complete
endpoint allowlist, labels, and semantic digests to equal the signed receipt.
Immediately before apply, `inference-stack` repeats provider enumeration and
every member challenge. During apply it repeats the full custody verifier every
five seconds, accepts only a later expiry for the same stable transaction and
policy, terminates the apply before the remaining window falls below 90
seconds, and repeats custody after a successful apply. A two-hour assertion is
therefore only a maximum renewal envelope, not permission for an unbounded
unwatched apply.

Provider rollout requirements, intentionally not executed by this source-only
task:

1. Independently review and publish a digest-pinned control-plane package.
2. Provision at least two provider-owned gateway hosts in separate failure
   domains, label every gateway instance, security group, and service account
   with the fixed custody labels, and bind ASGI only to loopback using the
   supplied systemd template.
3. Install the NGINX template with TLS 1.3 and provider-issued client CA.
4. Create the two idle operation Leases before restricting API access.
5. Write and sign the exact gateway policy and provider custody attestation,
   including the complete provider enumeration and each gateway TLS leaf.
6. Restrict the cluster endpoint to the sorted gateway host routes, capture the
   resulting cluster resourceVersion, and issue phase kubeconfigs whose server
   is the gateway URL.
7. Prove denial for a frozen object, collection DELETE, wrong-principal lock
   patch, missing-CAS patch, and direct endpoint access; prove that no
   in-cluster RBAC subject retains a mutation path; and exercise custody renewal
   across a bounded apply before any SAI-03 prepare phase.

There is no destructive break-glass operation. A future recovery policy is a
new signed, versioned provider policy and in-place update; it never deletes a
guard or operation Lease. This task supplies source and contracts only and
does not claim that the provider resources exist or that live custody is GO.
