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
`release_identity_model_bootstrap_assertion_secret_name`; Terraform references
only that public object name and never reads assertion data into state. The Job
has zero automatic retries because an ambiguous response must not replay a
single-use assertion. Existing model identities are preserved; new proposals
still pass the ordinary preview, qualification, persistence, and projection
services.

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
