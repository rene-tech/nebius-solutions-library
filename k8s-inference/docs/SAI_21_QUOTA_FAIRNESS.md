# SAI-21 quota and fairness remediation

Status: source candidate for independent review. This document does not claim
integration, deployment, or live acceptance.

## Admission contracts

Scientific artifact uploads now reserve bytes before an upload handle is
issued. `FS2_ARTIFACT_TENANT_QUOTA_BYTES` (Helm
`scientificArtifacts.tenantQuotaBytes`) bounds the sum of retained upload
intents for one tenant. `FS2_ARTIFACT_TENANT_QUOTA_OBJECTS` independently
bounds active intent count, so a zero-byte object still consumes one slot.
PostgreSQL serializes admission, replay, byte totals, object totals, and
insertion under one transaction-scoped tenant advisory lock; the in-memory
implementation uses its repository lock. A new reservation over either bound
returns `artifact_quota_exceeded` with HTTP 429.

Upload intents remain immutable. Their separate quota reservation has an
`active -> removing -> released` state and an append-only event ledger. An
unfinished intent becomes removal-eligible after
`FS2_ARTIFACT_UPLOAD_RESERVATION_TTL_SECONDS`; finalization extends eligibility
through the artifact retention deadline. Every issued write capability is
appended before the handle is returned, and cleanup is fenced through the
latest such expiry plus `FS2_ARTIFACT_UPLOAD_COMPLETION_GRACE_SECONDS`.
Capabilities authorize only an exact multipart generation, part number, byte
length, and SHA-256. The client cannot complete the multipart object; only the
server can publish it, under a renewable database ambiguity lease that cleanup
must exclude even after its worker deadline. An autonomous finalizer takes over
an expired generation and reconciles provider state before it can renew or
complete the lease; elapsed time is never deletion authority. Publication pins
the provider `VersionId` in the upload,
session, lease, and artifact within one narrowly checked `SECURITY DEFINER`
transaction. The runtime role cannot insert an artifact, update either
provider-version column, or complete a lease directly.
Neither elapsed time, attempt close,
logical purge, nor a remover's successful delete releases quota. The remover
has a distinct delete-capable object-store identity and can only claim deletion
work. A separately scheduled verifier has a read-only object-store identity,
waits `FS2_ARTIFACT_PROVIDER_STABILITY_GRACE_SECONDS`, takes a constant-size
exact-key version/session absence probe, performs an exact-key HEAD, and takes
the same probes again.
Only identical before/after generation snapshots can be bound to the current
removal and verification generations. The verifier then uses a
verifier-only database routine to append provider request evidence and release
the byte and object reservations. Runtime and remover roles cannot call that
routine or insert evidence. The reservation, evidence, and ledger rows remain
retained after release.

Removal and verification claims use bounded exponential retry timestamps and
persisted per-tenant round-robin cursors. Claim transactions lock cursor and
reservation rows with `FOR UPDATE SKIP LOCKED`; a held tenant row is skipped
rather than blocking the pass, and concurrent workers cannot duplicate the
same generation. Fair first rounds repeat until the requested bounded batch is
full, so one tenant can use remaining capacity without jumping ahead of other
eligible tenants. Provider cleanup mutates only a fixed-size page per key;
unfinished pages retain quota and resume under a later generation. Four
targets advance independently, so one poisoned or high-cardinality key cannot
hold later tenants behind serial provider pagination. Zero-byte intents
follow the identical path and remain charged as one object until independent
absence is recorded.

Scientific GPU submissions reserve this conservative upper bound at durable
admission for every GPU stage:

```text
accelerators per Pod × expanded Pods × maximum attempts × execution deadline
```

The service-class execution bound is authoritative when present; otherwise the
reviewed stage active deadline is required. A GPU stage without a bound is
rejected. Admission increments `gpu_seconds_reserved`, not
`gpu_seconds_used`, and idempotent replay does not reserve twice. The terminal
batch projection takes the same token lock as admission and calls one
`SECURITY DEFINER` database routine. That routine derives the stored batch and
attempt evidence, writes one immutable settlement, mutates token counters, and
projects the terminal operation atomically. Direct settlement inserts and
direct scientific terminal/reservation transitions are revoked or
trigger-rejected; terminal scientific operations cannot be resurrected.

Generic token rotation/revocation, deadline, payload-expiry, and stale-reaper
paths only assert an idempotent `cancel_requested` handoff. They leave the
reservation and public operation nonterminal. The scientific controller claims
cancellation ahead of ordinary dispatch, resolves the deterministic external
workload when an apply may have occurred before its UID was persisted, obtains
UID-fenced deletion/absence evidence, persists `resource_released`, and only
then terminalizes through the settlement routine. A persisted GPU attempt with
missing scheduling admission, zero accelerator count, missing completion, or
missing release evidence is not zero-use proof: it receives the bounded
fail-safe full reservation charge. A batch with no GPU attempt at all may settle
at zero. Complete lifecycle evidence charges conservative observed Kueue GPU
occupancy and releases the unused reservation; the charge never exceeds the
admitted bound.

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
retained-object policy, and reservation TTL no shorter than that deployment's
configured 30-to-900-second handle ceiling or longer than retention. The
configured handle TTL is now both the default and the maximum this image can
issue, so previously valid short-handle/short-reservation configurations remain
valid. Migration 0031 nevertheless installs a fresh conservative 900-second
fence for every retained reservation before adding completion and stability
grace, covering a maximum-length handle that an older image may have issued.
Completion grace does not increase the configuration's minimum reservation
TTL; the completion and provider-stability intervals are separately bounded.

Migration 0031 does not treat nullable legacy `provider_version_id` rows as
safe to serve. It captures every pre-0031 artifact in a verifier-only claim
ledger and immediately walks the complete exact-key version inventory in bounded
provider pages with persisted cursors and append-only page receipts, re-fetches
an immutable provider version that matches the retained digest, size, and type,
and appends an immutable binding. Version binding is safe before the old-handle
fence because a pinned provider VersionId is immutable; only destructive
cleanup waits through capability, completion, and stability fences. An
exhausted inventory without a match is marked `unresolved`, stays fail-closed,
and remains visible in the retained scan ledger. Artifact reads can perform the
same exact bounded binding on demand while the background verifier drains the
ledger, so unrelated inference and artifact operations stay available during
expansion. Contract readiness nevertheless requires zero pending, unresolved,
unbound, or unfinished-without-session legacy rows. These retained
binding, session, and lease records intentionally have no restrictive FK to
the 90-day application metadata, so ordinary retention purge remains possible.
The bucket's noncurrent-version lifecycle now follows the configured
application retention window instead of one day, preventing a pinned legacy
VersionId from expiring before its metadata; application cleanup still removes
every exact-key version after verified retention expiry.

The static sibling inspection used exact local commits `68071e5e` (SAI-17),
`329dc4cb` (SAI-18), `2e11eaf9` (SAI-19), `ffd86740` (SAI-20), `505228e0`
(SAI-22), and `d7f0979e` (SAI-28). They are not integrated here. SAI-19 also
introduces `0030_scientific_artifact_object_versions.sql`; this branch's
`0030_scientific_quota_settlement.sql` therefore has an ordinal collision that
an integration branch must resolve additively and then use to regenerate the
single ordered PostgreSQL release contract. Neither migration may be dropped
or rewritten to make that integration pass. This correction deliberately adds
`0031_scientific_quota_fencing.sql` and leaves `0030` byte-for-byte unchanged.
Integration must also prove this branch's exact `0030` digest was never
applied, or supply a reviewed forward migration from the exact applied digest;
the migrator is correct to reject a changed migration with an already-recorded
version.

Rollback after 0031 is forward-compatible application rollback, never a return
to the pre-0031 image. That older image embeds only migrations through 0030 and
correctly fails the immutable-prefix check once 0031 is recorded. The chart
therefore requires three distinct immutable identities for an upgrade: the
currently serving predecessor, a separately built and reviewed 0031-aware
bridge/rollback image, and the feature candidate. Both new images carry the
same exact 31-migration contract; their prepare Jobs run without a database
credential and bind each image digest to its own immutable receipt before any
schema change.

The executable sequence is `prepare -> expand -> contract -> activate`:

1. `prepare` leaves the predecessor serving and preflights both 0031-aware
   images. It performs no DDL.
2. `expand` uses the bridge image to apply 0031 and then serve. Automatic Helm
   rollback is disabled for this one phase because its prior revision is the
   incompatible predecessor. The migration temporarily preserves only the
   predecessor's exact artifact-publication grants, and a phase-aware trigger
   records every provider-version-less predecessor publication for background
   binding. Live-table triggers commit before their idempotent catch-up scans,
   so those scans do not retain a DDL lock while still covering predecessor
   writes on both sides of trigger installation.
   A post-upgrade `artifact-bridge-ready` Job then reads only the exact release
   Deployment and its gateway Pods. Its append-only receipt binds the API
   server time and audit IDs, Deployment UID/generation and replica counts,
   and a canonical digest of the Ready bridge Pod set. The writer cannot mark
   readiness until the predecessor is absent and all four legacy backlog
   counts are zero. Its Kubernetes role can `get` only the resource-name-bound
   Deployment and can only `list` Pods; its network policy denies ingress and
   permits only DNS, PostgreSQL, verifier object storage, and configured
   API-server CIDRs.
3. `contract` removes those temporary grants, advances the database phase
   one-way, and rolls to the candidate. Its contract-only database preflight
   requires the complete exact migration ledger, complete 0031 step ledger,
   registered bridge identity, and immutable zero-backlog receipt before any
   DDL path is reachable; it can never be the command that first applies or
   finishes 0031. Helm may now roll back safely because the prior revision is
   the 0031-aware bridge.
4. `activate` alone enables new multipart begin/part/inline writes. A
   `rollback` release selects the prepared bridge and keeps those writes off;
   VersionId-pinned reads, leased finalization, verifier backfill, cleanup,
   inference, accounting, debugging, and 90-day retention continue. Its
   distinct `migrate-rollback` command first requires the complete exact 0031
   migration and step ledgers and the registered bridge/predecessor identity.
   It appends the retry revision without applying schema or changing a
   contracted database back to expanded. It retains predecessor publication
   compatibility only while the durable phase is still expanded; it never
   restores those grants after contraction.

Expand and rollback require the artifact verifier and maintenance controllers;
the chart rejects either phase without them. Prepare, expand, contract, and
rollback force only *new multipart-v2 admission* off while preserving legacy
single-put, inline/trusted publication, and completion of already-admitted
sessions. Retry revisions are append-only bridge attempts. A valid receipt for
an earlier attempt is never invalidated; a retry may append another receipt for
the same exact bridge/predecessor identities. Contract is idempotent after its
one-way phase change when that immutable identity and receipt still match.
Terraform gives expand/rollback the configured bridge-readiness deadline plus
a 30-minute rollout margin instead of the ordinary 30-minute Helm timeout.
Fresh installs are separately identifiable by the absence of a predecessor
registration; after their direct contracted migration, cleanup relies on each
reservation's own capability/completion/stability fences and does not wait for
an inapplicable predecessor-drain receipt.

The finalization recovery CronJob uses its own tokenless
`artifact-finalizer` ServiceAccount, distinct from runtime, maintenance,
remover, verifier, and migration identities. It has no RoleBinding; its network
policy denies ingress and permits only DNS, PostgreSQL, and configured artifact
store egress. It also mounts dedicated `fs2-serve-database-artifact-finalizer`
and `fs2-serve-artifact-finalizer-store` Secrets. The PostgreSQL login can only
claim an expired lease generation and settle that current, unexpired recovery
generation; it cannot read runtime tokens/operations, acquire a foreground
lease, call the unwrapped settlement functions, or globally select upload,
reservation, or lease rows. A claim-bound definer projection returns only the
exact intent for the current unexpired recovery generation. Its provider identity is a
separate MysteryBox-delivered, canonical-prefix-only key needed for multipart
completion and exact VersionId verification. The public runtime retains only
unexpired generation-1 wrappers and cannot claim recovery work. The verifier
retains the separately scoped Kubernetes read role needed for bridge readiness.

Every `SECURITY DEFINER` routine introduced by resumable migration 0031 is
revoked from `PUBLIC` before the transaction boundary that makes that routine
visible. The final privilege-reset step repeats the exact revokes as defense in
depth; a paused or failed later migration step therefore cannot expose definer
authority to another database login.

The target and rollback image digests must differ, and neither may equal the
predecessor. A build/release lane must publish and independently accept the
bridge before integration; this source-only task does not manufacture a digest
or claim that gate passed. Do not remove artifact intents, reservation state,
ledger events, settlements, LocalQueues, or usage accounting during rollback.

## Upload protocol compatibility

The committed begin-upload response still always contains a write handle.
Omitting new request fields selects `single-put-v1`, preserves the legacy
idempotency bytes, opens one server-owned multipart part, and returns its exact
length/checksum-bound handle. Because an S3 multipart part is limited to 5 GiB,
larger uploads opt into `multipart-v2` and provide the first part SHA-256;
begin returns the first handle, and the existing tenant-authorized part route
issues each remaining exact part. Only the server can complete the provider
session. The multipart-v2 wire contract fixes `part_size_bytes` at exactly
`134217728` (128 MiB), so `first_part_sha256` is the lowercase SHA-256 of bytes
`[0:min(size_bytes,134217728))`; this rule is present in the HTTP schema and MCP
parameter documentation before begin admission. HTTP, MCP, and internal workload
shapes expose the same explicit protocol version.

Migration 0031 retains every unfinished pre-0031 ordinary-PUT intent as
`legacy-single-put-v1` state. Idempotent replay may reissue only an exact
digest/length/type-bound legacy handle and durably advances the cleanup fence
before returning it; replay failure never cancels the retained operation.
Finalize selects and re-hashes an exact matching immutable provider VersionId;
zero or ambiguous matches stay fail-closed and charged. A predecessor that finishes
an upload during the expand window is admitted only by the phase-aware legacy
transition and is queued for the same authoritative version-binding workflow.
The upload-session and artifact-claim triggers are followed by committed,
idempotent catch-up passes, closing the predecessor commit window between the
initial snapshot and trigger installation.

Expired finalization ownership is never cleanup authority. The runtime-owned
`artifact-finalization` CronJob renews expired leases and either completes the
still-valid server-owned multipart session or recovers its immutable provider
VersionId before atomically publishing the artifact. Remover and verifier jobs
exclude every active finalization lease, including an expired one. Provider
session creation is write-ahead fenced too: only the exact durable claim owner
may bind the external upload ID, and a crashed owner requires bounded aborts
plus two empty provider observations separated by the configured stability
interval before the upload may be retried.

The maintenance deadline is derived from the maximum object bytes and a
declared per-worker verification throughput floor, with 300 seconds of
control-plane overhead. The rendered chart rejects a shorter deadline and
binds the declared concurrency to the implementation's bounded concurrency of
four. Defaults allow 18,000 seconds for a 1 TiB object at 64 MiB/s per worker;
the three maintenance jobs retain `Forbid` concurrency so retries cannot stack.

## Verification status

Exact candidate `3b35578e42e18264f68216eec6a430bb6959030e` / tree
`04771da96571051c68f302283833dbfe4d414199` is preserved as rejected: it had no
reservation expiry, no object count, charged the retry-complete estimate as
usage at admission, and left CPU stages on shared queues. This additive
successor also preserves rejected direct child
`cc4e02a02222b3aa9cdc838aa23d8fc3cd8f6134` / tree
`8cab63505e178cb0e5112008a3667b6f944c2c8b`, which released quota without
provider-confirmed removal and allowed generic terminalizers to bypass the
scientific settlement path. Exact commit
`9e3931757b0eeba1b2795b6c4d0bfc3d5ef28aab` / tree
`1e4601fe4ad42bbd6aaee40a3d5283dccf1dec4a` is also preserved as rejected: it
could release after one immediate list/HEAD despite an outstanding or in-flight
signed PUT, and both janitors could block on a tenant advisory lock after
selecting duplicate limited prefixes. The successor authors regression
coverage for latest-capability fencing, late-PUT re-removal, stable double
version snapshots, nonblocking generation claims, provider-confirmed release,
retained ledger state, zero-byte object ceilings, tenant-fair backoff, disjoint
remover/verifier identities, locked PostgreSQL reservation races, interruption
handoff, missing-evidence fail-safe settlement, terminal immutability,
restricted maintenance, reserve/settle GPU accounting, and exact CPU/GPU
tenant routing.

The coordinator prohibited execution of tests, builds, linters/formatters,
package managers, Terraform, Helm, containers, scanners, and live probes. None
were run. Before the final reminder, one local read-only Python contract-builder
invocation ran with bytecode writes disabled and printed only the PostgreSQL
contract hashes; it created, overwrote, and removed no artifact. A later
read-only search accidentally asked Bash to execute literals `15` and `0015`;
both failed as unknown commands and changed nothing. During the final correction,
another read-only search similarly attempted literal `0030`; it also failed as
unknown and changed nothing. No auth, cluster, database,
registry, credential, provider, or service was inspected or mutated. Therefore
this is a source candidate only: it is not an integration, deployment,
live-verification, or acceptance claim.
