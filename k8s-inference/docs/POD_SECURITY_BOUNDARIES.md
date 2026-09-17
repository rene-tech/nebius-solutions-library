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
   must grant no ingress or egress, ServiceAccounts must be tokenless, and each
   frozen DaemonSet must have one stable, fully Ready generation. The result
   binds every retained Pod UID, resourceVersion, projected-spec hash, phase,
   and readiness value across two fenced reads. Admission freezes every
   retained identity, denies token requests and new consumers for retained
   ServiceAccounts, and permanently denies replacement Pods owned by retained
   DaemonSets. Existing healthy DaemonSet Pods remain available; no impossible
   `desiredNumberScheduled=0` condition is required. A distinct bounded reader
   uses a ten-minute anchor-bound token and Kubernetes
   `PartialObjectMetadataList` content negotiation to prove that no annotated
   legacy ServiceAccount token Secret exists. A full SecretList is never
   requested or loaded. The metadata collection resourceVersion is signed into
   the result. The result is withheld until the
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

Every phase, including the non-destructive initial custody-state handoff,
requires a short-lived external-custody handoff. Post-prepare transitions also
require one v5 Ed25519 receipt whose single signature covers the complete
canonical bundle: reviewed signer identity and key digest,
cluster/run/kube-system UID, deployment nonce, exact prior and next state,
phase, one-time nonce, expiry, pinned PSA minor, six-namespace inventory, PVC
UID/resourceVersion/volume/class, dataset/revision/tree, retained filesystem,
digest-pinned proof image, immutable tooling digest, three mutually exclusive
identities and groups, and live observations. Each present observation carries the exact Kubernetes UID,
resourceVersion, and canonical object hash; absence observations carry no
substitutable identity. Baseline gates accept v5 artifacts only and additionally
bind complete live collection resourceVersions plus exact object
UID/resourceVersion/hash sets and their aggregate digest for every relevant
workload kind, including ConfigMaps, in every frozen namespace. Historical
v3/v4 artifacts and fixed 103/103/716 counts cannot bootstrap a rollout.

### Authoritative custody evidence and retained-state v3 handoff

The receipt-only v2 custody path is retained as rejected evidence. It trusted
signer assertions about provider policy and backend state without opening the
claimed raw artifacts, and its separate Terraform state would have duplicated
ownership while the platform retained the same addresses. Both v2 trust locks
remain blocked and the v2 root must not be initialized, planned, imported or
applied.

The v3 contract is source-pinned in
`stages/pod-security-custody/custody-trust-lock-v3.json` and is also deliberately
blocked. Its read-only Nebius/S3 adapter exhaustively paginates the exact tenant
and every project returned under it: tenants, projects, service accounts,
tenant users, groups,
forward/reverse memberships, every subject's access permits, access-key/auth-
key/static-key/federated-credential metadata, and the native bucket resource.
Every provider response carries its request and trace IDs; it never calls a
secret-delivery API. S3 evidence includes the public caller
access-key ID, ACL, policy and public-status, encryption, versioning, Object
Lock, retention, legal hold, and an exact VersionId/ETag/length-fenced download
of the platform Terraform state. Bounded generation files are created once,
mode 0600, fsynced and retained; retries use a new collection ID and paths.

The v3 verifier digest-matches those raw files to two independently signed
receipts, uses distinct provider/backend authorities, and reconstructs the
semantics instead of trusting receipt fields. It binds the S3 caller key back
to the enumerated provider account, proves the exact provider hierarchy and
group graph, binds each required permit to its claimed identity or signer,
rejects any platform path to the tenant/project/bucket inheritance chain and
all other protected custody resources,
evaluates the native and S3 bucket controls, parses the raw Terraform state,
and derives every retained custody address from its lineage/serial/version.
A third distinct authority signs the manifest bundle. Two fresh, distinct
preflight collections must have identical reconstructed IAM, backend-control
and versioned-state projections. Collection age, raw
digests, semantic projection digests, access-key identity, state version and
address aggregate fence drift.

The v3 handoff is non-state-forgetting. Every platform address remains in the
platform state; no `removed`, `state rm`, import, second Terraform state or
dual ownership is allowed. The external-custody preflight binds the raw-state
address set to immediate live UID/resourceVersion/full-object reads and emits
an `await-external-server-side-apply` handoff for one external field manager.
It performs no mutation itself. Provider IAM must deny platform mutation of
the protected resources before a separately reviewed external executor may
act. This source does not contain that active external boundary or evidence,
so it cannot authorize a rollout and makes no GO claim.

### Retained rejected v2 archive (not operational)

The following design record is preserved to explain the rejected v2 custody
proposal. It is not an executable rollout path and does not supersede the
blocked, non-state-forgetting v3 contract above.

Live receipt verification and ledger mutation run only in the separately
administered custody pipeline, never inside platform Terraform. The standalone
`stages/pod-security-custody` root declares a partial encrypted, lock-enabled
S3 backend and a single owner provider graph; it accepts no platform,
receipt-reader, or metadata-reader kubeconfig. A canonical repository trust
lock—not ordinary Terraform variables—pins the external Ed25519 authority,
provider tenant/project and principal identities, exact exclusion set, backend
bucket/key/region, remote-state lineage/serial/object version/ETag/hash, and
separately signed provider-IAM and backend receipts. The checked-in lock is
deliberately inactive because those independently owned facts have not been
issued or reviewed. This revision therefore cannot plan, apply, consume a
handoff, or claim custody activation.

Before any future SSA, the external pipeline must prove a one-to-one inventory
of every static platform state address and every dynamic retained
NetworkPolicy, ServiceAccount, and DaemonSet instance. It immediately re-reads
each live object under the authenticated owner and compares UID,
resourceVersion, and canonical full-object hash. Present objects carry those
exact values as SSA preconditions. The new empty immutable token anchor is not
created by SSA: a typed Secret resource uses Kubernetes POST create, so a
same-name race fails atomically instead of adopting or patching the object.
After creation, the bound metadata reader proves its exact UID/resourceVersion
through `PartialObjectMetadataList` without retrieving Secret data. Missing, additional,
duplicate, stale, or unsupported addresses fail. The platform root retains
every current address: the predecessor `removed { destroy=false }` design is
archived inside an HCL comment and performs no state forgetting. A later
independently reviewed source commit is required even after complete adoption.

`scripts/run_sai07_external_custody_pipeline.py` is the source-owned external
entrypoint. It verifies provider/backend trust, rejects an existing saved-plan
path, refuses delete/replacement actions, performs exact adoption, then stops
for independently signed post-SSA rereads. Its separate authorization mode
verifies the v2 handoff before invoking the existing resourceVersion CAS. This
task did not execute either mode. Platform Terraform receives
only a descriptor-fenced, whole-file signed handoff binding the exact context,
bundle, phase, action, ledger UID/resourceVersion/sequence/nonce, adopted object
aggregate, authority audits, and metadata-only Secret inventory. It cannot
acknowledge its own apply: the external pipeline performs post-apply live reads
and acknowledgement separately. The monotonic ledger stores the last receipt,
nonce, sequence, state, and phase authorization.
An exact already-consumed bundle can resume idempotently after a process crash;
a different or expired bundle cannot. Phase skipping, stale resourceVersions,
spec/status drift, inventory omission, context substitution, and concurrent
ledger updates all fail closed. A digest-shaped string or a valid signature
without successful live reconciliation and ledger consumption has no authority.

Kubernetes admission cannot self-protect its own ValidatingAdmissionPolicy or
binding objects, so this design makes no such claim. Preventive custody can
come only from the separately issued, provider-evidenced IAM/backend boundary
and exact platform-principal exclusion outside the platform root. Before each
handoff, five full live audits cover the platform, inactive owner, token issuer,
receipt ServiceAccount, and metadata-reader ServiceAccount. Each artifact binds
the authenticated username, exact groups, and, for short-lived ServiceAccounts,
the token JTI. It carries—not merely hashes—every namespace's complete
SelfSubjectRulesReview and the exhaustive SelfSubjectAccessReview matrix for
Secrets, token minting, anchors, ConfigMaps, RBAC mutation/bind/escalate,
admission, quarantine objects, workloads, storage, proxy/exec/attach/
port-forward, webhooks, CRDs, CSRs, authentication/authorization reviews, and
user/group/ServiceAccount/userextras impersonation. The v2 handoff recomputes
the full-evidence hashes and requires the exact allow/deny matrix for every
identity across one identical namespace inventory.

The two short-lived automation identities are tokenless ServiceAccounts.
Their distinct TokenRequest credentials use exactly the Kubernetes API
audience, at most ten minutes, and `boundObjectRef` to the immutable empty
`fs2-system/fs2-pod-security-token-anchor` Secret UID. JWT subject, audience,
lifetime, ServiceAccount UID, bound Secret UID, and JTI are checked before each
token is held in anonymous memory-backed storage. Fail-closed admission limits
TokenRequests to those two exact ServiceAccounts and requires the anchor
reference on every request. The metadata reader requests
only `PartialObjectMetadataList`; the collector rejects any response containing
Secret `data`, `stringData`, or other non-metadata fields. No command in this
task queried live Secret data.

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
when the read-only v8 retained-quarantine result proves all exact objects and
the stable Ready Pod set under the admission fence described above.
Permissive/unhealthy objects, changed UIDs, annotated token Secrets, a token
drain shorter than the reviewed issuer maximum, new consumers or Pods, missing
objects, or any claimed removal keep integration and enforcement blocked.
Source conformance is not live closure evidence. The current SAI-03 successor
remains source-only and independently NO-GO; sharing its finite-profile
contract is not acceptance. The independently accepted KEDA scale-handoff
lineage is present in this branch, but that ancestry does not change SAI-03 or
SAI-07 acceptance status.
