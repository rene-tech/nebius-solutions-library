# Nested-module credential custody v9

This additive source-only successor preserves v8 and every predecessor. It
does not authorize integration or deployment. Production trust, provider
adapters, accepted SAI-08/SAI-09 integration, dynamic tests, and live evidence
remain absent; SAI-06 has static SOURCE GO only.

## Exact Terraform ownership

The reference-data credential delivery Secrets are resources of the
`reference_data` module instantiated by the workloads root. Their reviewed
configuration addresses are therefore:

- `module.reference_data.kubernetes_secret_v1.object_storage`
- `module.reference_data.kubernetes_secret_v1.object_storage_versioned`

They are not resources of a separate `reference-data` state root. Raw state
may add a module instance key and a resource generation key, for example
`module.reference_data[0].kubernetes_secret_v1.object_storage_versioned["2"]`.
Custody removes instance keys only for comparison with the registered
configuration address; it retains the complete module path and separately
parses the terminal resource generation.

The workloads feature-activation marker remains the sole non-secret authority
for whether reference-data delivery credentials are enabled. An enabled marker
requires the exact fixed module address. A disabled marker requires both fixed
and versioned module addresses to be absent. A root-level alias, a shortened
address, a different module path, or a separate state-root claim fails closed.

## Secret and predecessor binding

Secret classification uses the terminal Terraform resource type rather than a
root-only string prefix. This ensures module-owned Secrets participate in the
same state/live UID, resourceVersion, decoded-content commitment, class,
generation, immutability, consumer-readiness, and receipt checks as root-owned
Secrets.

The generation-one adoption exception remains exact and non-mutating. It is
bound to the canonical workloads module address and accepts the raw `[0]`
module instance only as that address's state representation. It does not apply
to a versioned successor, a root-level alias, another module, or another state
root.

## Preserved boundaries

The registry remains an exhaustive 12 required, seven feature-gated, and two
dependency-optional class partition. The other nine feature groups are
unchanged. PostgreSQL backup optionality, the two authoritative activation
markers, fixed-v1 nonmutation, immutable successors, and the pinned empty AWS
shared-credentials sentinel with ambient discovery disabled are preserved.

Only deletion-free static inspection is permitted for this revision. No tests,
formatters, Terraform, Helm, provider, cluster, database, registry, credential,
deployment, or cleanup action is authorized. Rollback remains source-forward:
retain this revision, v8, every credential generation, every evidence record,
and all customer data while selecting a later independently reviewed source
commit. Never delete, replace, state-forget, revoke, or overwrite a predecessor.
