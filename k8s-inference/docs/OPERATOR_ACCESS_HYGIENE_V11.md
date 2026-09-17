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

The configuration root has no credential resources or native credential gate.
Its plan is nevertheless inspected through a dedicated zero-credential path:
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

## Current authorization boundary

Checked production trust, configuration and adapters are not present, and
`deployment_authorized` remains false. SAI-06 has static SOURCE GO but is not
semantically integrated here; accepted SAI-08 and SAI-09 successors are also
absent. These facts are blockers, not defaults to bypass or placeholder values
to invent.

This revision was prepared with static source inspection only. No test,
formatter, Terraform, Helm, provider, cloud, cluster, database, registry,
credential, deployment, cleanup or deletion action was performed.
