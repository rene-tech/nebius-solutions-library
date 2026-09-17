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

Bootstrap recovery is append-only and inventory-driven. Before each plan,
Terraform's Kubernetes provider discovers all generation-prefixed ConfigMaps
and Jobs in `fs2-system`. The immutable ConfigMap contains the payload, exact
runner, digest-pinned image, assertion identity, public endpoint, and complete
Job execution/security contract. Terraform verifies that the generation key is
the digest of that identity, compares the retained Job to it, and declaratively
imports both resources if state was lost. The deprecated
`release_identity_model_bootstrap_retained_assertions` input must be empty.

To rotate or recover, supply a new assertion generation and its exact
`fs2-release-model-bootstrap-<generation>` Secret; never copy history into
tfvars. Terraform creates a new immutable ConfigMap and zero-retry Job while
`prevent_destroy` protects prior terminal generations. Missing, mismatched,
nonterminal, or one-sided historical inventory fails closed; the one allowed
partial-apply recovery is the current digest-matching ConfigMap whose Job has
not yet been created. A fail-closed ValidatingAdmissionPolicy must be installed before
the release authority creates the Secret; it admits only immutable,
generation-labeled, single-`assertion`-key Secrets. This is the supported
fresh-install and recovery path; a mutable Secret behind a fixed Job name is
not.

## Rollout and rollback boundary

Before a rollout, release coordination must provision the public trust
ConfigMap and, only when a model-bootstrap Job is required, the short-lived
payload-bound assertion Secret. An absent or invalid trust policy prevents the
control plane from starting; an absent or expired bootstrap assertion prevents
the seed Job from running. Neither condition falls back to the historical
shared secret.

Rollback is source-forward and data-preserving: retain the release assertion
receipts, operator principals, credential verifier rows, sessions, audit rows,
trust material, and deprecated Terraform Secret. Select the prior reviewed
application/chart revision without deleting or rewriting those records. This
source candidate contains no deployment or live-verification evidence.
