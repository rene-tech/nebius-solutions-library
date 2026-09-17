# SAI-21 quota and fairness remediation

Status: source candidate for independent review. This document does not claim
integration, deployment, or live acceptance.

## Admission contracts

Scientific artifact uploads now reserve bytes before an upload handle is
issued. `FS2_ARTIFACT_TENANT_QUOTA_BYTES` (Helm
`scientificArtifacts.tenantQuotaBytes`) bounds the sum of retained upload
intents for one tenant. `FS2_ARTIFACT_TENANT_QUOTA_OBJECTS` independently
bounds active intent count, so a zero-byte object still consumes one slot.
PostgreSQL serializes expiry, replay/reactivation, byte totals, object totals,
and insertion under one transaction-scoped tenant advisory lock; the in-memory
implementation uses its repository lock. A new reservation over either bound
returns `artifact_quota_exceeded` with HTTP 429.

Upload intents remain immutable. Their separate quota reservation has an
active/released state and an append-only event ledger. An unfinished intent
expires after `FS2_ARTIFACT_UPLOAD_RESERVATION_TTL_SECONDS`; closing an attempt
also releases each unfinished reservation. Finalization extends the reservation
through the artifact retention deadline; expiry releases it even when a
standalone input never produces the run result used by the purge selector.
Successful earlier retention purge also releases it. Released records and
ledger events are retained. An exact replay of an expired intent may reactivate
it only after the same locked byte/object admission check.

Scientific GPU submissions reserve this conservative upper bound at durable
admission for every GPU stage:

```text
accelerators per Pod × expanded Pods × maximum attempts × execution deadline
```

The service-class execution bound is authoritative when present; otherwise the
reviewed stage active deadline is required. A GPU stage without a bound is
rejected. Admission increments `gpu_seconds_reserved`, not
`gpu_seconds_used`, and idempotent replay does not reserve twice. The terminal
batch projection takes the same token lock as admission, writes one immutable
settlement, removes the full reservation, and charges conservative observed
Kueue GPU occupancy. Cancellation before GPU admission, CPU-only failure, and
materialization failure before GPU admission charge zero. Incomplete or
inconsistent GPU lifecycle evidence retains only the bounded admitted maximum;
the charge can never exceed the reservation.

Every scientific stage, CPU or GPU, resolves through a LocalQueue whose
`tenant_ids` is exactly the requesting tenant. GPU routing retains its model and
service-class specificity. CPU routing starts from the frozen CPU class and
selects exactly one tenant-only queue in the same namespace and ClusterQueue;
an unrestricted, absent, or ambiguous queue fails closed. Terraform derives
ordinary-namespace CPU queues for the deployment target, the academic tenant,
and every identity in the explicit `scientific_batch.enabled_tenant_ids`
inventory, while licensed academic classes remain bound only to the academic
tenant.

## Staged rollout and rollback

Before integration, inventory every enabled tenant from the authoritative
principal/configuration source and prove the rendered contract has exactly one
namespace-appropriate queue for every CPU class it may use, plus each scoped
GPU lane. The current static source provisions its explicit enabled-tenant
inventory; that inventory still must be reconciled against the principal
authority before integration. Reconcile overlapping SAI-17, SAI-18, SAI-19,
SAI-20, SAI-22, and SAI-28 successors before choosing an integration parent.
Set byte quota no larger than bucket capacity, object quota to the
retained-object policy, and
reservation TTL no shorter than a handle or longer than retention.

The static sibling inspection used exact local commits `68071e5e` (SAI-17),
`329dc4cb` (SAI-18), `2e11eaf9` (SAI-19), `ffd86740` (SAI-20), `505228e0`
(SAI-22), and `d7f0979e` (SAI-28). They are not integrated here. SAI-19 also
introduces `0030_scientific_artifact_object_versions.sql`; this branch's
`0030_scientific_quota_settlement.sql` therefore has an ordinal collision that
an integration branch must resolve additively and then use to regenerate the
single ordered PostgreSQL release contract. Neither migration may be dropped
or rewritten to make that integration pass.

Rollback is a source/image rollback to the prior reviewed control-plane and
scheduling contract. Do not remove artifact intents, reservation state, ledger
events, settlements, LocalQueues, or usage accounting. Migration 0030 is
forward-only; the prior application can ignore the new retained tables.

## Verification status

Exact candidate `3b35578e42e18264f68216eec6a430bb6959030e` / tree
`04771da96571051c68f302283833dbfe4d414199` is preserved as rejected: it had no
reservation expiry, no object count, charged the retry-complete estimate as
usage at admission, and left CPU stages on shared queues. This additive
successor authors regression coverage for expiry/release and retained ledger
state, zero-byte object ceilings, locked PostgreSQL reservation races,
reserve/settle GPU accounting, and exact CPU/GPU tenant routing.

The coordinator prohibited execution of tests, builds, linters/formatters,
package managers, Terraform, Helm, containers, scanners, and live probes. No
auth, cluster, database, registry, credential, provider, or service was
inspected or mutated. Therefore this is a source candidate only: it is not an
integration, deployment, live-verification, or acceptance claim.
