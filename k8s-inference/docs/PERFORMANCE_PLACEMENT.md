# Measured performance and advisory placement

The performance service reuses the platform's managed PostgreSQL database,
public durable operation APIs, existing Kueue admission and model controllers.
It is not a second Kubernetes scheduler and does not require an LLM to interpret
files. Kubernetes/Kueue remain responsible for resource admission and binding;
KEDA/the existing model controller remain responsible for their replica sets.

## State and execution

Migration 0035 adds immutable campaign specifications, individual trials and
fenced worker-attempt history. The operator API is
`/admin/api/v1/performance/campaigns`; the admin console links Capacity →
Benchmarks. Artifacts hold the detailed receipts; PostgreSQL holds the durable
results, measurements, hashes, operation IDs and complete outcome denominator.
Artifact retention is the platform's configured retention, not an indefinite
archive promise. Database backup/HA follows the existing managed database.

Opt-in CPU-only benchmark workers are a Kubernetes Deployment. Fixed replicas
bound aggregate concurrency; database row locks bound each campaign. Workers
claim a trial, renew a 120-second fenced lease every 25 seconds and submit with
trial-derived public idempotency keys. A restarted worker recovers the same
operation. Replayed client latency is null, never a falsely short new execution.
Credentials are mounted Secrets, not command-line arguments or tfvars values.
No cloud credential, kubeconfig or Kubernetes API token is mounted in workers.

Configure `deployment.applications.control_plane.benchmark_workers` in tfvars:
`enabled`, immutable `image`, 40-character `source_commit`, `replicas`,
`credential_secret`, `credential_key`, and CPU `node_selector`. Helm exposes
the same `benchmarkWorkers` settings. An ordinary expiring benchmark identity
needs grants for the tested Apps, artifact upload, operation access and MCP
for the LeRobot artifact client. Its concurrency/budget must match the intended
experiment; workers never increase these limits.

Campaign inputs are SHA-bound fixtures with explicit workload classes,
repetitions, runtime adapter and cache condition. Entire inventories include
unsupported/excluded entries with reasons. An App alias is not silently counted
as a second model. Local-GPU placement excludes external Token Factory models.

## Evidence and recommendations

Measurements distinguish end-to-end, queue, startup, execution and allocated
GPU-seconds. Missing values are null. End-to-end includes upload, polling,
download and semantic validation; a two-request trial is not one request's
latency. Native execution intervals include runtime-adapter work, not just
CUDA kernels. Multi-stage scientific runs need per-stage analysis and are not
misrepresented by one GPU identity. Node/Pod UIDs bind hardware observations;
requested pool labels alone never prove where work executed.

The deterministic `measured-runtime-median-v1` advisory policy is returned with
campaign details. It has no write path to model replicas or scheduling policy.
Comparisons require identical model/input/workload/cache, identical runtime
image, at least three valid execution measurements per observed environment,
no unresolved failures, and at least two environments. Uncontrolled cache
cohorts and overlapping measured ranges yield “more evidence needed,” not a
winner. Responses include a hash of the exact durable result basis. Three
samples are a baseline, not a tail-latency SLA. The objective is execution time,
not price, GPU availability, or a cross-runtime scientific-equivalence claim.

After initial baselines, use separate controlled warm/cold/snapshot cohorts and
isolated compatible hardware variants to compare H100/L40S/H200 or future GPU
types. Never evict customer models or change their policy merely to manufacture
a cold start. Snapshot validity remains tied to the exact runtime/driver/GPU
contract; a cache level label is not proof that a restore happened. Full-node
network benefits require distributed stages and suitable payloads, not an
assumption that every single-GPU model benefits from the faster NIC.

## Benchmark implementation and boundaries

`acceptance/performance-placement-20260920/` contains the campaign client,
managed worker, immutable fixture preparation, existing model-specific semantic
validators, phase collection and guarded rollout. The CPU worker has a locked
RDKit environment for independent chemistry validation and the existing locked
LeRobot reader for reopening datasets. Speech uses the complete public
PriMock57 consultation with a ground-truth transcript. Format/identity checks,
WER observations and clinical/scientific validity remain separate claims.

The full LeRobot reader is qualified with a read-only filesystem, non-root UID,
no network and a 4 GiB worker memory limit. Its Torch compiler cache is explicitly
under `/tmp`, and CPU numerical libraries use one thread. The two robotics
cases run in a separate campaign with `max_parallel: 1`; full-frame dataset
validation is memory-intensive and is not replaced with a metadata-only check.

The September 20 first campaign is exploratory: failures in the original test
runner (wrong Cosmos operation, externalized results, upload replay, missing
RDKit) are retained and followed by corrected campaigns. Do not count those as
model failures, erase them, or use them for hardware recommendations. MolMIM's
`generation_exhausted` is a real model/request outcome, distinct from a harness
dependency failure. Current data gathering is not a customer-readiness verdict.
