# Operator credential custody and release admission v3

> Superseded for source design by `OPERATOR_ACCESS_HYGIENE_V4.md`. This file is
> retained as history and is not integration or deployment evidence.

This document defines the source contract for remote Terraform state,
credential evidence, and operator/release identities. It is intentionally
fail-closed: the repository does not contain production credential values or
locally invented trust anchors.

## Admission status

The source implementation is not deployment-authorized. The checked source
trust allowlist is empty until an independently operated transparency log,
anchor key, at least two witness keys, endpoint, and prior checkpoint receive
independent acceptance. SAI-06 has static source approval only; its integration
and live acceptance remain pending. SAI-08 and SAI-09 do not yet have accepted
source successors in this lineage. Consumer rollout, rotation readiness,
ciphertext migration, and authentication-continuity admission therefore remain
blocked.

No deployment, state migration, credential change, revocation, or retirement
may be inferred from these source changes.

## Evidence trust

The client selects a trust-bundle ID, but it cannot introduce a trust bundle.
The selected bundle must already exist in the source-owned allowlist and binds:

- the HTTPS endpoint and transparency-log ID;
- producer and anchor public-key digests and anchor key ID;
- the exact root authority policy digest;
- a non-empty prior checkpoint tree size, root, and checkpoint digest;
- independent witness IDs, key IDs, and public-key digests; and
- a quorum of at least two witnesses.

Each observation must contain a canonical record leaf, an inclusion proof, a
consistency proof from the source-pinned prior checkpoint, an anchor signature,
and the witness quorum over the same checkpoint. A signed index or checkpoint
identifier without those proofs is not admission evidence.

## Identity separation

The read-only evidence identity is a viewer used only for provider inventory.
It is distinct from the release identity. The release identity is a non-human
service account with the exact project, credential, role set, audience
`terraform-release`, provider-enforced expiry of at most one hour, fixed Nebius
configuration and profile, and an allowlist limited to preflight, plan, and
apply. Interactive or human use is forbidden.

The wrapper rejects inherited `NEBIUS_PROFILE` and `NEBIUS_CONFIG` and has no
profile-selection option. It accepts release identity details only from the
externally anchored provider observation. The deployment target project must
equal that observation.

## Remote state

Normal planning and state inspection use only `terraform state pull` from the
configured S3 backend. Local legacy state is not a prerequisite or fallback.
Every backend configuration and its parent chain must be root-owned and not
group/world writable; the file must be mode 0600 and declare encryption,
locking, bucket, and object key.

Before initialization, provider-native custody evidence must bind the exact
backend-config digest and prove an HTTPS endpoint, KMS encryption, access
logging destination, and versioning. Backend migration is a separately
authorized operation and is not performed by the normal wrapper.

Legacy plaintext state remains quarantined only for independently approved key
migration. Rollback never restores it into an active run root. After all
historical generations and ciphertext have provider-observed migration proof,
an independently authorized retention workflow may move it into encrypted,
audited storage or retire it. This task performs neither action.

## Global inventory and class adapters

The registry enumerates every current infrastructure IAM Terraform address.
All other project IAM objects remain fail-closed unless they are one of the two
provider-attested authority identities or have an exact reviewed exemption.
Cluster Secrets are reconciled against Terraform state. The only source rules
for controller-owned Secrets cover Helm release records and bound service
account token objects in authority-approved namespaces; unknown objects fail
admission.

Each active credential class requires its own digest-pinned, root-configured
read-only adapter. The provider passes the exact class contract, canonical
state, global Secret commitments, IAM inventory, and evidence identity. For
consumer readiness, caller bindings must first match the provider-observed
namespace, name, UID, resourceVersion, decoded-content commitment, evidence ID,
class, and generation. A generic readiness response cannot authorize rollout.

## Rollback and no-delete boundary

Rollback is additive: retain old generations, restore the prior current-write
selector only after dual-read readiness, and preserve all append-only evidence.
Never restore exposed plaintext files, discard a historical generation, move a
protected Terraform address, revoke a predecessor, or delete a resource as a
rollback shortcut. Under the active no-delete directive, even approved
retirement steps remain blocked.

The source-only verification files added with this contract are not execution
evidence. Dynamic tests, Terraform, live provider reads, and deployment remain
unrun under the no-delete directive.
