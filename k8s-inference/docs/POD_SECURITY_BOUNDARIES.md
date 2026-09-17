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
   Under the active no-delete constraint, both generations remain enabled and
   destruction-protected in every later phase. Source does not authorize Helm
   count removal, registry-Secret removal, or controller-value removal of a
   legacy observer.
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
   integration blocker. Verified retained finding counts, rather than a
   manufactured zero, may advance the ledger only after a separately accepted
   non-destructive quiescence contract exists. This checked-in source retains
   the legacy agents, so enforcement remains blocked instead of claiming that
   retained privileged workloads are baseline-compatible.
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

The receipt state machine retains the historical rollback phase names, but the
current no-delete source does not implement a destructive rollback. In all
three rollback phases it keeps both legacy and exception agent releases,
registry Secrets, exception namespaces, admission boundaries,
content-addressed telemetry/tooling ConfigMaps, and retained reference-data CSI
claims present with `prevent_destroy`. `rollback-remove-enforcement` may remove
only the reversible PSA namespace label after an exact authorization;
`rollback-restore-host-agents` can only verify the already-retained legacy
generation; and `rollback-remove-exception` cannot disable or uninstall the
exception generation. Any future cleanup/removal design requires a separate
review and is outside this source candidate.

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
`stages/pod-security-custody/custody-trust-lock-v3.json` and is deliberately
blocked. Its read-only Nebius/S3 adapter exhaustively paginates the exact tenant
and every project returned under it: tenants, projects, service accounts,
tenant users, groups, Group-object forward/reverse memberships, every subject's
access permits, and the native bucket resource. Every provider response carries
its request and trace IDs. It performs no credential enumeration. In
particular, `AccessKeyService.ListByAccount` can return `status.secret`, while
enumerating another credential class still would not identify the caller that
signed an S3 request.

Each activation therefore binds one unique service-account epoch. Profile Get
proves the active caller and its project/tenant lineage; exhaustive group and
permit enumeration reconstructs the exact direct and inherited authority
closure. Every prior epoch service account must remain present with zero group
or permit closure. Credentials are neither read nor deleted/revoked; an old
credential cannot inherit the new epoch's permissions because its preserved
principal has no current authorization. The exact singleton backend group,
native/S3 policy and successful bounded S3 reads bind backend access to the
current epoch principal. S3 evidence includes ACL, policy/public status,
encryption, versioning, Object Lock, retention, legal hold, and an exact
VersionId/ETag/length-fenced platform-state download. Bounded generation files
are created once, mode 0600, fsynced and retained.

The verifier digest-matches those raw files to two independently signed
receipts, uses distinct provider/backend authorities, and reconstructs the
semantics rather than trusting receipt fields. It proves the provider hierarchy,
epoch lineage and complete group graph, binds each permit to its identity,
excludes platform paths to protected custody resources, evaluates native/S3
controls, and parses the complete raw Terraform state. The platform exclusion
set is derived from mandatory tenancy, backend, epoch, identity/group, permit,
signer/key and external-Kubernetes authorization categories rather than an
optional caller list. The raw byte hash plus all managed address count/digest
and each custody instance's API identity, attribute digest, UID/resourceVersion
and canonical desired security semantics are bound; every instance using the
retention-only custody provider must be in the exhaustive static/dynamic
inventory. Count-backed addresses retain their `[0]` instance key. Two fresh,
distinct collections must have identical reconstructed IAM, backend and state
projections. A third distinct authority signs the manifest bundle.

The tokenless Secret-metadata reader and its exact list/TokenRequest Roles and
bindings are additive platform-state resources ordered before the retained
custody boundary. They may be absent only in the first signed preparation;
subsequent generations bind their exact state address and live UID/RV/hash.
The external executor has no RBAC mutation authority.

The v4 execution capsule makes a static native launcher—not Python—the actual
PID 1 and first trust decision. The external root signs the exact
digest-qualified image, complete admission-object identities, launcher binary,
capsule, worker and evidence closure. The launcher measures `/proc/self/exe`,
rejects `PT_INTERP`, `PT_DYNAMIC`, writable executable segments and executable
stack, verifies that root signature without spawning a helper, and seals every
executable/runtime input before `exec` replaces it with measured Python. The
worker requires the launcher's sealed grant and exact sealed descriptors; its
preserved Python-first verifier is not reachable from plan/apply or external
acknowledgement execution.

Activation also requires two detached Ed25519-signed builder provenance
statements, not two self-declared builder labels. Distinct root-authorized
principals, keys, nonces and isolation domains independently bind the exact
source, build arguments and contract, pinned `CGO_ENABLED=0` Go toolchain and
standard-library closure, build environment, isolation-evidence digest and one
identical launcher output. The external root pins both public keys, receipts,
isolation facts and the closed receipt schema. Checked-in binary, authority,
toolchain, environment, isolation and receipt facts are deliberately null, so
launcher and capsule activation remain blocked. The former narrow verifier is
retained only as rejected provenance. General OpenSSL is sealed only after the
native decision and has configuration, engine and module discovery disabled.
The signed OCI digest, Pod image reference, runtime `imageID`, canonical image
provenance and SPDX envelope must still resolve to the same image.

Terraform parent timeout is not treated as mutation settlement. The dedicated
PID-1 capsule fences and reaps every descendant, including escaped process
groups, and proves the PID namespace empty. It then requires two independently
refreshed, stable Kubernetes-provider plans with no remaining managed action
and exact reconstruction of every known planned-object postcondition before a
failure can be classified as settled or execution can continue. Otherwise the
result is indeterminate and no later phase is authorized. All operations share
one bounded lease budget; checked-in activation remains blocked.

The handoff is non-state-forgetting. Every platform address remains in the
platform state through active, count-correct `prevent_destroy` declarations;
no `removed`, state rm, import, second Terraform state or dual ownership is
allowed. The preflight binds signed desired manifests one-to-one to that raw
semantic projection. Before and after additive writes, one ten-minute,
Kubernetes-API-audience epoch token arrives only through an inherited
descriptor and performs every live read, SelfSubjectReview, exhaustive
SSRR/SSAR, anchor request and acknowledgement SSA. Every SSRR resource rule is
expanded to atomic verb/API/resource/subresource/name authority and must equal
the complete source profile; every non-resource URL must equal the literal
read-only Kubernetes discovery set. Unknown CRDs/APIs/subresources/URLs and
wildcards fail even when two observations are stable. Its issuer and JWT subject
must bind the unique provider epoch principal, and the authenticator must expose
the same exact username/groups and JTI. No stable owner kubeconfig is accepted. Secret reads
use PartialObjectMetadataList only. The exact empty immutable,
custody-epoch-addressed anchor is an
atomic typed POST whose response must be PartialObjectMetadata and whose signed
admission policy rejects a non-empty/mutable/renamed or later rewritten form.
Every prior epoch anchor is retained and protected; a new epoch creates a new
`fs2-pod-security-token-anchor-v4-<epoch-sha256>` name, avoiding replacement or
deletion while preventing old credentials from minting a current token.
The same fail-closed policy evaluates every write from the external execution
identity and rejects any ConfigMap or Secret outside the exact anchor and
generation-acknowledgement profiles.

The platform kubeconfig is accepted only as canonical JSON and copied to a
sealed descriptor before use. The native-authenticated PID-1 worker parses those bytes before launching
kubectl; the rendered context must then resolve to exactly one linked cluster
and user with only embedded CA bytes and one embedded bearer token;
exec/auth-provider/token-file,
client certificate/key/CA paths, proxy/TLS overrides, extensions, and
impersonation are rejected. Terraform receives a sealed CLI configuration that
disables direct provider discovery, a fixed read-only configuration root with
no `.terraform` directory, a distinct fixed read-only `TF_DATA_DIR`, and a
content-hashed filesystem mirror. The configuration root, data root, mirror,
CLI, providers, and saved plan are selected by the attested capsule, not by a
caller's working directory or environment.

The external field manager owns zero fields on Terraform-retained objects. It
consumes the exact phase receipt, re-reads the retained set, and server-side
applies one immutable generation-addressed acknowledgement ConfigMap proven
absent from the complete raw state. A second read must exactly match the first.
The signed acknowledgement binds its UID/resourceVersion/full-object and field
set, phase/action/consumer/context, custody epoch, complete state aggregate,
the exact canonical saved-plan/config/variables/planned-values projection,
owner authority audits, token-anchor metadata and ledger-consumption digest.
It also carries the sorted, complete retained-object inventory with each state
address, API path, UID, resourceVersion and canonical full-object hash. The
apply-time verifier reconstructs that inventory from live API reads as its last
pre-apply operation and again as its first post-apply custody fence; aggregate
hash equality alone is not accepted.
The saved plan is created before the acknowledgement; planning does not read a
handoff file. The v4 PID-1 `plan-apply` capsule creates and applies the same
sealed descriptor, publishes a generation-addressed read-only copy for the
separate `external-ack` capsule, and derives the verifier query from the plan
rather than an environment override. The platform gate uses an in-place
`timestamp()` freshness clock. Its pending
update defers the exact acknowledgement data source until every apply attempt,
where it re-runs the pinned read-only plan projection and rejects any different
plan/config or expired acknowledgement before any dependent
resource can change. Foundation labels, scientific namespace labels, the
academic-assets module, ModelExpress namespace, and reference-data namespace
all depend on that verified output. The retained state-only gate updates in
place and has no replacement trigger or local-exec provisioner.

Plan, acknowledgement wait, acknowledgement verification and apply each have
source-owned finite deadlines. Immediately before mutation the PID-1 wrapper
requires both the plan-capsule attestation and the external acknowledgement
lease to remain valid for the complete maximum apply, post-verifier and Pod
identity windows. The exact sealed apply is killed at its bound and the
retained inventory is fenced even on timeout or nonzero exit. Such an attempt
is not replayed: recovery requires a new nonce and a fresh plan from the newly
observed state; no cleanup, state forgetting or force-unlock is performed.

The apply-time verifier also uses the exact sealed platform kubeconfig/context
from the saved plan. Before any authenticated request, PID 1 parses the sealed
canonical bytes and requires exactly one embedded bearer token and embedded
CA; exec/auth-provider/token-file, client key/certificate/CA paths, proxies,
extensions, TLS overrides and impersonation are rejected. A later minified
kubectl projection must expose only its fixed token/CA redaction markers and
the same server/context linkage. The kubectl executable itself is
descriptor-sealed and digest-pinned.
SelfSubjectReview must authenticate the separately
pinned platform username and groups, and every atomic SSRR resource and
non-resource rule in every frozen namespace must equal the repository-pinned
platform authority closure. Extra verbs, APIs, CRDs, subresources, named or
unnamed authority, wildcard grants and non-resource URLs fail closed; the SSAR
matrix is defense in depth rather than the completeness claim.

The checked-in v3 lock remains `activation=blocked` with null deployment facts.
Provider IAM must deny platform mutation, and independent review must pin the
exact epoch, identities, backend controls, keys, cluster, collector/executor
digests, plus accepted exact SAI-03 and SAI-04 commits before activation. Both
dependencies remain explicitly unaccepted. No deployment-bound facts were
invented, so this source cannot authorize a rollout and makes no GO claim.
The main lock also digest-pins a closed dependency source lock for raw-state
semantics, every manifest/trust verifier generation used by v3, the preflight,
metadata-only Secret handling, the phase-receipt verifier executed as a child
process, and the v1 audit client imported by the v2 authority auditor; an
unpinned direct or transitive helper cannot silently redefine the execution
contract.

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
generation-addressed `fs2-system/fs2-pod-security-token-anchor-v4-<epoch-sha256>`
Secret UID. JWT subject, audience,
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
