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
