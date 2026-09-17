# Provider-bound operator custody and release admission v4

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

The retained operator proxy uses a separate existing viewer authentication key
and a fixed root-owned kubeconfig. The authority proves its provider expiry,
service-account/project/group/permit lineage, complete authorization closure,
pod inventory access, the comprehensive Kubernetes denial matrix (including
Secret reads, service-account token creation, mutation, RBAC escalation and
impersonation), and semantic CIDR equality. The proxy never calls
`get-credentials`, creates a key, replaces a kubeconfig, or borrows the release
identity. Customer request-debug traffic remains available through that
read-only tunnel; its application-level scoped capture contract is unchanged.

`scripts/operator_handoff_readonly.py verify` is the only production handoff
entrypoint named by the deployment contract. The older
`scripts/operator_handoff.py` is retained as rejected-lineage evidence under
the no-delete directive; it is not a production component, and its legacy
issuance/revocation implementation must not be invoked.

## Exhaustive inventory

Terraform-managed Secrets are admitted only when the remote state address and
provider ID join one live namespace/name and the state metadata agrees with the
live UID/resourceVersion when present, immutable bit, credential class,
generation, and canonical decoded-value commitment. A name-only state match is
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
