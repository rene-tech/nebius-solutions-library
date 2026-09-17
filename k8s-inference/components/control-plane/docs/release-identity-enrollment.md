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

Bootstrap recovery is append-only, receipt-driven, and staged. Platform
Security first supplies three inputs outside customer deployment
configuration: a root-owned, non-group-writable short-lived kubeconfig/context
below `/run/fs2-security`, a separately root-custodied exact
admission-authority tuple plus pinned receipt-signing-key digest, and
(after object creation) a signed admission-bundle receipt. The first targeted
apply uses only `FS2_RELEASE_IDENTITY_KUBECONFIG`,
`FS2_RELEASE_IDENTITY_KUBE_CONTEXT`, and
`FS2_SECURITY_ADMISSION_AUTHORITY`; Helm and bootstrap deliberately remain
blocked while `FS2_SECURITY_ADMISSION_BUNDLE` is absent. That phase creates
the admission-policy lifecycle guard and the history, trust, receipt,
assertion, verification and schema-compatibility policies through the
separately custodied provider, never the general run provider. It also creates
a four-object stable epoch-router policy/lifecycle boundary. It rejects
protected names without an authority label, denies every protected update or
delete using request namespace/name matching, and admits CREATE only from a
bound-token automation release ServiceAccount. Public signed assertion
generations retain the v1 8–63-character lowercase/dot contract. Kubernetes
authority names use the separately derived
`epoch-<first-20-hex-of-sha256(public-generation)>`; every epoch gets six
literal `fs2-bootstrap-<purpose>-<epoch>` policy/binding pairs. The stable router
requires the caller username to be exactly
`system:serviceaccount:fs2-system:fs2-release-identity-<epoch>`, where the
suffix is read from the object authority label. Its lifecycle rule also
requires each reserved policy name to end in that same label. An older
still-valid bound token can therefore address only its own already occupied,
append-only names; it cannot preoccupy a future epoch or install a policy under
another epoch. The exact generation rules additionally bind the canonical
ServiceAccount UID and one bound short-lived-token credential ID, and their
object match conditions select only that same
`fs2.nebius.ai/authority-epoch` label. Assertion Secrets additionally retain
the unchanged public generation in `fs2.nebius.ai/assertion-generation`. An expired old
policy therefore cannot deny a later generation. The old
reusable `fs2-release-identity` username is refused. The public generation and
exact per-generation tuple remain root `dynamic_models.bootstrap_authority`;
no bearer token is a Terraform value. The fixed bootstrap authority is
supplied separately by Platform Security and is never accepted from that root
object. The lifecycle policy makes its own generation's policies and bindings
append-only.

Before that targeted apply, Platform Security provisions and operates the
fixed `fs2-security-release-admission` validating webhook outside this
Terraform state. The authority file pins its canonical manifest, UID, and full
provider-object digest. That external boundary validates the current approved
epoch and exact short-lived credential ID on every reserved CREATE, so the
repository's stable label router is not the rotatable authority and an older
still-valid credential cannot reserve a future epoch. Terraform refuses to
create, update, delete, or adopt the webhook.

If a targeted admission apply stops part way through, Platform Security must
re-read every existing reserved policy/binding UID and full provider object
and add exactly that complete set to the authority file's `adopted_objects`
map before a retry. Terraform refuses any pre-existing fixed or epoch name not
covered by that separately custodied map (or the final approved receipt); it
never silently server-side-adopts a self-consistent preoccupied name.

After the policy-first apply, Platform Security signs one
`fs2-serve.nebius.ai/security-admission-bundle/v1` receipt. It contains the
exact security-authority tuple, the external security webhook, and every fixed
and generation-scoped policy/binding canonical-manifest hash,
provider-observed UID, and full provider-object hash. The independently custodied authority file pins the
receipt signer key. A second plan supplied with
`FS2_SECURITY_ADMISSION_BUNDLE` re-reads the cluster and requires exact set,
identity, object and source-manifest equality before either Helm or bootstrap
can proceed. A preoccupied name fails the first create; substitution before the
second plan changes UID/object hash and fails closed. The fixed lifecycle guard
also protects the schema-compatibility guard used for stored-revision Helm
rollback.

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
older 12-object epoch sets while the new set is created. Platform Security must
sign a replacement admission-bundle receipt whose exact object set includes
the new epoch. Record the new UIDs, extend the trust binding with the complete
new set, and only then enable the bootstrap execution with its exact
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
single-`assertion`-key Secrets labeled with both the public assertion generation
and its digest-derived authority epoch. The policies must exist
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
current aligned-bucket admission into the exact ring at that call's timestamp
only when the transaction began at pre-existing schema `0032` or `0033` (or
when provenance is missing and therefore ambiguous). Because that legacy state
may already have overwritten a still-active prior bucket, the bridge denies
admission only until that prior bucket's latest possible aligned-boundary
expiry. The imported current budget then remains active for a complete window.
A fresh database or an observed `0029`/`0030`/`0031` database could not have
served the rejected limiter, so it opens the exact limiter immediately without
an invented outage. There is no empty-ring reset or claim that lost legacy
event timestamps were reconstructed exactly.
The migration explicitly removes inherited execute privileges from the
internal exact and bridge functions before granting only the legacy and exact
public wrappers.

Terraform always supplies Helm `migration.compatibilityImage` from the
independent root `applications.control_plane.schema_compatibility_image`.
The current chart refuses an upgrade when either compatibility repository or
digest is absent; only a fresh install may fall back to the application image.
Because Helm rollback renders an older stored chart, Terraform also installs
the `fs2-control-plane-schema-compatibility` ValidatingAdmissionPolicy and Deny
binding before the Helm release. That non-chart boundary survives revision
rollback and rejects the migration Job or gateway Deployment unless `migrate`
and `wait-schema` use the exact independently pinned compatibility image. A
direct rollback to a pre-guard chart therefore fails closed instead of using
the old application binary against the newer schema. During an application
rollback, retain that successor image and change only `control_plane_image`.
This preserves forward schema checks and the legacy wrapper.

Rollback is otherwise data-preserving: retain the release assertion
receipts, operator principals, credential verifier rows, sessions, audit rows,
trust material, and deprecated Terraform Secret. Select the prior reviewed
application image through the current compatibility-aware chart without
deleting or rewriting those records. This
source candidate contains no deployment or live-verification evidence.
