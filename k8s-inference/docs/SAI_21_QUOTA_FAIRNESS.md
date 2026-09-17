# SAI-21 quota and fairness remediation

Status: source candidate for independent review. This document does not claim
integration, deployment, or live acceptance.

## Admission contracts

Scientific artifact uploads now reserve bytes before an upload handle is
issued. `FS2_ARTIFACT_TENANT_QUOTA_BYTES` (Helm
`scientificArtifacts.tenantQuotaBytes`) bounds the sum of retained upload
intents for one tenant, including unfinished uploads and finalized artifacts.
PostgreSQL serializes reservations with a transaction-scoped advisory lock on
the tenant identity; the in-memory implementation applies the same rule under
its repository lock. Exact idempotent replays remain available after the quota
is full. A new reservation that would exceed the limit returns
`artifact_quota_exceeded` with HTTP 429. Retention purge remains the sole owner
of releasing retained artifact capacity.

Scientific GPU submissions are charged at durable admission using this
conservative upper bound for every GPU stage:

```text
accelerators per Pod × expanded Pods × maximum attempts × execution deadline
```

The service-class maximum execution duration is authoritative when present;
otherwise the reviewed stage active deadline is required. A GPU stage with no
bounded duration is rejected. The total participates in the token budget check
and is atomically added to `gpu_seconds_used`; it is not left as a generic
worker reservation because scientific workloads do not use the generic claim
and release lifecycle. Idempotent replay does not charge twice. Lifecycle
telemetry continues to report observed usage separately from this conservative
admission debit.

Scientific GPU scheduling accepts only a LocalQueue route whose `tenant_ids`
selector is exactly the requesting tenant. An unrestricted or cross-tenant
fallback cannot admit GPU work, even when it has a more specific model or
service-class selector. CPU placement remains bound by its existing stage-class
contract. The built-in academic lane is bound to its configured academic
tenant; other customers require separately owned LocalQueues with singleton
tenant selectors before scientific GPU admission is enabled for them.

## Staged rollout and rollback

Before rollout, inventory every tenant allowed to submit scientific GPU work
and add one namespace-appropriate LocalQueue route per tenant. Confirm each
route points to the intended ClusterQueue and retains its existing model and
service-class scope. Set the byte quota no larger than bucket capacity and no
smaller than the single-object ceiling. A rollout must first verify the current
shared-service image provenance and integrate all deployed sibling work.

The rollback is a source/image rollback to the prior reviewed control-plane and
scheduling contract. Do not remove artifact records, upload intents,
LocalQueues, or usage accounting as part of rollback. Tenant-byte reservations
remain governed by normal retention, and admission charges remain durable audit
history.

## Verification status

Regression coverage is authored for in-memory and concurrent PostgreSQL byte
reservations, HTTP 429 mapping, chart/settings propagation, exact tenant queue
routing, conservative GPU estimation, durable token debit, and idempotent
replay. The SAI-21 task coordinator prohibited execution of tests, builds,
formatters, Terraform, and Helm, so none of those checks was run for this
candidate. No cluster, database, registry, credential, provider, or live
service was inspected or mutated.
