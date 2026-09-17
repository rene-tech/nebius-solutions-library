# SAI-16 source handoff

This document describes a static source candidate. It is not integration,
deployment, live verification, or security acceptance evidence.

The exact parent `375a88e363f13c8f80e96c2abf4a3f58e109a93e` / tree
`59b235e3fe5e5cb65bb9ad6c8342a79e7380a817` is preserved as SOURCE NO-GO.
It corrected the earlier aligned-window, quota, and copied-history defects, but
its empty-ring migration was not mixed-version safe and its provider inventory
remained self-authenticating. Every prior rejected commit remains provenance;
this successor is additive.

## Session-exchange admission

The production and deterministic in-memory stores implement the same exact
sliding-window contract over admitted attempts. The interval is
`(decision_time - window, decision_time]`: the exact keyed-source limit takes
precedence, followed by one exact global ceiling. There are no UTC-aligned
resets, source-derived aggregate shards, local quotas, or admission collisions.

PostgreSQL owns the authoritative production clock. The in-memory test store
uses its injected timestamp as the deterministic clock. Additive migration
`0033` retains rejected migration `0032` as negative provenance and moves
runtime admission to a fixed 10,000-slot ring containing the exact last
admitted events. Time and `(source,time)` indexes support the two exact counts.
Only a potentially admissible request takes the singleton cursor lock and
re-checks both ceilings; a slot cannot be reused while its event is active.
The singleton also binds the configured window and ceilings on first use;
changing them without a separately reviewed state transition fails closed,
because a bounded ledger cannot reconstruct events discarded under a shorter
historical window.

Migration `0034` repairs the `0032`/`0033` rollout boundary without rewriting
either rejected migration. The migration runner publishes the schema version
which existed before its serialized transaction. If any prior migration
pre-existed—or provenance is absent—both the legacy and exact-v2 function
signatures enter one shared bridge and refuse every exchange for one complete
configured window. The refusal is read-only after the first settings bind.
Only then can the exact ring admit. Only a transaction-proven empty installation
can start immediately; a pre-limiter upgrade also quiesces because its earlier
audit-backed or process-era budget cannot be reconstructed exactly. Both
function signatures retain runtime `EXECUTE`, so old
rolling replicas and source-forward rollback replicas make the same decision.
Rollback keeps the successor migration/schema-wait image through
`migration.compatibilityImage` while selecting the prior application image;
it never reverses schema or discards limiter state.

Read-only source identities for this isolated lineage are: migration `0034`
SHA-256 `3b7e2df9858857afdb3a99c32aa3e73e205a63f30614122604eb2269b4b1eb10`,
34-entry ordered migration-set SHA-256
`31a94c628489cec61a8288b653b59c9eab96b7ba58e9642a5bb22e6f185efb92`,
and release-contract payload SHA-256
`eb37ee6ba8a70b105ec77e7dd6d8eb06dc724a07a053d9281a891de15bd1f571`.

The first rejection transition may write one bounded evidence slot and one
audit event saying only `one_or_more`; it never claims an exact rejection
count. Once established, every DB-reached rejection is a read-only fast path:
no row lock, counter update, or new audit row. An unexpired forensic-slot
collision suppresses evidence only and cannot affect admission. Per-process
bounded caches avoid even the read on ordinary repeats. The state is bounded,
indexed, and has no delete path.

## Model-bootstrap recovery

`models.bootstrap` assertions carry both a signed `resource_sha256` and signed
`resource_generation`. The request repeats that generation inside the
digest-bound payload; the API requires both fields to match. The external
Secret name is exactly `fs2-release-model-bootstrap-<generation>`.

Every generation's immutable ConfigMap retains canonical payload JSON, exact
runner source, and a v2 identity containing the digest-pinned image, assertion
identity, public endpoint and complete immutable Job security/resource spec.
Provider discovery is evidence, not authority. Before an object becomes an
import target, the external automation-only release identity publishes an
immutable public compact-JWS receipt. The receipt binds the generation and
identity digest, ConfigMap UID and canonical full-object digest; a terminal
receipt additionally binds the Job UID and a digest over the entire observed
Job object. It also binds the already consumed release
assertion receipt. A ConfigMap-only partial-apply receipt is permitted before
assertion consumption, but cannot attest a Job.

Terraform verifies Ed25519 against the same immutable public trust document
used by the control plane and imports only receipt-verified objects.
Fail-closed admission admits trust and receipt creation only from
`system:serviceaccount:fs2-system:fs2-release-identity`; trust, receipt,
ConfigMap, and Job updates/deletes are denied. Delete/recreate changes the
Kubernetes UID and
invalidates the signed chain. An injected self-consistent pair without the
external receipt fails planning. The legacy caller-copied retention map remains
empty.

ConfigMaps and zero-retry, non-root, tokenless Jobs remain generation-keyed and
protected with `prevent_destroy` plus cluster admission. A separate fail-closed
ValidatingAdmissionPolicy, installed before Secret creation, admits only
immutable, generation-labeled, uniquely named Secrets containing exactly the
`assertion` key.

## Parent integration dependencies

- SAI-10 independently owns `0030_customer_storage_credentials.sql`. SAI-16
  retains `0032_session_exchange_buckets.sql`, its `0033` exact-sliding
  successor, and additive `0034` mixed-version bridge; parent integration must
  retain all exact sibling histories and regenerate the combined immutable
  migration manifest. This branch does not copy or modify SAI-10.
- Before integration, security-owned release automation must independently pin
  the initial trust ConfigMap UID/digest and signer custody, then install the
  trust/history/receipt policies before it publishes any retained-history
  receipt. This source candidate does not manufacture that external authority.
- SAI-01 independently owns request-capture exclusions and the 90-day debug
  expiry/purge contract. This isolated SAI-16 lineage still predates those
  changes, so it must not be integrated alone over accepted SAI-01 work. This
  ticket does not edit request-debug capture, inference, MCP, operations,
  storage, or retention behavior.

The coordinator's static-only boundary prohibited executing authored tests,
Terraform, builds, formatters, scanners, package managers, or live probes.
