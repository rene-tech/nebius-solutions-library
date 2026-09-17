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
provider gateway/firewall/cluster-endpoint resource IDs, the current managed
cluster resourceVersion, sorted endpoint host routes, six certificate
principal bindings, freeze transaction, and operation-lock policy. Before
every plan, Terraform refreshes `nebius_mk8s_v1_cluster` and requires the live
resourceVersion and complete endpoint allowlist to equal the signed receipt.
Immediately before apply, `inference-stack` performs a fresh nonce-bound mTLS
status challenge and requires byte-for-byte equality with the signed policy.

Provider rollout requirements, intentionally not executed by this source-only
task:

1. Independently review and publish a digest-pinned control-plane package.
2. Provision at least two provider-owned gateway hosts in separate failure
   domains and bind ASGI only to loopback using the supplied systemd template.
3. Install the NGINX template with TLS 1.3 and provider-issued client CA.
4. Create the two idle operation Leases before restricting API access.
5. Write and sign the exact gateway policy and provider custody attestation.
6. Restrict the cluster endpoint to the sorted gateway host routes, capture the
   resulting cluster resourceVersion, and issue phase kubeconfigs whose server
   is the gateway URL.
7. Prove denial for a frozen object, collection DELETE, wrong-principal lock
   patch, missing-CAS patch, and direct endpoint access before any SAI-03
   prepare phase.

There is no destructive break-glass operation. A future recovery policy is a
new signed, versioned provider policy and in-place update; it never deletes a
guard or operation Lease. This task supplies source and contracts only and
does not claim that the provider resources exist or that live custody is GO.
