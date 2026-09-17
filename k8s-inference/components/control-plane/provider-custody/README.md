# External provider custody gateway

This directory is the concrete preventive control used by the SAI-03 network
boundary. It is not installed in Kubernetes. Two or more provider-owned hosts
run the digest-pinned `fs2-provider-custody-gateway` release, which terminates
mutual TLS directly in the same measured process. The managed-cluster public endpoint
allowlist contains only those hosts' exact `/32` or `/128` egress routes.

Uvicorn is configured with `CERT_REQUIRED`, a provider-issued client CA and
TLS 1.3 only. Its connection protocol hashes the certificate DER obtained from
the verified TLS transport into per-connection ASGI state. The application
does not read a certificate, authorization, forwarding or impersonation HTTP
header as client identity. It reconstructs one of the six signed phase
principals and supplies the corresponding exact Kubernetes username and group
set upstream. Policy, upstream CA, upstream token, server certificate/key and
client CA are absolute mode-0600 provider-host files. The policy bytes are
pinned by `FS2_PROVIDER_CUSTODY_POLICY_SHA256`. The retained NGINX template is
an evidence-only fail-closed 503 listener and is not part of policy v4.

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
unknown fields, and another principal all fail closed. Every patch must mutate
exactly one server-clock-bounded `renewTime` and one duration, and both the
gateway's current time plus duration and `renewTime` plus duration must remain
inside the active provider freeze.

The two Leases must therefore exist before endpoint cutover. Bootstrap creates
them once under the separately reviewed provider procedure with the exact
chart metadata and idle spec. Neither the wrapper nor the gateway can recreate
or delete them. The chart continues rendering the same objects so existing
Helm ownership is preserved; any attempted drift causes the gateway to reject
the release rather than weakening the lock.

The signed provider attestation contains the immutable policy digest, exact
provider gateway/firewall/IAM inventory, the current managed-cluster object,
sorted endpoint host routes, six certificate principal bindings, freeze
transaction, operation-lock policy and provider-authority census. It is not
accepted on signature alone. `inference-stack` independently reads the exact
provider project and cluster, enumerates every project instance, security
group and service account, then enumerates every declared gateway firewall
rule and IAM permit. Gateway membership no longer depends on mutable labels:
the declared instances must be exactly the provider instances whose live
public addresses equal the cluster's complete `/32` or `/128` API allowlist.
Each member's full-object digest/resourceVersion, service-account attachment,
public host address, child-rule set and permit set must equal the signed
projection.

A distinct root-owned, digest-pinned provider authority exporter is invoked
twice on every custody verification. Each observation carries unique provider
request IDs and a fresh timestamp; both canonical snapshots must be equal to
the provider-signed snapshot. The snapshot contains the complete
project-to-organization ancestry, users, groups and service accounts at every
scope, every principal's permits including inherited grants, every principal
able to change the MK8s endpoint, gateway compute/network/IAM objects or signing
material, and a provider-native completeness token. A provider-enforced,
self-protecting zero-principal freeze covers those exact resources, its own
freeze object, every enumerated authority permit, and the access-permit,
identity-binding, service-account and signing-material collections at every
inherited scope. This preserves ordinary cloud operators outside the bounded
resources while preventing a new parent-scope grant during the transition and
making endpoint, firewall, IAM and signing-material mutation atomic.

The production exporter source is in `native-exporter/`. Build it with
`CGO_ENABLED=0`, publish it as a provenance-bound Linux artifact, and enroll its
exact SHA-256/source commit/tree in provider custody v8. `inference-stack`
accepts only a root-owned static ELF with no `PT_INTERP`, copies the exact bytes
to a sealed memfd, and runs it with a new minimal environment. The Python entry
point is retained only as a readable protocol reference and test surface; it is
never an admissible custody executable. The native exporter uses direct TLS
1.3 with sealed inherited client credentials, a private provider CA and an
independently pinned live server leaf to call the exact provider-native
effective-authority API. The nonce-bound response must contain the provider
endpoint and server-leaf digest repeated by the signed snapshot, and the
provider response digest repeated by the completeness token; redirects, proxy
environment variables, non-JSON responses and oversized responses fail closed.

The v7 attestation also pins the absolute kubectl and Nebius CLI paths, hashes,
Nebius profile/home, and exact Kubernetes context. The wrapper reads each tool
once through descriptor-relative `O_NOFOLLOW`, executes a sealed copy with no
inherited PATH, loader, Python, proxy, or credential environment, and supplies
only single-read, root-custodied kubeconfig bytes through sealed memfds. Signed
attestations, signatures, trust roots, and phase credentials are verified and
parsed from the same captured bytes; pathname reopens are not authoritative.
The root deployment contract separately pins the exact SHA-256 of the static
`/opt/fs2/bin/fs2-custody-verifier` artifact before either signed document is
trusted. Its reviewed Go source is in `native-verifier/`. The launcher requires
a native ELF with no `PT_INTERP`, executes a sealed copy in a new minimal
environment, and repeats its digest in the signed authority receipt. The tool
uses the statically linked Go crypto/X.509 implementation and has no OpenSSL
configuration, provider-module, engine, shared-library, shell, or interpreter
dependency.

The same provider-native snapshot binds every gateway's exact instance and
measurement resource/version, immutable release, entrypoint, systemd unit/
environment, boot image, non-root executable/process, exact non-loopback listener address/
port/transport and listener measurement to provider attestation. It explicitly
requires `front_proxy_enabled=false` and TLS mode
`in-process-mutual-tls-1.3`; nonce status alone is never runtime provenance.

Every declared member is challenged independently. Before the HTTP challenge,
the verifier performs a fresh mTLS connection and compares the actual leaf
certificate DER digest with that member's signed certificate digest. The
nonce-bound response must report the same complete member set, provider
inventory hash, Kubernetes authorization hash, policy hash, freeze transaction,
and exact six-principal certificate map. A stale HA member, policy, leaf
certificate, route, firewall rule, principal, or permit therefore fails closed.

The Kubernetes-side census is the other half of the boundary. It still
enumerates every Role/ClusterRole and binding capable of identity minting,
Secret reads, exec, or mutation of Services, Secrets, ServiceAccounts,
NetworkPolicies, Pods and Pod-producing controllers. Those broad bindings are
detective evidence and ordinary controllers retain their reconciliation
authority. Receipt-bound namespaced objects are selected by the fail-closed
static-custody VAP using old-or-new authority labels plus exact scientific
writer and run-scoped JobSet-controller identities. Only bindings with direct
mutation authority over the five VAP/VAPBinding/VWC objects Kubernetes excludes
from self-admission must resolve exclusively to the six external X.509 users.
This closes the in-cluster bypass without deauthorizing the deployment,
ReplicaSet, Job, JobSet or Endpoint controllers.

Before the first mutating stage, the wrapper verifies provider custody v8 and
authority receipt v4, fixes the signed cluster/context/tool/credential epoch,
and refuses bootstrap through an unguarded shared kubeconfig. Terraform also
refreshes `nebius_mk8s_v1_cluster` and the
named provider resources and requires the live resourceVersion, complete
endpoint allowlist, labels, and semantic digests to equal the signed receipt.
Immediately before apply, `inference-stack` repeats provider enumeration and
every member challenge. During apply it repeats the full custody verifier every
five seconds, accepts only a later expiry for the same stable transaction and
policy, terminates the apply before the remaining window falls below 90
seconds, and repeats custody after a successful apply. Terraform runs in a new
process group. The wrapper installs scoped `SIGINT` and `SIGTERM` handlers
before `Popen`, converts either signal into the same exceptional-exit path, and
restores the prior handlers only after the process group is proven dead. An
outer `BaseException` fence also covers `KeyboardInterrupt`, custody failures,
nonzero exits and unexpected post-spawn failures. Every such path ignores
repeat catchable termination while it `SIGSTOP` fences, `SIGKILL`s and reaps
the complete credential-bearing group; any inability to prove the group dead
leaves the provider marker unresolved and blocks every later mutation. Before
that process starts, the wrapper opens a marker in a
provider-native, multi-AZ, append-only CAS journal. The provider signs the full
marker, including the exact source commit/tree, saved-plan digest, derived
postcondition digest, credential epochs and freeze transaction. Local files in
the run directory are diagnostic only: they never resolve or suppress a
provider record, and any legacy marker or resolution there must be a
root-owned, non-writable, single-link regular file or the wrapper fails closed.
On custody failure the whole credential-bearing group is `SIGSTOP` fenced and
then `SIGKILL`ed while still stopped; it is never resumed for graceful
termination. The pre-existing external marker remains unresolved, so a local
empty file, symlink, rewritten marker or forged self-hash cannot authorize a
later mutation. The separate
`reconcile-indeterminate` command requires a fresh signed recovery credential
and custody transaction, asks the static provider exporter for the complete
operation set correlated by the prior apply ID, credential epoch and Terraform
user-agent, waits for two stable terminal observations, applies only a
refresh-only state plan, and compares refreshed state with the exact original
saved-plan postconditions. Only then may a resourceVersion-CAS append the
provider-signed resolution bound to the original marker digest, a distinct
recovery transaction, provider settlement, refresh plan, refreshed state and
target postconditions. The provider journal retains the complete ordered event
history without deletion. Mutation gates read only the bounded, globally sorted
unresolved view, in pages of at most 256 records, from one immutable signed
checkpoint. Exact ordinals and chained range accumulators must converge to that
checkpoint's signed unresolved count/root; the same checkpoint also binds the
append-only event count/root and previous-checkpoint link. Thus no unresolved
record can be omitted and no fixed total-history response limit can exhaust the
service at record 4097. The journal service configuration and signing material
are held by the provider freeze;
the record store separately permits only service-mediated create and one
resourceVersion-CAS resolution append, never update, replacement or deletion.
Every resolution retains the marker's immutable open revision in
`marker_journal_resource_version`, but its
`journal_previous_resource_version` is the exact current revision supplied by
that resolve request. The signed resolution revision and response checkpoint
must equal that current predecessor plus one. Intervening journal events can
therefore precede recovery without either replaying a stale predecessor or
detaching the resolution from its original marker.
`SIGKILL` cannot be caught or handled by a userspace wrapper. If the wrapper
itself receives `SIGKILL` after spawning Terraform, the external marker remains
open and prevents a later controlled mutation, but the wrapper cannot claim
that the process group was fenced or that no already-authorized remote request
continued. That state is indeterminate and requires the explicit provider
settlement and recovery protocol; this source contract does not describe
`SIGKILL` as locally contained.
A normal successful apply uses the same settlement and postcondition
proof before appending its signed completion. A two-hour assertion
is therefore only a maximum renewal envelope, not permission for an unbounded
unwatched apply or ambiguous continuation.

Provider rollout requirements, intentionally not executed by this source-only
task:

1. Independently review and publish a digest-pinned control-plane package.
2. Provision at least two provider-owned gateway hosts in separate failure
   domains and install the supplied systemd unit with direct TLS 1.3 and the
   provider-issued client CA. Do not install a front proxy.
3. Publish the provider-native authority exporter and enroll its immutable
   provenance plus every gateway runtime/process/listener measurement.
   Provision and enroll the provider-native append-only apply journal, its
   no-delete/CAS policy, durable resource identity and receipt-signing key.
4. Create the two idle operation Leases before restricting API access.
5. Write and sign the exact gateway policy and provider custody attestation,
   including the complete provider enumeration, two stable authority
   observations, provider freeze, runtime measurements and each gateway TLS leaf.
6. Restrict the cluster endpoint to the sorted gateway host routes, capture the
   resulting cluster resourceVersion, and issue phase kubeconfigs whose server
   is the gateway URL.
7. Prove denial for a frozen object, collection DELETE, wrong-principal lock
   patch, missing-CAS patch, and direct endpoint access; prove that no
   in-cluster RBAC subject can mutate the five excluded admission guards while
   ordinary controllers still reconcile; and exercise custody renewal across a
   bounded apply before any SAI-03 prepare phase.
8. Prove journal begin-before-process, nonzero-exit process-group fencing,
   failed-apply persistence, forged local marker/resolution rejection,
   stale-resourceVersion rejection, signed fresh recovery, exact
   state-postcondition equality, multi-page range continuity and terminal
   checkpoint equality beyond 4,096 retained history events.

There is no destructive break-glass operation. A future recovery policy is a
new signed, versioned provider policy and in-place update; it never deletes a
guard or operation Lease. This task supplies source and contracts only and
does not claim that the provider resources exist or that live custody is GO.
