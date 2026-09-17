# SAI-25 model runtime hardening source candidate

Status: source candidate only. This change has not been built, tested, deployed,
or checked against a live cluster or registry. The 2026-09-17 coordinator
boundary permits static source authoring and read-only Git inspection only.

## Security contract

The final controller-rendered model Pod now applies a restricted security
envelope after cache and fast-start adapters have finished mutating the Pod:

- Pod execution is non-root and uses `RuntimeDefault` seccomp.
- Every application and init container disables privilege escalation, drops all
  Linux capabilities, and is non-root.
- The selected model runtime container has a read-only image filesystem.
- Writable model payloads, compilation caches, `/tmp`, and scientific workspaces
  remain explicit volume mounts. The cache and media delivery paths are not
  removed or made read-only.

The native catalog renderer and scientific batch renderer apply the same
runtime-container controls. The static Cosmos3-Nano manifest also mounts `/tmp`
for its localizer before making the localizer image filesystem read-only, so
localization and media-generation scratch paths remain writable.

The `fs2-models`, academic-runtime, and reference-data namespaces declare Pod
Security Admission `restricted` audit and warning labels. Enforcement is not
enabled by this source candidate; rollout owners can observe violations before
any admission behavior changes.

## Shared-cache ownership

Models that mount `fs2-scientific-runtime-cache` use stable, distinct UID/GID
identities:

| Model | UID/GID | Namespace |
| --- | ---: | --- |
| Mosaic | 11001 | `fs2-models` |
| OpenFold3/OpenBind | 11002 | `fs2-models` |
| Protenix v2 | 11003 | `fs2-models` |
| AlphaFold3 | 11004 | `fs2-academic-poc` |

The controller rejects identity drift between cache-consuming stages and
cross-model UID/GID reuse. Terraform independently rejects the same collisions
before preparing mode-`2770` model directories. The RWX claims, compiled-kernel
cache locations, directory names, storage class, and bootstrap ownership model
remain otherwise unchanged.

Changing a runtime UID can expose image-internal ownership assumptions. Each of
these four images therefore requires staged GPU requalification before this
candidate can be integrated or deployed.

## Image provenance

Cosmos3-Nano no longer names Docker Hub as a deployable source default. Its
catalog and manifest use the existing invalid-registry placeholder with the
same immutable upstream digest. Workload qualification now requires every
runtime override to reside below the private registry FQDN from the accelerator
pool contract.

No registry was contacted and no image was copied by this task. Before rollout,
an integration owner must mirror the exact digest into the approved regional
registry, provide that private digest-qualified override, and record provenance
and scan evidence. A placeholder reference must never be deployed.

## Authored regression coverage

Source tests were updated to cover:

- the final controller security envelope and capability removal;
- read-only/non-root native, scientific, and Cosmos runtimes;
- Cosmos private-image placeholder and private-registry qualification;
- PSA restricted audit/warning labels;
- distinct runtime-cache owners plus rejection of per-model identity drift; and
- the new canonical scientific execution-map digest and owner projections.

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

Rollback is the prior reviewed deployment revision and image set. Cache data is
disposable derived state, but this change does not authorize deleting or
cleaning it. Rollback must restore the previous UID/GID directory ownership only
through an independently reviewed, non-destructive migration plan.
