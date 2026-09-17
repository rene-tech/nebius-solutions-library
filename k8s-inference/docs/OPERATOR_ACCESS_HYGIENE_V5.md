# Provider lifecycle and purpose-scoped operator access v5

This source-only successor refines v4 without treating any rejected predecessor
as accepted. Integration and deployment remain disabled: the checked-in
external trust allowlist is empty, production adapters are not configured,
SAI-06 has static SOURCE GO only, and accepted SAI-08/SAI-09 integration
lineages are absent. No state, credential, cloud, cluster, database, registry,
or customer data was read or changed for this revision.

## Source custody is not lifecycle authority

Terraform state and live immutable Secret bindings establish value-free source
custody only. Their inventory status is "source-observed"; it never asserts
that a credential is active, disabled, expired, or eligible for rotation.
Every adoption and reconciliation must separately call the exact class's
digest-pinned rotation-readiness adapter. That adapter must return the
provider-native predecessor and successor identities, both active, with exact
class, owner, project, purpose, generation, distinct ID/fingerprint, readers,
and provider-enforced expiry where the registry requires one.

The adopt-successor command requires owner ID and project ID arguments; neither
value is inferred from the observed predecessor. The provider-derived lineage
must equal both arguments. The journal records the exact lifecycle identities
and externally anchored readiness evidence. It adopts only a separately staged
successor and performs no create, update, disable, revoke, replace, or delete.

All class operations carry the same authority-recomputed source-trust record
and exact Secret UID/resourceVersion/content/generation bindings. The
dual-read and current-write phases run every operation declared by the class
contract:

- consumer-readiness proves every exact consumer is ready;
- rotation-readiness re-proves both lifecycle identities;
- authentication-continuity, when declared, proves both generations
  authenticate and the predecessor remains enabled; and
- ciphertext-migration, when declared, proves pre-existing old-key reads,
  successor reads and writes, rollback reads, and tenant/principal binding.

Ciphertext evidence always returns retirement_allowed=false in this no-delete
workflow. No historical generation is discarded.

## Purpose-separated operator identities

One credential is no longer reused for status, tunnelling, and Secret
delivery:

1. operator-read-context uses the expiring provider-bound project viewer for
   configuration validation and status. Its comprehensive denial matrix still
   denies workload mutation, Secret access, service-account token creation,
   exec/attach/port-forward, escalation, and impersonation.
2. operator-proxy-context uses a distinct Kubernetes projected token with a
   maximum 15-minute lifetime. Its exact allowlist is pod get/list, Service
   get, and pods/portforward:create; Secret access, ordinary Pod creation,
   exec, workload mutation, token minting, and impersonation remain denied.
   The internal customer/support proxy therefore remains functional without
   granting the handoff viewer a tunnel subresource.
3. scoped-credential-context uses a distinct projected token with a maximum
   five-minute lifetime for either general-access or scientific-access.
   It can get exactly one named current-generation Secret and cannot list
   Secrets or read another Secret. Admin bootstrap and Grafana credentials
   remain non-exportable.

Every kubeconfig is root-custodied, mode 0640 for the sole kernel-authenticated
client UID/GID, digest-pinned, and backed by a pinned provider authorization
adapter. The wrapper rejects ambient profiles and caller-selected paths or
executables.

Before decoding a scoped PAT, the wrapper compares the returned Secret's
namespace, name, UID, resourceVersion, and canonical decoded-value commitment
with the authority observation. A rotation or replacement between observation
and read fails closed. Only the explicitly requested key is decoded in memory;
the token is never printed, while the output and value-free receipt are written
to new owner-only files.

## Rollback and stop condition

Rollback is forward-only and selects a retained, previously verified generation
through a new versioned source change. It never restores exposed plaintext
artifacts, overwrites a fixed generation, drops a journal event, revokes a
credential, or discards ciphertext history.

This revision has static parse and source-review evidence only. Dynamic tests,
Terraform/Helm commands, provider reads, and live acceptance remain blocked by
the hard no-delete constraint and missing production trust/adapters/dependency
acceptance. It is not deployment authorization.
