# Scientific performance registry

The performance registry is part of the existing control plane, not a second
GPU scheduler. PostgreSQL stores immutable campaign inputs, individual trials,
fenced worker attempts and final measurements. Raw receipts and outputs belong
in the existing Object Storage artifact service. Local JSON exports are evidence
copies, not the source of scheduling state.

## Ownership and scope

- Kueue continues to admit batch work; existing model controllers and KEDA keep
  their current ownership of workload definitions and serving replicas.
- Terraform/Helm install the infrastructure and service. Operators submit
  campaigns through the admin API; they do not edit Terraform for each model.
- Placement remains advisory. No benchmark writes a ModelDeployment policy.
- Token Factory LLMs are outside local GPU comparisons. CPU Apps remain in the
  catalog denominator but must not be charged as GPU work.

## Operator API

All routes use existing operator sessions and authorization:

| Route under `/admin/api/v1/performance` | Purpose |
| --- | --- |
| `GET /campaigns` | Bounded campaign list with all outcome counts |
| `POST /campaigns` | Create immutable, idempotently named experiment |
| `GET /campaigns/{id}` | Trials, inputs, results and grouped performance profiles |
| `POST /campaigns/{id}/claim` | Claim one trial within campaign concurrency |
| `POST /trials/{id}/heartbeat` | Renew a live worker lease |
| `POST /trials/{id}/result` | Atomically commit a fenced, immutable result |

`/admin/capacity/benchmarks` presents campaign coverage and measured timings.
Viewer access reads results; existing global administrator access runs workers.
The OpenAPI contract defines exact request shapes. Inputs reference committed,
hashed fixtures; they cannot supply executable shell commands.

## Measurement rules

Each cohort separates model, workload class, fixture digest, cache condition and
actual runtime/hardware identity. Report request-to-result latency, queue,
startup, execution and GPU-occupied time separately. Missing observations are
null, never zero. Unknown cache residency is `uncontrolled`, not `cold`.

Three valid repetitions provide baseline evidence, not reliable p95/p99. Do not
compare a tiny smoke fixture with a production-size workload, or recommend a GPU
solely from its marketing name. Record actual GPU count, full-node topology,
runtime digest and environment fingerprint. A requested pool is not proof of
where a job ran. Unsupported/unavailable models remain visible in the campaign.

Success requires an operation ID and a model-specific semantic validation result,
not merely HTTP 200. Receipts reference durable artifacts and their SHA-256.
Capacity shortages remain distinct from product failures and still contribute
to customer waiting-time evidence.

## Recovery and deployment

Trials use PostgreSQL row locking and `SKIP LOCKED`, a lease and a monotonically
increasing fencing number. An expired worker cannot publish a result after a
replacement has claimed its trial. Publication retries with exactly the same
result are idempotent. The executor must reuse the trial UUID for public
inference idempotency and resume the same durable operation after interruption.
This is not a claim of exactly-once GPU execution.

Migration 0035 is additive, but the platform validates an exact migration
manifest. After applying it, recover with a schema-35-compatible image; blindly
rolling back to a schema-34 image is not a supported recovery plan. Preserve
fresh live Helm values, including unrelated releases, and verify the revision
before an upgrade. The gateway is stateless with respect to campaign progress.

## Qualification boundary

The first release installs durable experiment bookkeeping and read-only
performance summaries. It does **not** yet enable automatic cross-GPU placement,
global allocation optimization, forced cold starts of customer Apps, or a
claim that all catalog models have been benchmarked. Campaign receipts and the
deployment report record those outcomes separately. Advisory placement needs
comparable validated hardware cohorts before it can make useful recommendations.
