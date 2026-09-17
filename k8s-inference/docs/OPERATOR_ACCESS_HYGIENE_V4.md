# Provider-bound operator custody and release admission v4

> Source contract v5 supersedes the lifecycle and purpose-scoped identity
> details in this document. See OPERATOR_ACCESS_HYGIENE_V5.md; this v4 text is
> retained as rejected-lineage context and is not deployment authorization.

This revision supersedes the v3 source contract without treating any earlier
candidate as accepted. It remains fail-closed and source-only: there is no
production authority configuration, accepted external trust bundle, provider
adapter deployment, state copy, credential action, or rollout evidence in this
repository.

## Release and operator identities

Release admission accepts only a provider-observed
`workload_identity_session`. Persistent access keys and authentication keys
cannot satisfy the contract. A digest-pinned provider adapter must prove the
session ID, service-account principal, project, issuer, token-exchange source,
`terraform-release` audience, provider issue/expiry times, and that human,
interactive, and impersonation use are disabled. Its maximum lifetime is one
hour.

A second provider adapter must enumerate the service account's complete
authorization closure across the organization and descendants. The exact group
membership and project permits must equal policy; direct, inherited-extra,
cross-resource-extra, and impersonation permits fail admission. The release
config, every executable, every adapter, and their parent paths are fixed by
root-owned policy and digest. Wrapper CLI flags cannot select alternate
executables, profiles, projects, or configuration files.

The authority service remains root-only, but the Terraform wrapper is not.
Release configuration, operator kubeconfig, and remote-backend configuration
therefore use an exact root-owned, mode-`0640`, dedicated-reader-group custody
contract. The authority binds both the sole client UID and GID through
`SO_PEERCRED`; the wrapper requires that same effective UID and group before it
opens a file. No file is world-readable and no ambient profile or caller path
is accepted. The authority's own configuration, evidence configuration,
legacy states, and global Secret-inventory kubeconfig remain root-owned mode
`0600`.

The retained operator proxy uses a separate existing viewer authentication key
and a fixed root-owned kubeconfig. The authority proves its provider expiry,
service-account/project/group/permit lineage, complete authorization closure,
pod inventory access, the comprehensive Kubernetes denial matrix (including
Secret reads, service-account token creation, mutation, RBAC escalation and
impersonation), and semantic CIDR equality. The proxy never calls
`get-credentials`, creates a key, replaces a kubeconfig, or borrows the release
identity. Customer request-debug traffic remains available through that
read-only tunnel; its application-level scoped capture contract is unchanged.

Global Secret inventory does not reuse the operator handoff. It requires a
separate root-private kubeconfig with an exact digest and a pinned adapter that
proves the Kubernetes ServiceAccount ID/name/namespace/UID, projected
credential ID, issuer, `credential-inventory` audience, issue/expiry times,
and an at-most-one-hour lifetime. The observed RBAC closure must equal only
Secret get/list and ServiceAccount list, while explicitly denying Secret and
workload mutation, pod execution, token creation, impersonation, and RBAC
escalation. An unrelated, group-readable, expired, or unpinned kubeconfig
cannot enumerate custody.

`scripts/operator_handoff_readonly.py verify` is the only production handoff
entrypoint named by the deployment contract. The older
`scripts/operator_handoff.py` is retained as rejected-lineage evidence under
the no-delete directive; it is not a production component, and its legacy
issuance/revocation implementation must not be invoked.

## Exhaustive inventory

Terraform-managed Secrets are admitted only when every reviewed base address
is present in the authoritative remote state and its provider ID joins one
live namespace/name. UID, resourceVersion, immutable=`true`, credential class,
generation, and canonical decoded-value commitment are all mandatory and must
equal live metadata. Missing/blank UID or resourceVersion, mutable Secrets,
class laundering, and a create at a reviewed address paired with a move/remove
to an unreviewed address all fail closed. A name-only state match is
insufficient.

Helm storage Secrets require an exact digest-pinned provider-adapter binding of
namespace, name, UID, resourceVersion, decoded-content commitment, release,
revision, and status. The regex is only a candidate filter. Service-account
token Secrets require exactly one ServiceAccount ownerReference whose name and
UID equal both annotations and a live ServiceAccount UID. Inventory exemptions
must have a provider ID, owner, purpose, readers, source, and an unexpired
maximum-30-day expiry.

The registry contains the normative addresses for all 21 credential classes,
including the eight PostgreSQL backup IAM/Secret addresses from the SAI-06
static-source-GO successor. Every class declares its applicable readiness,
rotation, ciphertext, and authentication operations and requires one pinned
production adapter. SAI-06 integration/live acceptance is still pending; SAI-08
and SAI-09 still lack accepted source successors in this lineage. Those facts
block operation and are not papered over as acceptance.

The guard and rotation journal parse the same five-field contract for all 21
classes, with no pending-class exception. Each readiness request carries the
current externally verified class source trust, its complete contiguous
retained-generation set, and only that class's exact Secret bindings; an empty
Secret set is valid only for a provider-only class whose exact
Terraform/provider bindings prove its source.
The authority independently recomputes both maps from its remote states and
live inventory. Class adapters receive only those scoped sources, never the
global Terraform, Kubernetes Secret, or IAM maps. Consumer evidence binds each
declared consumer to the hash of that exact class source.

## Legacy state copy and backend binding

Each Terraform root must identify one quarantined, root-owned legacy state by
path, raw SHA-256, canonical state SHA-256, lineage, serial, retention reason,
and retention expiry. Migration is an additive copy protocol:

1. prove the exact retained source and an exact empty destination;
2. create a new remote object version through a separately authorized process;
3. provider-observe the destination object version and prove its canonical
   state digest, lineage, and serial equal the retained source; and
4. cut consumers over only after the externally anchored observation is
   accepted.

`inference-stack` invokes `state-migration-readiness` before every remote
backend initialization. Initialization requires status
`copy-verified-source-retained`, a nonempty destination object version, equal
source/destination canonical state hashes, exact lineage and serial,
`source_retained=true`, and `overwrite_performed=false`. An empty destination
is valid pre-copy evidence but cannot initialize, plan, apply, proxy, or cut
over the remote root.

The read-only authority implements the pre-copy and post-copy observation. It
does not perform the copy. It rejects overwrite evidence and requires the
source to remain present. The active no-delete constraint forbids source
retirement; rollback selects the retained source or prior remote object version
without restoring exposed run-root files or discarding a credential generation.

Backend custody must equal the source-approved project, bucket, parent, owner
service account, object key, HTTPS endpoint, KMS key/parent/project, access-log
destination/parent/project/prefix, versioning, and object-lock mode/retention.
Boolean claims about encryption or logging alone are insufficient.

## Current admission status

Deployment and integration remain unauthorized. The source trust allowlist is
empty; no production workload-session, authorization-closure, Helm inventory,
class, backend, or state-copy adapter configuration exists; SAI-06 is static
SOURCE GO only; and SAI-08/09 are not accepted dependencies. Dynamic tests and
live verification were not run under the hard no-delete directive.
