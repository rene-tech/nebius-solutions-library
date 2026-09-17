# Authoritative feature-gated credential presence v8

This additive source-only successor preserves v7 and every predecessor. It does
not authorize integration or deployment. Production trust, provider adapters,
accepted SAI-08/SAI-09 integration, dynamic tests, and live evidence remain
absent; SAI-06 has static SOURCE GO only.

## Three presence categories

The durable registry separates all credential classes into exactly one of:

- **required** — unconditional platform credentials that must always be present;
- **feature-gated** — credentials required only by an enabled product feature;
- **optional** — dependency-owned PostgreSQL backup credentials whose complete
  source set may be absent until that dependency is integrated.

The seven feature-gated classes are the scientific and website academic PATs,
the reference-data cloud key and delivery Secret, the scientific-artifact cloud
key and delivery Secret, and NVIDIA registry credentials. PostgreSQL backup
classes retain their already-reviewed optional all-or-none policy. Every other
class remains unconditional.

## Authoritative state markers

Feature absence is never inferred from a missing Secret, access key, or copied
state. The infrastructure and workloads Terraform roots each declare one exact,
non-secret `terraform_data.credential_feature_activation` address. Its state
contains the v1 activation schema and an exhaustive class/group boolean map
derived directly from the same Terraform variables and locals that control the
credential resources.

The authority accepts the marker only from canonical remote state and the
root-owned, source-hashed Terraform configuration. Both registered marker
addresses must exist exactly once, their class/group keys must exactly equal the
registry policy, and every value must be a boolean. A missing, duplicated,
malformed, reduced, or expanded marker fails closed; it never means disabled.

For each enabled group, every fixed-generation address listed by the registry
must exist. Later retained generations may add their versioned addresses. A
partially present enabled group fails. For a disabled group, none of its managed
fixed or versioned addresses may exist. NVIDIA registry credentials are split
into four independent groups so enabling one consumer does not manufacture a
requirement for the other three.

Signed inventory and readiness evidence carry required, feature-gated,
feature-disabled, optional-disabled, and enabled class sets separately. No
adapter, source binding, generation, or readiness claim is emitted for a class
whose marker proves it disabled.

## Migration and rollback boundary

The new markers are additive Terraform resources with `prevent_destroy`. They
have not been planned or applied. A future accepted integration must stage and
attest their exact canonical-state instances before this source can authorize a
credential inventory. Existing credentials, state, historical generations, and
v7 receipts remain preserved throughout that prestage.

Rollback is source-forward and evidence-preserving: select a later reviewed
source generation while retaining both markers, all prior registry versions,
all credential generations, and all customer data. Never delete, replace,
state-forget, revoke, or overwrite a predecessor to make activation reconcile.

Only deletion-free static inspection is permitted for this revision. No tests,
formatters, Terraform, Helm, provider, cluster, database, registry, credential,
deployment, or cleanup action is authorized.
