# SAI-16 source handoff

This document describes a static source candidate. It is not integration,
deployment, live verification, or security acceptance evidence.

The exact parent `86de0ced25e498d0ca4d0c2f55d463d1cf982577` / tree
`601f5473a146ab875b5409f0188a3c5a47b551fd` is preserved as
SOURCE/INTEGRATION/LIVE NO-GO. Its fixed router bootstrap remained
preoccupiable and used only the general provider; its schema-compatibility
policy/binding had no cluster-enforced custody; and it narrowed the public
signed v1 assertion generation from the accepted 8–63 lowercase/dot contract
to a 32-character DNS label. The rejected parent
`dfb996a070a85381bc9a5619e21196fcdf010374` and every earlier rejected commit
also remain provenance. This successor addresses only those three findings
and is still an unreviewed static-source candidate.

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
which existed before its serialized transaction. A cutover fence is required
only when that value is `0032_session_exchange_buckets.sql` or
`0033_session_exchange_sliding_window.sql`; missing provenance remains
fail-closed. A fresh schema and observed `0029`, `0030`, or `0031` schema could
not have served the legacy limiter, so the exact limiter opens immediately.
For a real legacy cutover, the first post-commit call locks one shared bridge
and binds the limiter configuration. Legacy `0032` has
only one aligned bucket per slot, so a current bucket can have overwritten a
still-active prior tail. The bridge imports only authoritatively visible current
source/aggregate counts, timestamps them at the bridge call, and denies all
admission until the previous bucket's latest possible aligned-boundary expiry.
After that bounded fence, imported current counts continue consuming exact
capacity. Cutover cannot reset or under-count a lost tail and does not pretend
legacy buckets contain per-event timestamps. Old rolling replicas and new
replicas use the legacy and exact public wrappers over that same bridge. The
internal bridge and renamed exact-v2 function have inherited runtime/current-
role execute grants explicitly revoked.

The platform-root deployment contract requires, validates, regionally mirrors,
and forwards the independent immutable
`applications.control_plane.schema_compatibility_image` as workloads-stage
`control_plane_schema_compatibility_image`, which Terraform maps to Helm
`migration.compatibilityImage`. Rollback retains that successor
migration/schema-wait image while selecting the prior application image; it
never reverses schema or discards limiter state. The current chart independently
requires the compatibility repository and digest for every Helm upgrade; only
a fresh install retains the application-image fallback. Because rollback
renders the stored older chart, the separately custodied release provider owns the durable
`fs2-control-plane-schema-compatibility` ValidatingAdmissionPolicy and Deny
binding outside Helm and orders the release after it. The guard matches the
exact migration Job and gateway Deployment and admits only the Terraform-pinned
image in the `migrate` and `wait-schema` containers. A stored pre-fix chart can
therefore neither bypass the guard nor remove it.

The regenerated `0034` SHA-256 is
`2aabc8efc578a90e115af56e78a2dbb2864920abc9b0fd11f7ce024647b033a0`;
the ordered migration-set SHA-256 is
`219269587f16e05583d5b6c2261081a61d02ecc3ae39aedffb81316acf3dbb48`;
and the release-contract payload SHA-256 is
`223d07b4923abcf3f2647cf967fa95dbb4b4bad478ed634459c11fcaea8f9819`.
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

Recovery now requires a security-custodied policy-first apply. The general run
provider cannot write admission or protected bootstrap objects. A root-owned,
non-group-writable, non-symlink kubeconfig below `/run/fs2-security` and a
distinct context configure the `kubernetes.release_identity` provider, while a
second root-custodied file binds
the exact short-lived ServiceAccount username, UID, bound-token credential ID,
an independently selected receipt-signing-key digest, and the exact
manifest/UID/full-object identity of Platform Security's externally operated
`fs2-security-release-admission` webhook. Terraform does not create, mutate,
delete, or adopt that webhook. It performs the live rotatable current-epoch
and exact-JTI authorization which a retained static CEL router cannot. A four-object stable epoch-router
lifecycle/policy boundary first rejects unlabeled protected names, denies
protected mutation/deletion, and admits new epochs only from bound-token
automation release ServiceAccounts. Every assertion generation then gets six
literal `fs2-bootstrap-<purpose>-<epoch>`
lifecycle/history/trust/receipt/assertion/verification policy and binding
pairs. The public signed generation retains the v1 8–63 lowercase/dot wire
contract. A separate DNS-safe epoch is deterministically derived as
`epoch-<first-20-hex-of-sha256(public-generation)>`; no public assertion or
retained payload is rewritten. The router requires the
object epoch label, reserved policy-name suffix, and caller username suffix to
be identical, while each immutable exact Deny rule additionally binds the
canonical ServiceAccount UID and exact Kubernetes bound-token credential ID.
A prior still-valid credential can therefore address only its own already
occupied append-only names and cannot preoccupy or attach an arbitrary policy
to a future epoch. A retained old policy cannot match or deny a later
generation, and a reusable username alone is insufficient. Rotation retains
all prior authority tuples and appends the next epoch's policies.

A partial admission apply is recoverable without broad adoption: Platform
Security adds exactly the complete observed reserved-policy/binding
UID/full-object set to the authority file's `adopted_objects` map. An unlisted
pre-existing fixed or epoch name blocks before server-side apply; the final
receipt replaces that partial custody record only after the complete intended
object set exists.

Helm and model bootstrap remain blocked after that first phase. Platform
Security must issue one bounded `security-admission-bundle/v1` receipt whose
exact object set contains the four fixed router objects, the schema-
compatibility policy/binding, the external security webhook, and every
configured 12-object epoch set. The
receipt binds each canonical source-manifest digest, cluster UID, and full
provider-object digest; its signer key and exact admission authority must match
the separately custodied authority file. Terraform re-reads the provider
inventory and requires exact set equality before Helm or bootstrap. A
preoccupied name fails the first create, while any later object substitution
changes the second-phase UID/object digest and fails closed. Partial epoch pins
fail closed. The trust binding separately
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
immutable, uniquely named Secrets containing exactly the `assertion` key and
both the public generation and digest-derived authority-epoch labels. Its match uses the admission request namespace/name and an
operation-aware `oldObject` label on DELETE, so unrelated Secret deletions
cannot fail because the request object is absent. UPDATE and matched DELETE are
both denied.

## Parent integration dependencies

- SAI-10 independently owns `0030_customer_storage_credentials.sql`. SAI-16
  retains `0032_session_exchange_buckets.sql`, its `0033` exact-sliding
  successor, and additive `0034` mixed-version bridge; parent integration must
  retain all exact sibling histories and regenerate the combined immutable
  migration manifest. This branch does not copy or modify SAI-10.
- Before integration, security-owned release automation must use the separate
  provider/authority contract for the policy-first apply, sign the exact
  canonical-manifest/UID/full-object admission bundle, pin every trust
  UID/digest/key-set, and run the apply-time verifier stage before any later
  import plan. This source candidate does not manufacture that external
  authority or receipt.
- SAI-01 independently owns request-capture exclusions and the 90-day debug
  expiry/purge contract. This isolated SAI-16 lineage still predates those
  changes, so it must not be integrated alone over accepted SAI-01 work. This
  ticket does not edit request-debug capture, inference, MCP, operations,
  storage, or retention behavior.

The coordinator's static-only boundary prohibited executing authored tests,
Terraform, Helm, builds, formatters, scanners, package managers, or live
probes; none were executed for this successor. Source inspection used only
read-only `sed`/`rg` and Git identity commands. No command created, overwrote,
or removed a generated artifact.
