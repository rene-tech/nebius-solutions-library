# Purpose-bound operator and backend access v6

This source-only successor supersedes v5. It does not authorize integration or
deployment. Production trust, provider adapters, and accepted SAI-08/SAI-09
integration remain absent; SAI-06 has static SOURCE GO only. No live state,
credential, Secret, customer data, cloud resource, cluster, database, or
registry was read or changed for this revision.

## Optional declarations and authoritative observations

The durable registry is the exhaustive classification policy, not an assertion
that every optional or dependency-owned address already exists. An absent
declared address is recorded as `uninstantiated_declared_addresses`, and an
absent credential class is `not-observed`. Neither is rotation-ready. Every
resource that *is* present in authoritative remote Terraform state, the whole
cluster Secret inventory, Helm release storage, or project IAM inventory must
still be classified; an unmanaged observed resource fails closed.

Fixed generation-one Secret resources may predate custody annotations. Their
class and generation are derived only from the exact source registry address,
never from mutable annotations. Admission still requires exact state/live
namespace, name, UID, resourceVersion, decoded-byte content commitment, and
`immutable=true`. A partial, stale, or contradictory annotation fails closed.
Composite version keys such as `2:owner` and `2:consumer` are generation two;
only the leading positive numeric component is the generation.

An expiry-required identity is usable only when its provider timestamp parses
and is later than the authority's current UTC time. A non-null past timestamp
does not satisfy custody or rotation readiness.

## Local purpose separation

Five operating purposes have distinct Unix UID/GID pairs, unified-cgroup
paths, and digest-pinned Python executable and client-script identities:

- release automation;
- operator read/status;
- operator proxy;
- general PAT delivery; and
- scientific PAT delivery.

The client checks its local purpose before connecting. The root authority
independently uses `SO_PEERCRED`, `/proc/<pid>/cgroup`, `/proc/<pid>/exe`, and
the exact two-element interpreter/script command line. Its source-fixed
operation map permits each identity to request only its own context. The
authority injects that kernel-derived purpose into the provider request and
externally anchored audit record. A human or status process cannot request a
release, proxy, or credential-delivery identity merely because it can reach
the socket.

The proxy identity is bound to one `fs2-system` Role and RoleBinding, including
the Role/RoleBinding UID and resourceVersion, canonical rules and subjects
commitments, and the selected ServiceAccount UID and resourceVersion. Its
allowlist is namespace-qualified. A ClusterRoleBinding or permission in
another namespace cannot satisfy the contract. The proxy kubeconfig is
root-owned, purpose-group-owned mode 0640; the proxy validates that exact
custody rather than incorrectly requiring mode 0600.

## Purpose-bound remote-state access

Ambient `AWS_*`, `S3_*`, `NEBIUS_PROFILE`, and `NEBIUS_CONFIG` values are
removed. Every command receives a separate provider-observed workload-identity
session with audience `terraform-backend:<purpose>`, a one-hour maximum
lifetime, non-human actor, and impersonation disabled. Its root-owned mode-0640
configuration is readable only by that purpose's UID/GID. The root authority
uses a separate mode-0600 `authority` backend session.

The S3 backend configuration contains no credential and is root-owned mode
0644 under a non-writable parent chain. Backend-custody and additive state-copy
observations must echo the exact provider backend-session commitment. The
wrapper compares that commitment with the active purpose identity before
Terraform initialization. Status, proxy, scoped output, plan, and apply
therefore neither consume ambient workstation credentials nor share one
backend principal.

## Rollback and stop condition

Rollback remains forward-only: add a new source revision selecting a retained,
previously verified generation. Never restore exposed files, overwrite fixed
generations, remove journal records, revoke credentials, or discard historical
ciphertext. The remote-state migration remains additive exact-copy with the
legacy source retained.

Only static, deletion-free inspection is permitted for this revision. No test,
build, formatter, Terraform, Helm, live provider, cluster, database, registry,
credential, or cleanup command is authorized. Integration and deployment stay
blocked until the production authority configuration, adapters, trust bundle,
accepted dependency lineage, and independent review all pass.
