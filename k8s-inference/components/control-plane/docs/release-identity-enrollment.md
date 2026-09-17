# Release identity, operator enrollment, and recovery

This contract replaces the shared human bootstrap bearer. It does not create a
second interactive administrator. A security-owned release authority first
attests one provider `workload_identity_session`, then signs a narrowly scoped
Ed25519 compact JWS for one control-plane action. Signing keys never enter this
repository, Helm values, Terraform state, Kubernetes ConfigMaps, browsers, or
the control-plane process.

The public trust ConfigMap contains only the v1 trust document accepted by
`ReleaseIdentityVerifier`: issuer, exact workload subject, audience
`fs2-admin-release`, authorization-closure SHA-256, allowed capabilities, and
Ed25519 public keys. The approved workload session must be non-human,
non-interactive, impersonation-disabled, audience-bound, and no longer than one
hour. Each assertion is no longer than five minutes and is consumed once into
`fs2_release_identity_receipts` before any expensive credential work or
authorized side effect.

## Fresh install and recovery

The release authority signs exactly one `operator.enroll` capability and the
complete global-administrator target. Tenant-scoped targets and non-admin roles
are invalid. `POST /admin/api/v1/operator-enrollment:consume`
accepts no request body fields and therefore cannot select or impersonate a
principal. `create` requires both the signed UUID and subject to be absent.
`recover` requires the UUID, subject, display name, human kind, role, tenant,
enabled state, and existing row to match exactly. Recovery rotates the personal
credential and revokes every session for that principal; it cannot change the
principal.

The response discloses the new personal credential once to the approved secure
delivery step. A human uses that credential only at
`POST /admin/api/v1/session`. The server derives the principal from the
credential verifier. A caller-supplied principal identifier is ignored because
the session route has no such input.

The assertion receipt is committed before Argon2id hashing. Argon2 work is
admitted to a fixed-size executor without an unbounded waiter queue; saturation
returns 429 before another memory-hard allocation. If validation, hashing,
delivery, or the credential transaction fails, that assertion remains
spent and the release authority must attest and sign a new assertion. Replaying
the old assertion is rejected before memory-hard work begins.

## Scoped release automation

The retained `/admin/v1/*` routes accept only fresh, single-use assertions with
exactly one of `tokens.issue`, `tokens.list`, `tokens.revoke`, or `audit.read`.
The static Terraform rollback value is not mounted into the control-plane Pod,
is not read by the application, and is refused by the session and automation
routes.

An initial dynamic-model seed uses one `models.bootstrap` assertion bound to
the SHA-256 of the canonical `fs2-serve.nebius.ai/model-bootstrap/v1` payload.
The external release authority owns the short-lived Secret named by
`release_identity_model_bootstrap_assertion_secret_name` and supplies the
non-secret `release_identity_model_bootstrap_assertion_generation`, which is
also a signed assertion claim and a field in the digest-bound request; Terraform
references only those public identities and never reads assertion data into
state. The payload, implementation, runtime image, assertion generation and
Secret name produce a 32-hex Job generation key. A Job has zero automatic
retries because an ambiguous response must not replay a single-use assertion.
Existing model identities are preserved; new proposals still pass the ordinary
preview, qualification, persistence, and projection services.

Bootstrap recovery is append-only, receipt-driven, and staged. First apply the
admission-policy lifecycle guard and the history, trust, receipt, assertion,
and verification policies with model bootstrap disabled and root deployment
`dynamic_models.bootstrap_trust_binding.enabled=false` (forwarded internally as
`release_identity_model_bootstrap_trust_binding`). The first policy phase also
creates a four-object stable epoch-router policy/lifecycle boundary. It rejects
protected names without an authority label, denies every protected update or
delete using request namespace/name matching, and admits CREATE only from a
bound-token automation release ServiceAccount. Every assertion generation then
gets six uniquely named policy/binding pairs. Their CREATE rules are bound to
that generation's release ServiceAccount username, exact Kubernetes UID, and
one bound short-lived-token credential ID; their object match conditions select
only the same `fs2.nebius.ai/authority-epoch` (or assertion-generation) label.
An expired old policy therefore cannot deny a later generation. The old
reusable `fs2-release-identity` username is refused. These three public current
identifiers are root `dynamic_models.bootstrap_authority`; no bearer token is a
Terraform value. The lifecycle policy makes its own generation's policies and
bindings append-only. Record their provider-observed UIDs only after that
policy-first apply.

The same exact release credential may then create the immutable public trust
ConfigMap labeled with its authority epoch. Integration records its UID, full provider-object SHA-256, trust
document SHA-256, canonical issuer/key-set SHA-256, and every policy/binding
UID in root `dynamic_models.bootstrap_trust_binding`. Source preconditions
recompute and compare all values and require trust/history creation timestamps
to follow their admission bindings. A trust document selected only from the
same inventory is never authority.

Terraform's Kubernetes provider exhaustively discovers all generation-prefixed
ConfigMaps, Jobs, and retention receipts in `fs2-system`. Discovery is not
authority. The release authority signs a public durable compact JWS only after
observing the exact generation. It binds the identity digest, ConfigMap
UID/full-object digest, and—for terminal history—the Job UID, entire observed
Job-object digest, and consumed assertion receipt. Terraform does not execute
an ambient interpreter or repository script to accept that signature. It
creates a retained, zero-retry verification Job from root
`applications.control_plane.schema_compatibility_image` (forwarded as
`control_plane_schema_compatibility_image`), after admission and read-only RBAC
are active. That Job re-reads the exact API objects, verifies the pinned trust
root and Ed25519 receipt, and exits successfully only on an exact UID/spec/hash
match. Only a later plan may use the protected terminal Job as import
authority. A replaced object has a new UID and cannot reuse either receipt or
verification Job. The deprecated
`release_identity_model_bootstrap_retained_assertions` input must be empty.

To rotate or recover, retain every applied prior generation/authority tuple in
`dynamic_models.bootstrap_retained_authorities`, supply a new assertion
generation and current authority, and first perform a policy-only apply. The
old trust pin remains valid for the four shared router objects plus complete
older 12-object epoch sets while the new set is created. Record the new UIDs,
extend the trust binding with the complete new set, and only then enable the bootstrap execution with its exact
`fs2-release-model-bootstrap-<generation>` Secret. Partial epoch pins and
execution from an unpinned epoch fail closed. Terraform creates a new immutable
ConfigMap and zero-retry Job while `prevent_destroy` protects prior terminal
generations and policies. Never remove a retained authority or copy history
into tfvars. Missing, mismatched,
nonterminal, one-sided, unsigned, or UID-mismatched history fails closed. A
current ConfigMap-only partial apply requires its own authority-signed
ConfigMap-phase receipt before import and Job creation; it cannot self-attest.
Fail-closed ValidatingAdmissionPolicies deny ConfigMap/Job/trust/receipt and
assertion-Secret mutation or deletion, restrict all creation to the exact
generation-specific release credential, and admit only immutable
generation-labeled single-`assertion`-key Secrets. The policies must exist
before trust, receipt, history, assertion, or verification creation.
This is the supported
fresh-install and recovery path; a mutable Secret behind a fixed Job name is
not.

## Rollout and rollback boundary

Before a rollout, release coordination must provision the public trust
ConfigMap and, only when a model-bootstrap Job is required, the short-lived
payload-bound assertion Secret. An absent or invalid trust policy prevents the
control plane from starting; an absent or expired bootstrap assertion prevents
the seed Job from running. Neither condition falls back to the historical
shared secret.

Limiter rollout is also source-forward. Migration `0034` keeps both old and
new SQL entry points behind one bridge. Its first post-commit caller locks the
cutover state, validates the limiter configuration, and imports every visible
current aligned-bucket admission into the exact ring at that call's timestamp.
Because legacy state may already have overwritten a still-active prior bucket,
the bridge denies admission only until that prior bucket's latest possible
aligned-boundary expiry. The imported current budget then remains active for a
complete window. There is no empty-ring reset or claim that lost legacy event
timestamps were reconstructed exactly.
The migration explicitly removes inherited execute privileges from the
internal exact and bridge functions before granting only the legacy and exact
public wrappers.

Terraform always supplies Helm `migration.compatibilityImage` from the
independent root `applications.control_plane.schema_compatibility_image`.
The chart itself also refuses an upgrade or rollback when either compatibility
repository or digest is absent; only a fresh install may fall back to the
application image.
During application
rollback, retain that successor image for the migration Job and schema-wait
init container and change only `control_plane_image`. This preserves forward
schema checks and the legacy wrapper.

Rollback is otherwise data-preserving: retain the release assertion
receipts, operator principals, credential verifier rows, sessions, audit rows,
trust material, and deprecated Terraform Secret. Select the prior reviewed
application image through the current compatibility-aware chart without
deleting or rewriting those records. This
source candidate contains no deployment or live-verification evidence.
