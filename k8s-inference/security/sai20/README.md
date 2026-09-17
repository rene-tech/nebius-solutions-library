# SAI-20 authority roots

`authority-roots-v1.json` is the sole trust root for SAI-20 database-network
activation. Terraform variables, environment variables, authority packets and
filesystem paths cannot add or replace roots.

The initial registry is deliberately `ENROLLMENT_REQUIRED` with no keys. This
makes the source successor fail closed until Platform Security supplies two
distinct Ed25519 public roots in a later additive commit: one evidence
collector and one independent reviewer. Each root must name an accepted Git
commit/tree, a committed public-key source path/blob and an independent review
receipt whose full normalized content is in that provenance document. The
verifier resolves those Git objects, recomputes the receipt and derives the
key ID from the raw public key; a caller-selected key is never trusted.

Root enrollment is a separate review event. Never replace or remove an enrolled
root in place. Rotation adds a new generation and retains prior provenance
until every packet and saved plan using it has expired. Private keys, tokens,
kubeconfigs and provider credentials must never enter this directory.

The registry's current empty state is not an operational authority packet and
does not authorize a plan, apply, integration or deployment.

## Enrollment protocol

Enrollment is additive and requires two reviewed commits. The first commits a
non-secret root document containing the public key, role, exact principal and
group, plus a content-derived acceptance record for the reviewed source. A
later independently reviewed commit may append that document's immutable Git
commit/tree/path/blob identity to the registry. Collector and reviewer roots
must have different keys, principals and groups. A Terraform input, packet,
environment value or uncommitted filesystem document can never enroll a root.

The evidence collector must use its enrolled identity to preserve raw request
and response bytes, transport endpoint/CA/TLS identities, API audit/request
IDs and observation times. The signed bundle must include authenticated raw
Kubernetes lists, the collector's own SelfSubjectReview and
SelfSubjectRulesReview results, per-principal identity/rules/access reviews,
and the authoritative provider group-membership response. The verifier, not
the collector's summary, reconstructs every list receipt and permission or
impersonation digest from those bytes.

No enrollment or evidence collection was performed while authoring this
source-only successor.

## v5 external enrollment and bootstrap prerequisites

Independent review rejected `d5c19b3a8b3345acbec7b16bd5a2c00a455874d8`
because the provenance document could assert its own acceptance. The v5 gate
therefore does not treat that document, a reviewer string, a Terraform value
or the v5 packet as an enrollment authority. Every evidence root additionally
needs a detached Ed25519 receipt in `root-enrollment-receipts-v1.json`. The
receipt statement binds the root key, role, principal, group and immutable Git
provenance, and its signer must be a retained
`platform-security-enrollment-authority` from an `ACTIVE`
`enrollment-authorities-v1.json` snapshot in a strict-ancestor commit. The
verifier resolves that historical commit/tree/path/blob and validates the
signature. The committed authority and receipt registries are empty and
`ENROLLMENT_REQUIRED`, so this source cannot activate or self-bootstrap.

The successor also requires a source-exact, already active bootstrap
ValidatingAdmissionPolicy and `Deny` binding. Their complete specs are defined
in `contracts/sai20-bootstrap-guard-v5.json`; raw API lists are signed and the
identity-stage apply read re-observes them before any v4/v5 policy is created.
The guard protects its own names and every successor policy/binding name. This
repository intentionally does not create that prerequisite: an integration
owner must establish and independently accept it under an earlier trusted
platform admission boundary, then supply its immutable UID/resourceVersion
receipt. If no such prior boundary exists, activation stays blocked.

Provider group membership is not a plan-only assertion. The v5 bundle signs
the exact observer executable digest and credential-subject digest. The final
unknown-nonce apply gate executes that observer, validates a fresh raw
provider transcript against the pinned endpoint and CA, and requires its empty
membership response to equal the signed response before the database policy
can become authoritative. No provider executable, credential or response is
committed here.
