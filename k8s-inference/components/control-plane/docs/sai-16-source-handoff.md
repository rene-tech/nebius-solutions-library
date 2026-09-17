# SAI-16 source handoff

This document describes a static source candidate. It is not integration,
deployment, live verification, or security acceptance evidence.

## Session-exchange admission

The production and deterministic in-memory stores implement the same admission
contract: UTC-aligned fixed windows, keyed source fingerprints, two-choice
65,536-slot source state, at most 16 aggregate shards, and a quota allocation
whose shard quotas sum to the configured aggregate ceiling. An exact source
limit takes precedence, then the aggregate shard limit, then the rare source
slot collision. A collision is source-throttled and never cached as an
aggregate rejection.

PostgreSQL owns the authoritative production clock. The in-memory test store
uses its injected timestamp as the deterministic clock. Bucket coordinates and
quota arithmetic share one Python implementation; migration `0032` encodes the
same closed arithmetic in SQL.

The first rejection transition writes one boolean/timestamp state change and
one audit event saying only `one_or_more`; it never claims an exact rejection
count. Once established, every DB-reached rejection is a read-only fast path:
no row lock, counter update, or new audit row. Per-process bounded caches avoid
even that read on ordinary repeats. The state tables are fixed-size, their
forensic transition fields are indexed, and the migration adds no delete path.

## Model-bootstrap recovery

`models.bootstrap` assertions carry both a signed `resource_sha256` and signed
`resource_generation`. The request repeats that generation inside the
digest-bound payload; the API requires both fields to match. The external
Secret name is exactly `fs2-release-model-bootstrap-<generation>`.

Each current or retained Terraform entry is a complete non-secret immutable
execution spec: canonical payload JSON, exact runner source, digest-pinned
image, assertion generation and Secret name. Its 32-hex resource key is
recomputed from that identity. ConfigMaps and zero-retry, non-root, tokenless
Jobs are generation-keyed and protected with `prevent_destroy`. A fail-closed
ValidatingAdmissionPolicy, installed before Secret creation, admits only
immutable, generation-labeled, uniquely named Secrets containing exactly the
`assertion` key.

## Parent integration dependencies

- SAI-10 independently owns `0030_customer_storage_credentials.sql`. SAI-16
  adds only `0032_session_exchange_buckets.sql`; parent integration must retain
  both exact sibling histories and regenerate the combined immutable migration
  manifest. This branch does not copy or modify SAI-10.
- SAI-01 independently owns request-capture exclusions and the 90-day debug
  expiry/purge contract. This isolated SAI-16 lineage still predates those
  changes, so it must not be integrated alone over accepted SAI-01 work. This
  ticket does not edit request-debug capture, inference, MCP, operations,
  storage, or retention behavior.

The coordinator's static-only boundary prohibited executing authored tests,
Terraform, builds, formatters, scanners, package managers, or live probes.
