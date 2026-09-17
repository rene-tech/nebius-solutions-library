# SAI-25 model runtime hardening source candidate

Status: source candidate only. This change has not been built, tested, deployed,
or checked against a live cluster or registry. The 2026-09-17 coordinator
boundary permits static source authoring and read-only Git inspection only.

The first candidate, commit `c483b90a7e7f9661fc27f694e4934bd4c14b72b5`
(tree `35856e2f58f5f8b5aeb46f159ba6fc9d7c2b7199`), is preserved as
rejected evidence. Independent review found that its invalid-registry Cosmos
placeholder was a functional regression and that its final-image, writable
path, container-identity, and cache-authorization contracts were incomplete.
The follow-up exact review additionally found that Terraform-owned/static
renders bypassed the dynamic hardener and that changing only top-directory
ownership did not preserve nested legacy cache access. This document describes
the additive successor; it does not convert either candidate into integration
or deployment approval.

## Security contract

The final controller-rendered model Pod now applies a restricted security
envelope after cache and fast-start adapters have finished mutating the Pod:

- Pod execution is non-root and uses `RuntimeDefault` seccomp.
- Every application, init, sidecar, and declared ephemeral container disables
  privilege escalation, drops all Linux capabilities, uses a read-only image
  filesystem, and resolves an explicit reviewed non-zero `runAsUser`.
- Every final container image is digest-pinned and is checked against the same
  Terraform-qualified private registry as the selected runtime. This validation
  occurs after snapshot, cache, and transport adapters have finished rendering.
- Every final container receives a bounded `emptyDir` at `/tmp`. `HOME`, XDG,
  framework, compiler, and JIT cache variables are either injected below that
  mount or must already resolve below an explicit writable mount. Immutable
  model and reference-data mounts remain read-only.

Terraform applies the same envelope to every selected Deployment before
placement, cache-claim, and autoscaling rendering, whether the model is static,
controller-eligible, or controller-ineligible. It does so only when an exact
`runtime_security_compatibilities` record binds the final model ID, container
class/name, immutable image, non-zero UID/GID, bounded `/tmp` size, all required
writable environment paths, external review digest, and canonical binding
digest. The final-render precondition checks every application, init, and
ephemeral container. Missing records do not fall back to the image's default
USER or to a generic writable-path guess.

The same records are part of the controller bundle and its template digest.
After snapshot, cache, residency, or transport adapters run, the controller
again requires an exact record for every final container before enabling the
read-only root. Adapter-added helpers are therefore ineligible until their
exact image and writable-path compatibility is reviewed; Terraform ownership
or prior direct-source rendering is not an exception.

The native catalog renderer gives the runtime an explicit UID/GID, bounded
`/tmp`, and writable HOME/XDG defaults. The scientific renderer validates that
the model runtime and every companion/tool container use the same immutable
registry, assigns all containers the exact stage UID/GID, and supplies an
attempt-local bounded `/tmp` plus HOME/XDG paths for every helper. Terraform
independently requires every scientific stage and companion image to be
digest-pinned beneath the accelerator contract's approved private registry.
The static Cosmos3-Nano manifest retains its explicit scratch and runtime-cache
mounts, so localization and media-generation paths remain writable.

The `fs2-models`, academic-runtime, and reference-data namespaces declare Pod
Security Admission `restricted` audit and warning labels. Enforcement is not
enabled by this source candidate; rollout owners can observe violations before
any admission behavior changes.

## Shared-cache ownership

Models that mount `fs2-scientific-runtime-cache` use stable, distinct current
UID/GID identities and an explicit legacy identity during the reversible
transition:

| Model | Current UID/GID | Legacy UID/GID | Namespace |
| --- | ---: | ---: | --- |
| Mosaic | 11001 | 10001 | `fs2-models` |
| OpenFold3/OpenBind | 11002 | 10001 | `fs2-models` |
| Protenix v2 | 11003 | 10001 | `fs2-models` |
| AlphaFold3 | 11004 | 1001 | `fs2-academic-poc` |

Every scientific runtime now mounts only its own first-level directory from the
RWX claim at `/cache`, using the derived `mosaic`, `openfold3`, `protenix`, or
`alphafold3` directory as the final Pod's safe `subPath`. The checked-in
execution map and its historical qualification digest remain byte-identical;
the controller and Terraform independently derive the same directory from the
existing cache environment paths. They reject directory reuse, identity drift,
foreign cache supplemental groups, or an environment path that escapes that
single model directory before Terraform prepares the mode-`2770` boundaries.
Only the model container receives the cache mount. Its Pod uses
`supplementalGroupsPolicy: Strict`, carries the unique current UID/GID, and
receives only its own reviewed legacy group. Although three historical models
share legacy group 10001, safe `subPath` confinement prevents any of those Pods
from mounting another model's first-level directory.

The root ownership Job uses a dedicated tokenless
`fs2-scientific-cache-bootstrap` service account in each cache namespace. No
Role or RoleBinding is created for it, and model service accounts are never
assigned to the Job. Terraform remains the only declared Job owner. Each
namespace's ownership contract contains only directories consumed in that
namespace. Ownership contract v2 declares a `dual-access-legacy-group` phase.
Using stable directory descriptors and `O_NOFOLLOW`, the bootstrap refuses
symlinks, special files, foreign UIDs, or world-accessible entries; for an
accepted tree it preserves every byte and existing owning UID, retains the
legacy GID, adds matching group permission bits, and sets setgid on directories.
Existing nested compiled entries and newly created entries are therefore
available to both the old and new identities without a delete, copy, or cache
reset. Claim sizes, storage classes, observability, and cache reuse are
unchanged.

Changing a runtime UID can expose image-internal ownership assumptions. Each of
these four images therefore requires staged GPU requalification before this
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
  and bounded writable paths;
- read-only/non-root native, scientific, and Cosmos runtimes;
- preservation of the reviewed Cosmos source plus exact source-to-mirror
  provenance binding and all-container private-registry qualification;
- PSA restricted audit/warning labels;
- distinct runtime-cache owners, safe per-model `subPath` mounts, Strict group
  policy, foreign-group rejection, dedicated bootstrap identities, and
  byte-preserving nested dual-access migration; and
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

Stage the change with the exact private Cosmos image and the four scientific
runtime images on task-owned GPUs. Verify startup, readiness, two semantic
requests, media output, scientific result publication, compiler-cache reuse,
queue admission, and observability. The required security result is zero
`restricted` audit/warning violations for model Pods. Treat any runtime UID,
read-only-root, or capability compatibility failure as a failed cohort; do not
weaken the security envelope to obtain a passing result.

Rollback is the prior reviewed deployment revision and image set. During the
documented dual-access phase, the previous runtime UID/GID retains access via
the legacy owning UID/group, so rollback does not require another chown, copy,
delete, or cache reset. Finalizing directories to current-only ownership is a
separate migration and is explicitly outside this source candidate. This task
does not authorize deleting or cleaning cache data.
