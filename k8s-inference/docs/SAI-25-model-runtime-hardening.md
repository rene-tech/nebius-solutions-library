# SAI-25 model runtime hardening source candidate

Status: source candidate only. This change has not been built, tested, deployed,
or checked against a live cluster or registry. The 2026-09-17 coordinator
boundary permits static source authoring and read-only Git inspection only.

The first candidate, commit `c483b90a7e7f9661fc27f694e4934bd4c14b72b5`
(tree `35856e2f58f5f8b5aeb46f159ba6fc9d7c2b7199`), is preserved as
rejected evidence. Independent review found that its invalid-registry Cosmos
placeholder was a functional regression and that its final-image, writable
path, container-identity, and cache-authorization contracts were incomplete.
The follow-up candidate, commit
`e5a8e496d4ce4e080fe56c8ec1f97462ecf98788` (tree
`f8603d2c5d5f7f5fe90bb39bd1b65f3254e72d0a`), is also preserved as rejected
evidence. Independent review found an RDMA regression, same-document
authorization, non-atomic cache journaling/migration, incomplete tenant and
block-device boundaries, and unsigned NIM Operator descendants. This document
also preserves rejected successor `e706968afdb9387081231151978168b52404e717`
(tree `cb93f68b36ef43011c8b9322711eaf84158390ed`). Its final review found
incomplete NIM descendant binding, a post-gate residency holder, an asserted
rather than enforced cache fence, an incomplete Restricted validator, disabled
CUDA/CRIU paths, and an inexact ModelExpress qualification. This document
describes the next additive successor; it does not convert any rejected commit
into integration or deployment approval.

## Security contract

The final controller-rendered model Pod now applies a restricted security
envelope after cache and fast-start adapters have finished mutating the Pod:

- Ordinary Pod execution is non-root and uses `RuntimeDefault` seccomp and
  AppArmor, strict supplemental-group merging, disabled host namespaces, no
  sysctls, and only Restricted volume source types.
- Every application, init, sidecar, and declared ephemeral container disables
  privilege escalation, drops all Linux capabilities, uses a read-only image
  filesystem, and resolves an explicit reviewed non-zero `runAsUser`.
- `IPC_LOCK` is admitted only for the exact ModelExpress `nixl-rdma` runtime or
  exact locked host-memory holder named by a signed compatibility record. All
  other ModelExpress transport/model/pool tuples remain cap-free; Terraform
  enumerates every enabled model and qualified pool rather than accepting a
  container-count proxy.
- Every final container image is digest-pinned and is checked against the same
  Terraform-qualified private registry as the selected runtime. This validation
  occurs after snapshot, cache, and transport adapters have finished rendering.
- Every final container receives a bounded `emptyDir` at `/tmp`. `HOME`, XDG,
  framework, compiler, and JIT cache variables are either injected below that
  mount or must already resolve below an explicit writable mount. Every
  mount—read-only as well as writable—must resolve to its exact reviewed volume
  source digest, read-only bit, and safe literal `subPath`; `subPathExpr`,
  `volumeDevices`, unmounted volumes, forbidden source types, and unbounded
  `emptyDir` volumes are rejected. Immutable model and reference-data mounts
  remain read-only.

Terraform applies the same envelope to every selected Deployment before
placement, cache-claim, and autoscaling rendering, whether the model is static,
controller-eligible, or controller-ineligible. It does so only when an exact
`runtime_security_compatibilities` record binds the final model ID, container
class/name, immutable image, non-zero UID/GID, Pod groups, bounded `/tmp` size,
all required writable environment paths, the complete mount inventory, exact
security profile, external review digest, and canonical binding
digest. The final-render precondition checks every application, init, and
ephemeral container. Missing records do not fall back to the image's default
USER or to a generic writable-path guess.

The same records are part of the controller bundle and its template digest.
After snapshot, cache, residency, or transport adapters run, the controller
again requires an exact record for every final container before enabling the
read-only root. Adapter-added helpers are therefore ineligible until their
exact image and writable-path compatibility is reviewed; Terraform ownership
or prior direct-source rendering is not an exception.

The final effective-Pod check is repeated immediately before the controller
publishes a workload and after the scientific startup-policy adapter returns.
The controller applies that same gate to each generated host-memory residency
DaemonSet after it is appended, including the writable receipt PVC and its
content-addressed agent ConfigMap. A locked holder has one signed `IPC_LOCK`
exception and otherwise retains the ordinary envelope.

Previously qualified serving and scientific CUDA/CRIU paths are not silently
disabled. Their exact runtime, tools, address helper, immutable images,
bounded scratch, mount inventory, root identities, capability sets, and
Unconfined profiles require externally signed, digest-bound exception records.
Ordinary model rendering remains available when a snapshot bundle lacks those
records; only that unapproved bundle is withheld. Scientific read-only
reference `hostPath` is likewise an explicit signed exception over the final
security projection, never a claimed Restricted volume. Terraform-owned static
manifests refuse any pre-existing added capability or `subPathExpr` instead of
erasing it. The dynamic ModelExpress path remains supported only for its exact
signed `nixl-rdma` runtime record and exact `IPC_LOCK` capability.

Compatibility, image-promotion, scientific-image, cache-boundary, and cache
quiescence records cannot authorize themselves. Terraform invokes a checked-in
Ed25519 verifier over exact no-follow evidence and attestation. Authority comes
only from the canonical
`/run/fs2-runtime-security/platform-security/authority.json` document: every
path component must be root-owned and not group/other writable, and the
document fixes the independent authority, active session, bounded validity
window, and trusted public keys. Caller-supplied trust paths, digests, keys, and
sessions are rejected. The verifier checks file digests, freshness/session, signature/key
identity, exact subject, and exact independent-review claims before projecting
an authorization. Terraform projects that exact verified key set into the
runtime trust mount. The model-controller loader and scientific control plane
then reopen the embedded
canonical evidence and signed envelope and independently verify them against
the separately mounted public trust roots. Reviewer or decision strings inside
the execution map are not authority.

The native catalog renderer gives the runtime an explicit UID/GID, bounded
`/tmp`, and writable HOME/XDG defaults. The scientific renderer validates that
the model runtime and every companion/tool container use the same immutable
registry, assigns all containers the exact stage UID/GID, and supplies an
attempt-local bounded `/tmp`, a workspace bounded by the stage ephemeral-storage
limit, and HOME/XDG paths for every helper. Terraform
independently requires every scientific stage and companion image to be
digest-pinned beneath the accelerator contract's approved private registry.
The static Cosmos3-Nano manifest retains its explicit scratch and runtime-cache
mounts, so localization and media-generation paths remain writable.

NIMCache and disabled-at-zero NIMService rendering require a signed
`nim-operator-security-subject/v3` for the exact model, CR kind, private
digest-pinned descendant image, operator digest, admission-policy digest, Pod
service account/token and host-namespace state, exact CR and descendant
admission actors, exact controller owner reference including UID, every final
volume source digest, every read-only and writable mount, every container
class/image/security context, the NIMCache `modelPuller` or disabled NIMService
repository/tag fields, and the prohibition on block devices. Each signed container also carries an
explicit non-zero UID/GID; companion images may differ from the runtime image
but must have their own immutable private image and exact security/mount entry.
The catalog exposes both the exact descendant validator and an AdmissionReview
handler for the actual operator-created Pod, enumerating init, application, and
ephemeral containers. Admission verifies the external signature before trusting
the signed actor and then compares the request actor, owner, complete volume
and mount closure, and effective Pod. This static candidate does not install an
admission server or policy. No NIM CR may be applied by an integration lane
until the signed policy digest is installed and this handler is connected
fail-closed for both CRs and descendants.
NIMService remains at zero and route-disabled until that descendant admission
succeeds; a tag-to-digest annotation alone is not activation authority.

The `fs2-models`, academic-runtime, and reference-data namespaces declare Pod
Security Admission `restricted` audit and warning labels. Enforcement is not
enabled by this source candidate. Ordinary model Pods must produce zero
Restricted audit/warning violations. The narrowly signed `IPC_LOCK`,
CUDA/CRIU, and read-only `hostPath` Pods are deliberate, separately enumerated
exceptions and must not be reported as zero-violation Restricted Pods.

## Shared-cache ownership

Every scientific runtime now mounts only its signed tenant-plus-model
first-level directory from the RWX claim at `/cache` as the final Pod's exact
safe `subPath`. The execution map carries a unique non-zero UID/GID and an
explicit legacy identity for each `(tenant_id, model_id)` boundary. The
controller and Terraform reject reuse of a tenant/model tuple, directory, UID,
or GID; identity drift; foreign current or legacy supplemental groups; or an
environment path escaping that exact directory before Terraform prepares the
mode-`2770` boundary. Only the model container receives the cache mount. Its
Pod uses `supplementalGroupsPolicy: Strict`, carries the unique current
identity, and receives only its own reviewed legacy group.

The root ownership Job uses a dedicated tokenless
`fs2-scientific-cache-bootstrap` service account in each cache namespace. No
Role or RoleBinding is created for it, and model service accounts are never
assigned to the Job. Terraform remains the only declared Job owner. Each
namespace's ownership contract contains only directories consumed in that
namespace. Ownership contract v3 declares a
`journaled-dual-access-legacy-group` phase and requires signed exact
zero-writer evidence plus a nonblocking exclusive filesystem lease. The signed
quiescence subject now also binds a fresh, at-most-15-minute observation,
expiry, zero active writers, an enforced admission fence, and the exact
activation ID that is signed into each tenant/model boundary and annotated on
subsequent Pods. It binds the exact generated policy digest, observed policy
UID/resourceVersion, binding name, fixed lock name, lock device/inode, and
canonical lock-content digest. The additive lock initializer creates that
root-owned mode-`0444`, single-link inode with `O_EXCL`, fsyncs it, and never
truncates, replaces, or deletes an existing inode. Every
cache-writing stage is required to use the generated stage runner, mounts that
inode read-only, and holds a shared lock for the complete child-process lifetime;
the bootstrap holds the nonblocking exclusive lock. Existing writers therefore
make migration fail closed, rather than racing the journal. Helm remains ordered
after successful bootstrap, so the new activation cannot admit writers before
the transaction completes. Using
stable directory descriptors and `O_NOFOLLOW`, the bootstrap refuses symlinks,
special files, cross-boundary hard links, foreign UIDs, or world-accessible
entries. It inventories the whole tree and requires exact entry-key and inode
equality again immediately before the first mutation.

The immutable journal is published as fully fsynced content-addressed bytes
followed by an atomic commit-marker directory. A crash may leave an ignored
staging inode but never a partial committed journal; retry removes or
overwrites nothing. Every entry is recorded for rollback before any mutation.
The bootstrap grants the legacy group its mirrored access before changing UID,
then restores set-ID bits after `fchown`; rollback restores the legacy owner
before narrowing permissions. The
recovery accepts every original/desired UID, GID, and mode Cartesian intermediate
state across these syscalls. On failure it restores all journaled
metadata. For an accepted tree it preserves every byte, moves ownership to the
signed tenant/model UID, retains the legacy GID, adds matching group permission
bits, and sets setgid on directories. A root-created new-empty directory is
accepted only as that empty boundary root, journaled before mutation, and then
moved to the same signed UID; root ownership is not a steady state.
Existing nested compiled entries and newly created entries are therefore
available to both the old and new identities without a delete, copy, or cache
reset. Claim sizes, storage classes, observability, and cache reuse are
unchanged.

The cluster-scoped `ValidatingAdmissionPolicy` and Deny/Audit binding are the
writer fence: after activation, a Pod may mount the claim only as the one exact
tenant/model scientific stage (two projections: its cache directory and the
read-only lock) or as the fixed-image, fixed-program, tokenless migration Job
owned by the dedicated Terraform Job identity. It rejects other
containers, foreign groups, block devices, `subPathExpr`, and extra cache
projections, so legacy/non-cooperating writers cannot start between observation
and mutation. Integration must serialize lock initialization, fence activation,
independent zero-writer/policy/inode observation, external signature, and the
migration apply; a single self-attested apply is not valid evidence.

Changing a runtime UID can expose image-internal ownership assumptions. Every
affected image therefore requires staged GPU requalification before this
candidate can be integrated or deployed.

## Image provenance

Cosmos3-Nano continues to name the previously reviewed, working, digest-pinned
`docker.io/vllm/vllm-omni` source. Source authoring does not replace it with a
nonexistent registry location.

Promotion is parameterized and fail-closed. Each final application, init, or
ephemeral container image needs a `model_image_promotions` record containing
the model ID, exact source image, exact private mirror image, provenance receipt
SHA-256, and a canonical binding SHA-256 over those fields. Source and mirror
must carry the same digest. Terraform rewrites only an exact attested source,
then rejects the final Pod unless every image is digest-pinned beneath the
accelerator contract's approved private registry and appears in that model's
promotion records. The primary promotion mirror must also equal the model image
override. Static Cosmos, helpers, sidecars, and future ephemeral containers are
therefore covered by the same final-render rule. Cold-start keeper DaemonSets
are independently checked across application, init, and ephemeral containers;
their Terraform ownership cannot bypass the promotion gate. Scientific stage
and companion images remain separately fail-closed on immutable references
beneath the accelerator contract's approved registry.

No registry was contacted and no image was copied by this task. Before rollout,
an integration owner must mirror the exact digest into the approved regional
registry and supply the promotion/override inputs from separately reviewed
provenance. Until those inputs exist, planning is intentionally blocked while
the checked-in working source reference remains unchanged. No placeholder is a
deployable fallback.

## Authored regression coverage

Source tests were updated to cover:

- the post-adapter controller envelope across application, init, sidecar, and
  ephemeral containers, including exact compatibility bindings, explicit UID,
  bounded writable paths, complete read-only/writable mount sources, rejected
  read-only `subPathExpr` and block devices, post-render residency holders,
  snapshot exception profiles, and transport-scoped `IPC_LOCK`;
- read-only/non-root native, scientific, and Cosmos runtimes;
- preservation of the reviewed Cosmos source plus exact source-to-mirror
  provenance binding and all-container private-registry qualification;
- PSA restricted audit/warning labels;
- distinct tenant-plus-model runtime-cache owners, exact safe `subPath` mounts,
  Strict group policy, foreign-group rejection, exact pre-mutation inventory,
  failure rollback, atomic immutable journal publication, stable shared-writer
  versus exclusive-migrator locking, and byte-preserving nested dual-access
  migration;
- external Ed25519 verification, same-document self-authorization rejection,
  and signed NIM CR plus actual-descendant actor, owner, volume-source,
  read-only-mount, and security validation; and
- preservation of the qualified scientific execution-map digest
  `0d0baff84eff6ff6db3654a2231d7286f10eebe0436295ada264578047224709`
  plus independently derived final-render cache owner projections.

Per coordinator direction, none of these tests, formatters, Terraform/Helm
checks, builds, package managers, scanners, or live probes were executed.

## Integration, verification, and rollback gates

Before integration, run the focused controller/catalog tests, the repository's
offline suite, formatting checks, Terraform validation, Helm rendering, and a
restricted-PSS static audit from an authorized integration lane. Before a
shared rollout, reconcile with the currently deployed commit and record the
previous image digests and release revision.

Stage the change with the exact private Cosmos image and the scientific
runtime images on task-owned GPUs. Verify startup, readiness, two semantic
requests, media output, scientific result publication, compiler-cache reuse,
queue admission, and observability. The required ordinary-runtime security
result is zero `restricted` audit/warning violations. Separately enumerate and
approve the signed `IPC_LOCK`, snapshot, and hostPath exceptions and prove that
no other violation is present. Treat any runtime UID,
read-only-root, or capability compatibility failure as a failed cohort; do not
weaken the security envelope to obtain a passing result.

Rollback is the prior reviewed deployment revision and image set. During the
documented journaled dual-access phase, the previous runtime retains access via
the legacy group while the immutable journal preserves the former UID/GID/mode
for an explicitly authorized metadata rollback, so rollback does not require a copy,
delete, or cache reset. Finalizing directories to current-only ownership is a
separate migration and is explicitly outside this source candidate. This task
does not authorize deleting or cleaning cache data.
