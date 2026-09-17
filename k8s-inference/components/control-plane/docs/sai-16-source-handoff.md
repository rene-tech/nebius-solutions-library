# SAI-16 source handoff

This document describes a static source candidate. It is not integration,
deployment, live verification, or security acceptance evidence.

The exact parent `0f875be76e3f2497b46348d0ecbbb0b4ea680ce6` / tree
`f3d2d060ca17adf924cd59ceafa2c72aebfbea0a` is preserved as SOURCE NO-GO:
its aligned fixed windows, partitioned aggregate quotas, and caller-copied
bootstrap retention are negative evidence corrected additively here.

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
The workloads stage inventories all generation-prefixed ConfigMaps and Jobs
through the Kubernetes provider, requires one-to-one terminal history, verifies
the key as the digest of that complete identity, compares the live Job to the
stored contract, and declaratively imports discovered objects to recover lost
Terraform state. The legacy caller-copied retention map is refused unless
empty. A matching current ConfigMap with a missing Job is the sole partial-
apply exception, allowing Terraform to create that exact missing Job.

ConfigMaps and zero-retry, non-root, tokenless Jobs remain generation-keyed and
protected with `prevent_destroy`. A fail-closed ValidatingAdmissionPolicy,
installed before Secret creation, admits only immutable, generation-labeled,
uniquely named Secrets containing exactly the `assertion` key.

## Parent integration dependencies

- SAI-10 independently owns `0030_customer_storage_credentials.sql`. SAI-16
  retains `0032_session_exchange_buckets.sql` and adds only its `0033` exact-
  sliding successor; parent integration must retain both exact sibling
  histories and regenerate the combined immutable migration manifest. This
  branch does not copy or modify SAI-10.
- SAI-01 independently owns request-capture exclusions and the 90-day debug
  expiry/purge contract. This isolated SAI-16 lineage still predates those
  changes, so it must not be integrated alone over accepted SAI-01 work. This
  ticket does not edit request-debug capture, inference, MCP, operations,
  storage, or retention behavior.

The coordinator's static-only boundary prohibited executing authored tests,
Terraform, builds, formatters, scanners, package managers, or live probes.
