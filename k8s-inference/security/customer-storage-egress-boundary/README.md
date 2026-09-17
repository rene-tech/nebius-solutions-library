# Customer-storage egress security boundary

This is the Kubernetes defense-in-depth companion to the separate Nebius
provider authority in `security/customer-storage-egress-authority`. It is not
a child module of the ordinary workloads stage and must use a different
kubeconfig/credential. It owns immutable, generation-named public trust,
signed contract, and NetworkPolicy objects plus a secondary validating policy.
The provider VPC security group and target-cluster tainted node group remain
the canonical boundary even if Kubernetes admission is bypassed.

The inputs are append-only. Contract, trust, and boundary generation entries
must never be removed or edited after apply. A rotation adds a new generation,
applies it with the security-owner identity, and passes `current_handoff` to a
later workloads plan. Old resources remain protected by `prevent_destroy` and
old admission bindings remain active. This overlap is intentional: no rollout
or rollback requires deleting or replacing a security object.

Every boundary generation is create-only and admits additions only
for authenticated members of
`fs2:customer-storage-egress-security-owner`. The workloads and release
identities must not be members of that group or mutate admission generations.
Every exact kubeconfig is root-owned on a read-only filesystem; its
descriptor-read digest, authenticated subject, category and provider principal
must match the signed exhaustive inventory. Permission probes cover Secrets,
exec/attach/port-forward, ephemeral containers, service-account tokens, CSR
approval/signing, workload and service-account mutation, impersonation, RBAC
bind/escalate/delegation and webhook mutation. The v2 Helm chart is wired here
as an append-only, generation-named release using the separately inventoried
release identity and ConfigMap Helm driver; no operator-side install is
canonical. The dependency gate also rejects the plan unless
`HELM_DRIVER=configmap`, so this requirement cannot remain a comment-only
operator convention.
The owner also hashes every live Role, RoleBinding, ClusterRole and
ClusterRoleBinding, including subjects, rules, UIDs and resource versions; the
result and the derived binding-to-role effective-authority graph must equal the
fresh independently signed target-cluster RBAC receipt. Namespace-scoped
permission probes enumerate every observed namespace separately; they do not
use `--all-namespaces` as an authority shortcut. Impersonation probes use the
core `users`, `groups`, and `serviceaccounts` resources and also cover Pod
binding/eviction, node proxy/status, TokenRequest, RBAC delegation, and
workload-controller pivots.
Every User and Group subject must resolve to an authenticated identity and its
exact signed group set; every ServiceAccount subject must resolve to the signed
ServiceAccount inventory. Provider access and mutation sets come from the
signed provider-native effective-authority graph, including inherited,
federated and external principals, rather than candidate declarations.
ServiceAccount and Kubernetes-native User/Group memberships are derived from
Kubernetes identity rules rather than accepted from the signed payload. Their
direct-plus-group authority is then recomputed from the live RBAC graph.
Dangerous capabilities are rejected unless they are in the narrow source-coded
Deployment, ReplicaSet, DaemonSet, scheduler, security-owner, or release
admission contract; the signed dangerous list is evidence, not authorization.
Every authenticated owner, workloads, release, human, break-glass and other
identity is evaluated through the same graph, including resource-name-limited
rules. Wildcards and resource names are evaluated semantically for
Secret, ConfigMap, Pod/exec/attach/binding, TokenRequest, node, workload,
NetworkPolicy, RBAC bind/escalate, impersonation, admission and CSR pivots;
matching the cluster-wide RBAC hash alone is insufficient.
`capture_kubernetes_rbac_inventory.py` emits only the canonical unsigned body
through descriptor-bound, read-only Kubernetes calls; the separate checkpoint
owner signs and installs it on the authority's read-only anchor.

Admission matching evaluates empty, partial and negative selectors against the
exact current Pod generation with Kubernetes semantics. The controller-added
`pod-template-hash` is read from each actual Pod and supplied to both the
readiness verifier and the post-rollout workloads proof; `Exists`, `In`,
`NotIn`, and negative selectors therefore cannot hide a widening policy. The
policy permits only the exact content-bound NetworkPolicy to be
created by the security owner; updates, deletion, and later widening selecting
policies are denied at admission, closing the post-init race. This
admission layer remains defense in depth: the target-cluster node group's
provider VPC security group and exact provider IAM inventory are the canonical
boundary.

Every retained legacy and v3 policy and Deny binding is listed with its full canonical
spec in the separately signed prior boundary checkpoint. Before a successor is
created, both this root and the workloads root read every listed live object
and require exact equality. A drifted older generation therefore cannot hide
behind Terraform `ignore_changes`.

A second content-bound policy covers Pods, ServiceAccounts, Deployments,
ReplicaSets, DaemonSets, StatefulSets, Jobs and CronJobs. The release identity
can create only the exact token-blind ServiceAccount and Deployment. Only the
signed Deployment controller may create its exact ReplicaSet child, and only
the signed ReplicaSet controller may create the corresponding Pods. Generated
Pods must keep the signed image, generation labels, protected node target,
Secret and image-pull-secret allowlists; projected Secrets, host paths, PVCs,
CSI volumes, `spec.nodeName`, and additional secret-backed environment sources
are denied.
The workload policy has no namespace exemption. It evaluates whether a Pod can
reach the single signed protected node: direct `nodeName` must equal that node;
otherwise every `nodeSelector` entry and every requirement in at least one
required node-affinity term must match the complete signed scheduling-label and
node-name projection, and the Pod must tolerate the lane's `NoSchedule` taint.
Terms are ORed and their label/field requirements are ANDed with Kubernetes
`In`, `NotIn`, `Exists`, `DoesNotExist`, `Gt`, and `Lt` semantics. Keyed
`Equal`/`Exists` tolerations, omitted effects, and a keyless blanket `Exists`
toleration are therefore all guarded when they make the exact node schedulable.
Only the exact signed CSI, OTel, node-exporter, and lane-observer identities may
use their inventoried blanket scheduling path.

The provider ledger binds five exact DaemonSets: the lane-specific OTel and GPU
observers plus the retained filesystem CSI, Prometheus node-exporter, and OTel
node agents. Each entry fixes namespace, name, live UID, canonical spec digest,
separately inventoried release owner, and the DaemonSet-controller child owner
reference. The retained agents keep their recorded blanket toleration but may
not claim the lane key; only their exact identity/spec is accepted when the
controller adds per-node affinity. This preserves storage and telemetry without
granting a namespace-wide or generic DaemonSet exception. UPDATE evaluates both
`object` and `oldObject`, so entering or leaving any protected scheduling path
cannot evade the policy. Pod binding subresources are matched cluster-wide and
accepted only from the provider-bound scheduler. Retained policies protect
their own retained node groups without selecting later exact storage or
observer workloads because selector and taint keys carry the signed lane ID.

Retained Deny policies are never overridden, edited, disabled or deleted. The
non-destructive handoff is ordered: first create lane-named OTel and GPU
observer successors using the future lane-unique key while no matching
node exists; next capture and independently sign their exact UIDs/specs; then
activate exactly one provider node and bind its name into the same signed
generation before installing the new workload Deny policy. Only after that
gate is live may the credential-bearing
storage release trigger scale-up to the one-node maximum. The former observer DaemonSets and every
predecessor admission object remain present. Under policy conjunction, old
exact-key rules do not select the successors and the new rule rejects every
unlisted blanket workload. A generation cannot be accepted if either observer
receipt or the exact activated-node inventory receipt is absent.

The ConfigMap Helm driver is not a namespace-wide exemption. RBAC may grant
the release identity namespace-scoped create only because Kubernetes cannot
resource-name-restrict create; update/patch must be restricted to the exact
generation's Helm v1 record. The active policy matches every ConfigMap request
from that identity and admits only that record's canonical labels and sole
`release` data key. Contract/trust ConfigMaps and all other namespace config
remain unreachable, and delete is never admitted.

The retained first additive policy uses component `storage-reconciler-v2`.
Compatibility-v3 uses disjoint `fs2-storage-v3-*` names, the
`storage-reconciler-v3` component, and a content-derived release identity. The
fixed predecessor and retained v2 VAPs therefore do not select or authorize v3
objects. Each v3 boundary and workload policy map entry stores its own exact
canonical policy spec and digest, so retained generations protect themselves
without matching a later rotation. The fixed Deployment selector is never
edited. The exact live UIDs and content
digests form a predecessor receipt. A separately mounted prior-head checkpoint
also commits the workloads backend identity, state lineage/serial, Helm release
ID/revision/manifest, predecessor Deployment and NetworkPolicy UIDs/specs, and
the four retained Terraform addresses including `helm_release.control_plane`.
Both Terraform roots compare initialized S3 backend metadata plus the actual
remote state lineage, serial, snapshot bytes, exact non-empty address set, and
object-store version ID to signed custody. The object version is obtained only
through a fixed root-owned, read-only, digest-bound provider adapter. This root
will add nothing unless both custody records name the same predecessor digest.
The generation-named NetworkPolicy inventory Role and RoleBinding are created
and retained by the security owner, not the Helm release identity, so that
identity has neither `bind` nor `escalate` authority. Because Kubernetes cannot
resource-name-restrict create, every Role/RoleBinding request from the owner is
admission-matched. Only a create-only `fs2-storage-v2-*` or
`fs2-storage-v3-*` Role with exactly `get,list` on NetworkPolicies and a
same-name binding to the same-name namespace ServiceAccount is admitted. No
Role may delegate ConfigMap, Secret, Pod, workload, token, RBAC, or admission
authority.
`capture_predecessor_receipt.py` creates that receipt through exact read-only
`kubectl get` calls and descriptor-bound kubeconfig access; it reads no Secret.
The capture must occur before the provider ledger is independently signed.

The workloads root keeps the three direct predecessor addresses plus the
control-plane Helm release with `prevent_destroy`; the direct objects also use
`ignore_changes = all`. It does not use `removed` blocks
or post-forget custody. Do not target resources, remove state entries, use `-replace`, or apply a plan
that destroys or replaces any existing generation. The current remediation is
source-only; no live action is authorized.

The only future execution entry point is
`security/apply_custodied_additive_plan.py`. It descriptor-binds the exact
root-owned saved-plan bytes, externally signed approval under a fixed
read-only public key/execution profile, clean source commit/tree, accepted
SAI-10 ancestry and predecessor state; rejects every action other than
create/read/no-op; applies those same bytes; then proves the expected successor
lineage, minimum serial and exact address set. It has no plan-generation or
cleanup mode.

The dependency verifier binds the exact independently accepted SAI-10
commit/tree and requires that accepted corrective commit to be an ancestor of
integration `HEAD`. It does not reject the accepted continuation merely
because its immutable history preserves an earlier rejected predecessor.

## v10 protected-lane correction

The v10 admission contract treats the stable lane taint key as the security
identity. Any exact-key toleration, keyless blanket `Exists` toleration, or
direct binding to the attested Node enters the guard regardless of selectors or
affinity. This conservative rule removes the need to predict satisfiability
from a future or partial label projection. Availability exceptions are limited
to the complete signed live DaemonSet inventory: exact namespace/name/UID,
canonical spec, owner identity and controller-created child Pod spec.

Controller authorization compares the audit-proven kube-system ServiceAccount
username, UID and complete deterministic group set. The Kubernetes role names
`system:controller:*` are never used as authenticated identities. The current
generation also guards the attested Node against deletion or any change to its
full label and taint maps, then re-reads the live Node after the Deny binding is
installed. The stable lane is provisioned first by the separate provisioning
root; post-creation Node/controller/agent facts are signed only in the later
attestation/admission generation, removing the prior bootstrap cycle.

## v11 authenticated maintenance and NodeGroup binding

The v11 successor keeps every v10 predecessor but replaces its unverified
inputs. Controller roles are mapped only by a fresh, externally signed audit
receipt from the fixed read-only authority store; command-line controller JSON
is not accepted. The receipt covers the actual ServiceAccount or native system
identity username, UID and complete groups. It also supplies the exact
node-health controller and exact mutable health label/taint keys.

The protected Node is admitted only after its `spec.providerID` is proved as a
member of the fresh signed NodeGroup/provider/backend receipt. Admission keeps
the Node UID, providerID, lane label and lane taint immutable. Ordinary status
updates remain possible when scheduling metadata is unchanged; only the
audit-proven node-health identity may change the signed allowlist of health
labels/taints or `unschedulable`. Provider maintenance can therefore operate
without allowing a different Node or security group into the credential lane.

Every live DaemonSet is read twice at one exact list resourceVersion. The full
list digest and all blanket-tolerating agents are signed, then the boundary
repeats that complete check at the activation boundary. The v12 successor
makes both reads pre-activation and delegates the continuing invariant to the
external fence. Critical-agent updates require an audit-proven ServiceAccount plus namespace-scoped
`resourceNames` RBAC for only the recorded DaemonSet names; wildcard update or
patch authority is rejected. The provider-only bootstrap uses one node rather
than relying on a DaemonSet to scale a zero-node group.

## v12 continuous fence and race-free activation

The v12 handoff requires a separately owned continuous DaemonSet admission
fence before this root can create an ordinary generation binding. The fixed
read-only registry pins the external adapter, enforcer artifact and prior
ledger anchor. Source defines the canonical semantics; verification parses the
full policy/binding specs and ledger, recomputes their digests and hash chain,
and requires an independent adapter read of the same live objects. A receipt
echo is not accepted. The ordinary root has no authority to create, alter,
disable, or delete that fence.

Critical-agent upgrades first append an authorized old/new snapshot transition
to that external ledger, then carry its content generation and digest on the
DaemonSet and child Pods. Fence-aware retained policies constrain exact
namespace/name/UID, authenticated owner and controller owner-reference, but
delegate snapshot authenticity to the continuous fence instead of pinning one
spec forever. Consequently an authorized update and its replacement Pods can
satisfy every retained fence-aware Deny policy. A pre-fence exact-spec policy
cannot be made composable additively; the authority verifier refuses that
state rather than pretending a later allow can override an earlier Deny.

The fence is live-equal before two ordinary inventory reads. The historically
named `post_guard` is now the second pre-activation gate. Both ordinary Deny
bindings are ordered after both reads and the final Node equality gate. The
workload binding additionally waits for trust, contract, NetworkPolicy and
RBAC objects, so no post-binding precondition can fail and strand a newly
active gate. Continuous external enforcement closes the interval between the
final read and binding creation.

Each lane generation has one attested provider member. CREATE of a second
same-generation lane Node and replacement/deletion of the attested Node are
denied. A distinct lane generation may be prepared while retaining the
predecessor, but the source does not call that repair or authorize cutover.
Cutover still requires a separately reviewed quiesce/drain/retirement protocol.
Under the active no-delete rule no eviction, teardown or retirement may run, so
this remains source-only and fail-closed.

## v13 renewable authority and independent evidence

The v13 handoff adds the canonical receipt-authority registry digest to the
provider ledger, release-values contract and immutable activation trust mount.
Provider-drain, rollback zero-inflight, schema-compatibility and provider-
continuity receipts use four distinct purpose-bound Ed25519 keys, all distinct
from the cutover signer. Each signature covers canonical raw observations and
the exact observation-adapter digest. The ordinary workloads root can consume
the registry but cannot substitute an authority or weaken its purpose.

Short-lived cutover credentials are renewed by an append-only `AUTH_REFRESH`
epoch. The external verifier requires byte-identical transition state,
Deployment custody, controller identities, active generation and registry
digest; only the unique bounded owner credential and epoch validity advance.
This keeps stable admission and reconciliation available without fabricating a
cutover or reviving a retained principal.
