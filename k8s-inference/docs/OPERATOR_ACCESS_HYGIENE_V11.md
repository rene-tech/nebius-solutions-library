# Operator access hygiene v11: executable gate and lineage contracts

This additive source contract refines v10 without authorizing deployment. It
preserves the encrypted, versioned, locked and access-logged remote backend,
owner-only local artifacts, purpose-specific identities, exact CIDR policy and
request-debug exclusions.

## Exact Terraform root identity

The wrapper maps each resolved Terraform directory to its authority name. The
top-level directory is `configuration`; it is never inferred from the basename
`k8s-inference`. Infrastructure, foundation and workloads use their registered
stage names. Unknown directories fail closed.

The configuration root has no credential resources, but it has the same native
apply gate as every credential-bearing root. Its plan is also inspected through
a dedicated zero-credential path:
credential-shaped resources, moved addresses, mode/address disagreement and
mutating data sources are rejected. Credential-bearing roots continue through
the full registry, state, Secret, consumer and append-only apply-gate checks.

Every plan and saved-plan reinspection receives the same initialization mode.
A `greenfield-empty` plan supplies `--greenfield-bootstrap`; an established
plan supplies the exact root-specific durable identity receipt, including the
infrastructure and foundation receipts rather than only workloads.

## Backend authorization closure

A short-lived workload session is not permission evidence. A separately
digest-pinned adapter must expand the provider-effective authorization closure
for the exact service account, session, project, buckets and object keys.
Direct, cross-project, impersonation and unscoped grants must all be empty.
The provider observation must be current; a closure older than five minutes or
dated in the future is rejected.

Authority, operator-read, operator-proxy and scoped-delivery purposes are
read-only and must have no put, delete or multipart action. Release automation
may read/write a state object and read/write/delete only its lock object. It
may not delete the state object, any object version, the bucket or its policy.
The wrapper rejects non-release evidence containing a write or delete grant.

## Secret-free IAM inventory

The authority does not execute or deserialize a generic AccessKey list. A
dedicated adapter must project the fixed metadata field set at the provider
boundary before serialization. Its response must report zero returned data
fields and zero observed secret fields, and the observation must be no more
than five minutes old. Unknown fields, duplicate identities,
raw public/private/key data and malformed metadata commitments fail closed.

## Greenfield lineage transition

The first greenfield apply remains additive and is bound to its saved plan,
saved-plan apply receipt and bootstrap evidence. Immediately after success, a
read-only provider adapter must attest the first remote object version, absent
lock, applied plan hash, Terraform lineage and serial, canonical state hash and
every managed credential identity. It must report that it performed no
mutation and that there are no unmatched plan credentials or unmanaged live
credentials.

The wrapper writes that observation once under a content-derived filename; it
never replaces or removes a receipt. If the process stops after Terraform
returns but before sealing the observation, the next invocation reconciles the
immutable saved-plan gate receipts with the provider-observed applied plan and
can create the same transition receipt. More than one matching transition is
ambiguous and fails closed.

The wrapper then stops at the stage boundary. Before any later Terraform
command, the root-owned authority policy must be published additively as
`remote-established`, with `lineage_origin=greenfield-bootstrap`, the exact
lineage, and every field from the transition receipt. The authority verifies
that the genesis state is exact and that later serials descend from it. It
never changes an established root back to greenfield or aliases this evidence
to legacy migration/adoption.

## Operation registry parity

The greenfield lineage transition is a release-automation-only read operation.
The provider's purpose map names it explicitly and refuses to start unless its
map covers exactly the complete read-only operation registry. The authority,
local client, external anchor and configuration schema carry the same operation
set. A source regression derives the provider dispatch set and requires exact
equality across all five registries, including the transition operation's exact
`release-automation` purpose.

## Saved-plan object pinning

Release automation opens the owner-only saved plan once with `O_NOFOLLOW`,
validates its full device/inode/size/time/hash identity, then copies the exact
approved bytes into a Linux memfd sealed against write, grow, shrink and later
seal changes. Receipt validation and plan inspection are repeated against the
sealed snapshot. Terraform receives only that snapshot as
`/proc/self/fd/<n>`; its hash and seals, plus the original source identity, are
rechecked after Terraform returns. Same-UID mutation of the source pathname or
open source file therefore cannot change the bytes Terraform reads.

The provisioner embedded in every additive gate generation does not select a
plan from `FS2_TERRAFORM_SAVED_PLAN`. It walks its kernel process ancestry,
requires the fixed Terraform executable and exact wrapper `apply` argv,
duplicates Terraform's actual inherited plan fd through `/proc/<pid>/fd`, and
requires the duplicate to carry the complete memfd seal set before checking
its receipt, plan JSON, state and live bindings. The native gate never reads
the environment plan pathname. A named direct apply, an unrelated
environment-selected plan, or an unsealed descriptor fails closed.
Infrastructure and workloads feature-activation markers explicitly depend on
the native gate, so their state cannot advance ahead of it.

Every root also rejects a current receipt hash already present in its permanent
gate history. Retaining every historical generation remains mandatory, while
each apply must add one new receipt generation whose creation provisioner runs
the native check. Reusing an old generation cannot suppress local-exec.

### Embedded reference-data ownership

The customer reference-data plane is part of the workloads Terraform state,
not a second state root. When `module.reference_data` is enabled, workloads is
the sole native gate owner: the module receives the exact receipt path, source
commit, receipt hash, complete gate history and migration phase from workloads,
and the entire module depends on the workloads gate token. The child retains
its stable, protected dependency token for its credential resources, but its
duplicate external/native gate and apply generation are disabled. It therefore
cannot demand a `reference-data` ancestor while Terraform is applying the
workloads saved plan, and it cannot advance before the actual workloads gate.

The reusable reference-data Terraform directory keeps a self-gated standalone
mode only for an invocation whose exact resolved directory is registered as
the separate `reference-data` root. Nested use never infers standalone
authority from `path.module`; its effective root is `path.root`. Parent-owned
mode requires `path.root != path.module`, while standalone mode requires exact
equality, so a standalone caller cannot disable the native gate by setting the
parent-mode input. The normal three-stage stack does not plan or apply a
separate reference-data state.

Preflight validation retains the original canonical path solely to match the
write-once receipt. Runtime validation compares the sealed snapshot's path
binding, byte size and SHA-256 with that receipt rather than incorrectly
requiring the memfd to reuse the source file's device and inode. Staged and
greenfield admissions reuse this computed execution identity; they do not
reopen a descriptor path without its required original-path binding.

## Greenfield durable identity handoff

The first greenfield apply is not complete when only its lineage transition is
sealed. Before policy promotion, the wrapper pulls the new remote state,
requires its lineage, serial and canonical state digest to equal the
provider-attested genesis, and automatically creates the root's write-once
durable-identity receipt using the fixed authority's live Secret observations.
If the process stopped after the apply, the next greenfield initialization
re-observes the immutable transition, initializes only that remote backend,
creates the missing receipt, and still stops for additive policy promotion.
No manual `capture-state` step is part of the supported lifecycle.

## Current authorization boundary

Checked production trust, configuration and adapters are not present, and
`deployment_authorized` remains false. SAI-06 has static SOURCE GO but is not
semantically integrated here; accepted SAI-08 and SAI-09 successors are also
absent. These facts are blockers, not defaults to bypass or placeholder values
to invent.

This revision was prepared with static source inspection only. No test,
formatter, Terraform, Helm, provider, cloud, cluster, database, registry,
credential, deployment, cleanup or deletion action was performed.
