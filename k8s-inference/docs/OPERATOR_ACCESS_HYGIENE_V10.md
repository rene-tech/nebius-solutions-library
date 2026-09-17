# Operator access hygiene v10: data-source and greenfield state admission

This additive source contract refines the v9 durable-credential inventory. It
does not authorize deployment. Production authority configuration, adapters,
dependency integration, and independent acceptance remain required.

## Managed resources versus data sources

The migration guard consumes Terraform's explicit resource `mode`. A
`mode=data` entry is read-only configuration/plan input and is excluded from
the durable managed-resource address registry. Unknown modes fail closed.
Nested `mode=managed` resources retain their complete module path and remain
subject to exact registration, move protection, state binding, and Secret
custody. In particular, reading an existing Kubernetes Secret as a data source
does not register that read as ownership of the Secret.

## Three non-interchangeable state modes

Every authority-owned Terraform root declares exactly one mode:

- `legacy-copy` requires the existing additive, exact-copy migration proof,
  retained source, nonzero serial, and exact lineage.
- `greenfield-empty` requires a null lineage and no legacy source. A distinct
  digest-pinned provider adapter must attest that the exact backend object,
  every object version, and its lock are absent and that the complete scoped
  project IAM and cluster Secret inventories contain no managed credential.
- `remote-established` requires the already-created remote lineage and uses
  the normal authoritative state and live-object gates.

Migration/adoption evidence cannot stand in for greenfield evidence, and an
absent migration file cannot select greenfield mode. The provider binds the
bootstrap observation to the exact project, cluster, namespace set, registry,
Terraform root, backend object, and purpose-specific backend session. The
guard re-observes it at planning, native-gate evaluation, saved-plan sealing,
and saved-plan apply.

## First installation

Only `greenfield-empty` may initialize an absent backend without running
`terraform state pull`. The wrapper supplies an in-memory empty prior-state
shape; it does not create a local state file. The plan is rejected if any
managed prior-state resource exists. A fixed generation-one resource may be
created only under this fresh bootstrap proof. Planned Secrets still require
an immutable, write-only, class/generation/content commitment and the staged
provider admission path.

After the first successful apply creates a remote lineage, the root authority
configuration must advance to `remote-established` with that provider-observed
lineage before any later command. Reusing `greenfield-empty`, fabricating a
local empty state, or presenting a migration/adoption receipt cannot authorize
a later create.

## Rollback and preservation

Rollback preserves every state object/version, credential generation,
authority observation, and predecessor source commit. It may select an earlier
application version that reads the retained generations, but it must not
restore local plaintext state, change an established root back to greenfield,
discard a lineage, overwrite a fixed generation, or remove any evidence. A
failed bootstrap leaves the backend and live inventory unchanged and must be
re-observed rather than cleaned up.

This revision was prepared with static source inspection only. No tests,
formatters, Terraform/Helm commands, provider calls, credential actions, or
live mutations were performed. Integration remains blocked on semantic
composition with the accepted SAI-06 source and accepted SAI-08/SAI-09
successors.
