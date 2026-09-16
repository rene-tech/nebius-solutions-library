# SAI-05: website catalog access

The public Scientific AI website must not share the academic customer's
invoke-capable PAT. The workloads stage now creates a distinct Secret and
idempotently provisions principal `terraform-scientific-ai-website` with this
exact policy:

- tenant: the configured academic catalog tenant;
- scopes: `catalog.read` only;
- models: `*`, so the public catalog follows authorized publication changes;
- maximum concurrency: `1`.

The Helm schema and template reject additional website scopes or a concurrency
value other than one. Public invoke routes continue to require
`inference.invoke`; the focused API regression proves that this catalog-only
identity can list models while `POST /v1/chat/completions` returns HTTP 403 and
creates no operation.

The website repository consumes only Secret `fs2-serve-website-access`. Its
deployment also selects an egress-deny NetworkPolicy that permits cluster DNS
and the exact HTTPS catalog gateway, plus a Cilium FQDN policy that permits
`api.resend.com:443`. No credential value is present in either repository.

## Staged rollout and verification

Deployment remains blocked until the SAI-09 release-lineage/provenance gate is
cleared. Once cleared, the integration owner must first reconcile the branch
with the currently deployed control-plane and website revisions and record the
current stable pre-deploy Helm revision and all affected image digests at the
assigned rollout slot. Historical control-plane revision 132 is known unsafe
because it enables blanket request capture and must never be selected as a
rollback target. The approved release and its rollback candidate must both set
`request_debug_enabled=false`.

1. At rollout time, capture the then-current stable Helm revision, values, and
   affected image digests. Fail closed if the candidate or rollback values set
   `request_debug_enabled` to anything other than `false`, or if the rollback
   target resolves to revision 132.
2. Plan the workloads stage. The expected security delta is one generated
   website PAT, one `fs2-system/fs2-serve-website-access` Secret, one bounded
   bootstrap Job, and its bootstrap NetworkPolicy. Do not accept unrelated
   deletes, replacements, or image changes.
3. Apply the reconciled control-plane release before changing the website.
   Confirm the bootstrap Job completed and the admin API reports the website
   principal with exactly `catalog.read`, models `*`, and concurrency `1`.
4. Without logging the token, issue authenticated `GET /v1/models` and
   `GET /v1/scientific-models` requests and require HTTP 200. Issue a bounded
   `POST /v1/chat/completions` and require HTTP 403 with no operation created.
5. Apply the website manifest. Keep the existing immutable website image unless
   a separately reviewed source change requires a replacement. Wait for `1/1`
   Ready, then verify `/api/health`, `/api/models`, `/mcp/`, lead delivery, and
   the existing `/v1`, `/mcp`, and `/admin` route precedence.
6. Prove denied egress from the website pod to an unrelated destination and
   positive DNS, catalog gateway, and Resend connectivity. Re-run the affected
   customer and operator smoke tests before declaring the release accepted.

## Rollback

Retain only the stable pre-deploy Helm revision and image digests captured at
the assigned rollout slot, after verifying that revision has
`request_debug_enabled=false` and contains the required sibling remediations.
Never roll back the shared control plane to revision 132. If egress policy
causes an availability regression, roll back only the website NetworkPolicy
objects first while retaining the catalog-only credential. If credential
bootstrap is the failure, roll back the control-plane release only to the
captured safe revision and only after restoring a working website catalog
source. Reusing `fs2-serve-scientific-access` or otherwise restoring the broad
PAT is forbidden. The academic Secret and principal are otherwise unchanged
throughout this rollout.
