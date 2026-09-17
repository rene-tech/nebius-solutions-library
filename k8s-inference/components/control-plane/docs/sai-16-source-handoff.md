# SAI-16 source handoff

This document describes a static source candidate. It is not integration,
deployment, live verification, or security acceptance evidence.

The exact parent `362026705237be0ffd3eeb88353d768fb465e27f` / tree
`cdf5954c85669daa3dfa466535641193e1c0f1cc` is preserved as
SOURCE/INTEGRATION/LIVE NO-GO. It added the first mixed-version bridge and
signed full-object retention receipts, but began a blanket cutover interval
inside the migration transaction, preserved an internal runtime ACL, used an
ambient verifier and same-inventory trust root, lacked an apply-time fence and
strong admission identity/lifecycle, left assertion Secret DELETE open, and
did not platform-wire the rollback image. Every prior rejected commit remains
provenance; this successor is additive.

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
which existed before its serialized transaction. The first post-commit call
locks one shared bridge, binds the limiter configuration, checks that active
legacy source and aggregate counts agree, and imports each still-active legacy
admission into the exact ring at that call's timestamp. The synthetic entries
conservatively retain the whole prior budget for a complete new window without
resetting capacity or denying every operator/debug-login attempt. Old rolling
replicas and new replicas use the legacy and exact public wrappers over that
same bridge. The internal bridge and renamed exact-v2 function have inherited
runtime/current-role execute grants explicitly revoked.

The platform-root deployment contract requires, validates, regionally mirrors,
and forwards the independent immutable
`applications.control_plane.schema_compatibility_image` as workloads-stage
`control_plane_schema_compatibility_image`, which Terraform maps to Helm
`migration.compatibilityImage`. Rollback retains that successor
migration/schema-wait image while selecting the prior application image; it
never reverses schema or discards limiter state.

The regenerated `0034` SHA-256 is
`35ea43d1cebab826f7c52aa432ca8e3d782c5fe924400fdeb0703bdc7c4ad6e2`;
the ordered migration-set SHA-256 is
`f87489dff4d81a15ddd8496a1876e10ec9e9cfe29f243a7cd14ce767ee80ef73`;
and the release-contract payload SHA-256 is
`08c4bed91506b78ab0a15542ff59df174dabdc8b4fb13ddd52568fccf59c7eac`.
The exact successor commit/tree is recorded in the Task Deck handoff after the
additive commit is sealed.

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

Recovery now requires a policy-first apply. The lifecycle guardian and exact
history/trust/receipt/assertion/verification policies are installed before any
object may be accepted. Every CREATE is bound to a generation-specific release
ServiceAccount username, its canonical UID, and the exact Kubernetes bound
token credential ID; a reusable username alone is insufficient. The accepted
policy and binding UIDs are integration inputs. The trust binding separately
pins the trust ConfigMap UID, full-object digest, trust JSON digest, canonical
issuer/key-set digest, and policy/binding UIDs. Provider inventory recomputes
all pins and requires object creation after the relevant binding.

Terraform no longer executes `python3` plus a mutable repository verifier.
After admission is bound it creates a retained zero-retry verifier Job from the
digest-pinned schema-compatibility image. The Job has read-only resource-name
RBAC, re-reads each exact ConfigMap/Job/receipt/trust object from the API,
verifies Ed25519 and every UID/full-object digest, and records success only as
terminal Job status. Only a later plan can import that exact generation. All
source objects and the verifier Job are append-only, so there is no plan/apply
replacement window. An injected self-consistent pair without the external
receipt and successful apply-time fence is never an import candidate. The
legacy caller-copied retention map remains empty.

ConfigMaps and zero-retry, non-root, tokenless Jobs remain generation-keyed and
protected with `prevent_destroy` plus cluster admission. A separate fail-closed
ValidatingAdmissionPolicy, installed before Secret creation, admits only
immutable, generation-labeled, uniquely named Secrets containing exactly the
`assertion` key. UPDATE and DELETE are both denied.

## Parent integration dependencies

- SAI-10 independently owns `0030_customer_storage_credentials.sql`. SAI-16
  retains `0032_session_exchange_buckets.sql`, its `0033` exact-sliding
  successor, and additive `0034` mixed-version bridge; parent integration must
  retain all exact sibling histories and regenerate the combined immutable
  migration manifest. This branch does not copy or modify SAI-10.
- Before integration, security-owned release automation must perform the
  policy-first apply, pin every policy/binding and trust UID/digest/key-set,
  then run the apply-time verifier stage before any later import plan. This
  source candidate does not manufacture that external authority.
- SAI-01 independently owns request-capture exclusions and the 90-day debug
  expiry/purge contract. This isolated SAI-16 lineage still predates those
  changes, so it must not be integrated alone over accepted SAI-01 work. This
  ticket does not edit request-debug capture, inference, MCP, operations,
  storage, or retention behavior.

The coordinator's static-only boundary prohibited executing authored tests,
Terraform, builds, formatters, scanners, package managers, or live probes.
