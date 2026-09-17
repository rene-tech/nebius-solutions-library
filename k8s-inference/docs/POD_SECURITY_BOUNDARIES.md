# Pod security and controller ownership

FS2 applies Kubernetes Pod Security Admission (PSA) at namespace boundaries.
Application namespaces enforce the `baseline` Pod Security Standard and report
`restricted` violations in audit records and admission warnings. Host-integrated
node agents and the one retained CUDA checkpoint profile use two separately
admission-constrained exception namespaces.

| Namespace owner | Enforced state | Purpose |
| --- | --- | --- |
| Foundation: `fs2-system`, `fs2-data`, `fs2-models`, `fs2-observability` | `baseline`; `warn`/`audit=restricted` | Platform, database, model runtime, and namespaced observability workloads |
| Academic-assets and existing scientific namespaces named in deployment inputs | `baseline`; `warn`/`audit=restricted` | Scientific jobs and retained scientific namespaces |
| Workloads ModelExpress namespace | `baseline`; `warn`/`audit=restricted` | Optional ModelExpress control service |
| Reference-data module | `baseline`; `warn`/`audit=restricted` after CSI verification | Reference-data staging and status services on the RWX claim |
| Foundation: `fs2-node-observability` | `privileged`; `warn`/`audit=restricted` | Host-integrated, operator-owned node agents only |
| Foundation: `fs2-snapshot-operations` | `privileged`; `warn`/`audit=restricted` | One digest-pinned ESMFold2 donor/restore profile only |

The node-observability namespace is not a general workload destination. Two
fail-closed ValidatingAdmissionPolicies admit only four exact DaemonSet/service
account pairs and their Pods. Direct Pods and ephemeral containers are denied;
images, commands, host paths, mounts, host namespaces, and capabilities are
bounded. The snapshot namespace has a separate fail-closed policy and a fixed,
tokenless manager ServiceAccount/RoleBinding. It admits only the exact reviewed
runtime and tools digests, command, GPU resources, PVCs, mounts, and isolation
profile. Neither exception is selected by a caller-provided username or a
general workload label. These admission boundaries still apply if an unrelated
role is later widened.

## Ordered rollout

`deployment.pod_security.rollout_phase` makes admission changes separable from
workload movement. Advance only after the checks for the current phase pass:

1. At the serialized rollout slot, record the then-current stable Helm revision
   and image digests. Confirm `request_debug_enabled=false`. A historical Helm
   revision is never a cross-ticket rollback target.
2. `prepare`: install the admission-protected `fs2-node-observability` and
   `fs2-snapshot-operations` boundaries and their additive replacement
   resources. Application namespaces remain unlabeled and the old host agents
   remain in service.
3. `bootstrap-baseline` captures the complete v5 live inventory and advances to
   `baseline-captured` only after an authorized whole-bundle signature binds
   the artifact. V5 records the authoritative counts observed at capture time,
   the complete legacy-ServiceAccount token-Secret metadata inventory and its
   collection resourceVersion;
   it does not substitute historical counts. The signed artifact and an
   immediate live re-read must match object-for-object.
4. `migrate-reference-data` consumes `baseline-captured`. Dual-run the GPU
   observer, DCGM exporter, node telemetry collector, and Prometheus node
   exporter in both old and exception namespaces. Advance to `exception-ready`
   only after immediate reads prove exact UID, resourceVersion, spec hash,
   immutable configuration content, and readiness of every old/new agent plus
   the exception admission and RBAC objects.
5. `cleanup-legacy-resources` consumes `exception-ready` and advances to
   `reference-data-ready` only after the canonical retained RWX claim, every
   BioIR reference-data successor, and the snapshot reference and checkpoint
   successors are Bound to retained classes and their exact content/durability
   probes pass. Missing claims or probes fail closed.
6. `quiesce-enforcement` consumes `reference-data-ready`. Reconcile every
   retained model and App under the finite network profiles. The no-delete
   closure first adopts existing controller NetworkPolicies and ServiceAccounts
   into exact deny-only/tokenless Terraform custody without replacement. It
   adds a destruction-protected quarantine label to every retained DaemonSet;
   admission freezes its spec and refuses every new owned Pod. The closure then
   performs exact read-only UID/resourceVersion/spec checks and permits
   a nonempty legacy inventory only as a retained quarantine: NetworkPolicies
   must grant no ingress or egress, ServiceAccounts must be tokenless and have
   no consumers, and DaemonSets must schedule and own zero Pods. Admission
   freezes every retained identity, denies token requests and new consumers for
   retained ServiceAccounts, and permanently denies Pods owned by retained
   DaemonSets. Existing DaemonSet Pods are not deleted and remain a hard block
   until they naturally reach zero. An immediate complete Secret-list read must
   also prove that no annotated legacy ServiceAccount token Secret exists; its
   resourceVersion is signed into the result. The result is withheld until the
   admission/token fence has outlived the externally reviewed maximum prior
   bound-token lifetime. The signed result preserves every frozen UID and permits no
   removed or absent object. Any active or permissive object remains a hard
   integration blocker. The verified retained finding counts, rather than a
   manufactured zero, advance the ledger to `enforcement-quiesced`; the CAS
   then activates the fail-closed admission fence for Pod-producing writes.
7. `enforce` consumes `enforcement-quiesced` and applies `baseline` enforcement
   plus pinned-minor restricted warn/audit labels to
   the foundation, reference-data, academic, ModelExpress, and explicitly listed
   existing scientific namespaces. A privileged Pod submitted to `fs2-models`
   must be rejected. Follow the negative probe with model-controller,
   arbitrary-UUID App, scientific-job, database, telemetry, and inference smoke
   tests. The admission fence remains active until both Terraform stages have
   immediately re-read the pinned labels and acknowledged the exact
   authorization. Sign `baseline-enforced` only after these checks pass.

The existing-scientific-namespace input is exactly the frozen set
`fs2-academic-poc` plus
`fs2-bioir-{boltz2,coverage,openfold,protenix,snapshot}`, not a prefix selector
or an optional empty list. The read-only inventory collector independently
compares that complete live scientific inventory before enforcement.
Historical hostPath launchers have CSI-only successor contracts that refuse a
live launch until each namespace-local claim is Bound to the retained class and
every required subpath passes a read-only probe. The privileged donor/restore
renderer has a separately admission-constrained exact-profile successor in
`fs2-snapshot-operations`; it cannot launch until its exact reference and
checkpoint claims independently pass retained-content and durability gates.
Source contracts are not evidence that those claims exist or contain data in a
live cluster, and PSA rollout must stop while any successor is absent.

## Ordered rollback

Rollback is also phased; do not delete the exception namespace while an agent
still uses it:

1. `rollback-remove-enforcement` consumes `baseline-enforced` and removes
   application-namespace enforcement while exception agents remain Ready.
2. `rollback-restore-host-agents` consumes `enforcement-removed`, recreates the
   old-namespace agents while the exception copies continue running, and signs
   `host-agents-restored` only after all restored agents are Ready.
3. `rollback-remove-exception` consumes `host-agents-restored` and first refuses
   active snapshot Pods or unexported checkpoints. It disables the exception
   agents only after their legacy copies are Ready. The exception namespaces,
   admission boundaries, and content-addressed immutable telemetry/tooling
   ConfigMaps remain as a retained generation; they are not phase-conditioned
   away and every one is protected by `prevent_destroy`. The retained
   reference-data CSI claim is likewise not destroyed by rollback.

Use plans and the stable Helm revision captured at the serialized rollout slot,
and reject any rollback candidate with request debugging enabled. Never use a
historical shared revision and never jump directly from `enforce` to an agent
restore or exception removal phase.

## Reference-data CSI gate

`prepare` retains the legacy read-only host path while creating the RWX claim
on `fs2-reference-data-retained-sc`. Its separate driver is rooted at
`/mnt/fs2-reference-data/csi-mounted-fs-path-data`, not the general model cache.
The StorageClass is post-rendered to `Retain`; the chart release and PVC use
`prevent_destroy`; the storage handoff must prove deletion is forbidden and
capacity is at least the 1611 GiB request.

A completed Job or mutable annotation is not storage evidence. Each reference
claim proof has a signed generation-derived name and must bind the live PVC UID,
resourceVersion and volumeName, exact dataset tree, digest-pinned runtime, and
content-addressed immutable tooling. The verifier re-reads the bounded set of
Job-owned retry Pods, checks every exact command and runtime image ID, and
requires exactly one successful self-hashed termination proof. Snapshot
checkpoint durability requires separate writer and read-only remount Jobs:
an exact writer followed by an exact read-only remount reader for the same
challenge-bound marker. Missing namespace-local claims, immutable tooling,
writer/reader Jobs, or their owned Pods keeps the rollout SOURCE/LIVE NO-GO;
annotations alone never satisfy the gate.

The deployable successor graph is owned by
`stages/workloads/reference_data_successors.tf`. A post-`prepare` deployment
must supply `deployment.pod_security.successor_storage`; there is no default and
no dynamic-empty-claim fallback. The contract identifies the live canonical
reference PV by name, UID, resourceVersion, CSI driver, handle and attributes,
and identifies a distinct pre-provisioned checkpoint CSI volume. Both identities
carry an external provisioning-receipt digest and storage owner. The complete
contract digest is part of the signed rollout context consumed independently by
the foundation and workloads stages. The short-lived custodian has `get` (and
never list or mutation) for exactly that canonical PV, the six fixed alias PVs,
and the fixed checkpoint PV so all eight required live reads are authorized.

Terraform then creates exactly six fixed `ReadOnlyMany` PV/PVC aliases: one in
each of the five BioIR namespaces and `fs2-snapshot-reference` in the snapshot
exception namespace. All aliases use the live-verified canonical CSI handle, so
they read the retained dataset rather than provisioning empty per-claim
directories or copying 1.6 TiB six times. A separate fixed `ReadWriteMany` PV/PVC
backs `fs2-snapshot-checkpoints`. Every PV and PVC uses `Retain` semantics and
`prevent_destroy`; immutable proof tooling is replicated to every consumer
namespace. The checkpoint writer Job must complete before the independently
mounted read-only reader Job. Proof tooling is keyed by its content digest, and
Jobs are keyed by the full signed proof-generation identity rather than a
mutable single-instance address. The signed ledger retains one through eight
unique, monotonically sequenced generations; each retry appends a new
nonce/attempt generation, while every earlier ConfigMap and Job remains in
Terraform state with `prevent_destroy`. Reaching eight generations fails closed
pending a separately reviewed archival procedure. Within one generation, a
failed Job may create at most three Pods. The writer first creates and fsyncs a
unique candidate, then atomically hard-links that complete inode into the final
no-overwrite name and fsyncs the directory. A crash or short write before
publication leaves only a bounded retained candidate; a retry uses a new
candidate. A crash after publication resumes only when the final marker's exact
bytes match, and that retry fsyncs the containing directory again so a prior
link-before-directory-fsync crash cannot be acknowledged prematurely. No marker
or candidate is unlinked or rewritten. This is exact filesystem namespace and
remount evidence; it is not an independent disaster-recovery or backup claim.
All six reference read probes and both checkpoint Jobs bind the generation,
attempt, signed nonce, and live PVC UID, resourceVersion and volumeName.

The predecessor layout is adopted without replacement. A signed `retained-v2`
mode binds fourteen exact retained ConfigMap/Job addresses, their live UIDs,
resourceVersions and canonical object hashes, plus the exact v2 rollout-ledger
data digest. A mutually exclusive `fresh-v3` mode requires a null v2 digest and
an empty predecessor set. Explicit Terraform `moved` blocks transfer old
namespace/successor/count addresses to destruction-protected retained addresses.
The verifier re-reads every predecessor after signature verification and before
the CAS. Its one allowed v2-to-v3 ledger edge writes the new proof-generation
custody and the next signed phase in one resourceVersion-guarded update; an
exact retry can resume Terraform after a crash without replaying a nonce.

The rollout ConfigMap ledger separately persists the ordered generation IDs,
latest sequence/active ID, and signed generation-ledger digest. Its CAS accepts
only an append-only prefix extension and records that extension atomically with
the authorized phase transition. Thus a failed probe can use a fresh signed
nonce without resetting the rollout state, while removing, rewriting, reordering
or replaying a prior generation fails before any acknowledgement.

The snapshot admission policy has finite, non-privileged profiles for only the
Job-controller-created snapshot reference probe and the two durability Pods.
They bind the exact proof image and immutable tools generation, command
arguments to Pod annotations, fixed claims, read/write mode, resource limits,
storage-node placement and restricted security context. They do not broaden the
privileged snapshot runtime profile or admit caller-created Pods.

These source resources do not establish external storage custody or live
readiness by themselves. The rollout remains blocked until the referenced
volumes and receipt digests are independently reviewed, a non-destructive plan
proves only additive actions, and the live probes complete under the serialized
rollout gate.

Every post-prepare phase requires one short-lived v5 Ed25519 receipt whose single
signature covers the complete canonical bundle: reviewed signer identity and
key digest, cluster/run/kube-system UID, deployment nonce, exact prior and next
state, phase, one-time nonce, expiry, pinned PSA minor, six-namespace
inventory, PVC UID/resourceVersion/volume/class, dataset/revision/tree,
retained filesystem, digest-pinned proof image, immutable tooling digest, and live
three mutually exclusive identities and groups (platform Terraform, receipt
operator, and custody owner), and live observations. Each present observation carries the exact Kubernetes UID,
resourceVersion, and canonical object hash; absence observations carry no
substitutable identity. Baseline gates accept v5 artifacts only and additionally
bind complete live collection resourceVersions plus exact object
UID/resourceVersion/hash sets and their aggregate digest for every relevant
workload kind, including ConfigMaps, in every frozen namespace. Historical
v3/v4 artifacts and fixed 103/103/716 counts cannot bootstrap a rollout.

Receipt verification is an apply-time operation, never a replayable Terraform
data source. The foundation consumer re-reads every signed object and inventory
from the selected API server immediately before an atomic ConfigMap
resourceVersion compare-and-swap. The monotonic ledger is context- and
authority-bound, deletion-protected, admission-limited to exact rollout
identities, and stores the last receipt, nonce, sequence, state, and phase
authorization. The owner and workloads stages acknowledge the exact
authorization only after their dependent resources pass immediate live checks.
An exact already-consumed bundle can resume idempotently after a process crash;
a different or expired bundle cannot. Phase skipping, stale resourceVersions,
spec/status drift, inventory omission, context substitution, and concurrent
ledger updates all fail closed. A digest-shaped string or a valid signature
without successful live reconciliation and ledger consumption has no authority.

The verifier does not impersonate the rollout identity. Every post-prepare
stage requires three different kubeconfigs and authenticated users: platform
Terraform, a non-system receipt operator in the dedicated receipt-custodian
group, and a separately administered non-system custody owner in its own group.
The foundation provider alias for custody resources uses only the third
kubeconfig. A self-protecting admission boundary excludes the platform,
receipt, `system:masters`, and service-account identities from modifying or
deleting custody ServiceAccounts, RBAC, ledger and admission objects. A
SelfSubjectReview proves all three identities and groups are disjoint.
SelfSubjectRulesReview is then evaluated in every live namespace, not a static
namespace subset, and rejects Secret reads, Pod proxy/exec/log access, unrelated
RoleBinding grants, persistent mutation, RBAC bind/escalate, admission-policy,
impersonation, credential, or workload pivots. Non-resource authority is
restricted to the exact Kubernetes discovery/health URL set. The only
persistent mutation edge allowed is exact-name `serviceaccounts/token`
creation, alongside the self-review APIs needed to prove the boundary. A
fail-closed admission policy requires a
directly authenticated non-system identity with authenticator JTI metadata and
permits only the exact API audience for at most ten minutes. The returned JWT
is checked for subject, audience, lifetime and live ServiceAccount UID, then
held in an anonymous in-memory kubeconfig. Exact SelfSubjectAccessReviews also
pin the one token edge and deny unbounded token minting, direct ledger or
admission mutation, RBAC creation/bind/escalate, custodian
user/ServiceAccount/group impersonation, and authenticator-extra impersonation.
Ledger admission
independently requires that short-lived ServiceAccount JWT's authenticator JTI;
ordinary username/group impersonation cannot write the ledger. The retained
predecessor manager and its bindings remain unchanged for non-destructive state
continuity, but that username cannot pass ledger admission and is never used by
the verifier.

## Model-controller ownership

The dynamic model controller does not own ConfigMaps, NetworkPolicies,
PersistentVolumeClaims, ServiceAccounts, or DaemonSets.
Dynamic Deployments use the dedicated, non-token-mounted `fs2-model-runtime`
ServiceAccount provisioned by Terraform. Host-memory-residency declarations
remain published: Terraform owns one finite holder per canonical model/pool,
and arbitrary App UUIDs only consume its signed receipt. The controller never
creates or mutates those DaemonSets.

The controller has no ConfigMap, PersistentVolumeClaim, or NetworkPolicy API
endpoint and its Role has no `configmaps`, `persistentvolumeclaims`, or
`networkpolicies` rule. It therefore cannot get, list, watch, create, patch, or
delete those resource kinds. Runtime isolation is supplied by
a finite set of Terraform-owned profiles selected by immutable
`fs2-serve.nebius.ai/network-profile` Pod labels. Standard profiles are bound
to exact service ports; ModelExpress profiles are bound to an exact reviewed
qualification and pool. Runtime App UUIDs are never policy object identities.

The profile contract is shared with the runtime-isolation implementation and
must be integrated as one reviewed lineage. Arbitrary App creation, update,
stale-workload cleanup, and finalizer cleanup must continue using only the
Deployment, Service, and ScaledObject permissions. Integration verification
must prove all six NetworkPolicy verbs are denied to the controller service
account while those App lifecycle paths remain functional.

## Verification

After `enforce`, inspect the namespace labels and exercise both negative and
positive paths:

```bash
kubectl get namespace \
  -L pod-security.kubernetes.io/enforce \
  -L pod-security.kubernetes.io/warn \
  -L pod-security.kubernetes.io/audit
```

The rollout evidence must include the exact deployment inputs, CSI driver/class
and claim UID, whole-bundle signature identity, pre/post ledger resourceVersion
and sequence, one-time consumption result, DaemonSet readiness, exact namespace
and live object/list hashes, exact no-delete legacy closure inventory,
admission-policy identity, negative privileged-Pod result, positive
customer/App/inference checks, and the slot-time stable rollback revision with
request debugging disabled.

Under the current no-delete operating constraint, legacy controller-owned
objects are never reported as removed. A nonempty inventory may advance only
when the read-only v7 retained-quarantine result proves all exact objects inert
under the admission fence described above. Active/permissive objects, changed
UIDs, annotated token Secrets, a token-drain interval shorter than the reviewed
issuer maximum, consumers, scheduled/owned Pods, missing objects, or any claimed
removal keep integration and enforcement blocked. Source conformance is not
live closure evidence. The current SAI-03 successor remains source-only and
independently NO-GO; sharing its finite-profile contract is not acceptance.
