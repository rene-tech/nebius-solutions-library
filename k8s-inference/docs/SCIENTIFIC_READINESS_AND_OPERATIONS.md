# Scientific cluster: readiness and operator guide

Current customer-serving release: `c85aa26e`, deployed through Terraform on
8 September 2026. Two consecutive customer cohorts passed across all nine
trial variants, including ordinary HTTP/MCP traffic, real admin workflows,
automatic priority recovery and complete route-log checks. The
[final customer handoff](../acceptance/customer-trial-remediation-20260907/FINAL-ACCEPTANCE.md)
records the current endpoints, exact identities, measurements and limits.
The earlier fleet qualification below remains dated evidence for all ten
scientific profiles; it is not the current control-plane release identity.

All ten scientific profiles passed the
[final public fleet acceptance](../acceptance/scientific-fleet/evidence/final-fleet-acceptance-h100-20260906.md)
on runtime source `8bb53aab`, on 6 September 2026. Real-browser
[customer-key and scientific model controls](../acceptance/scientific-fleet/evidence/customer-access-policy-h100-8bb53aab-20260906.md)
also passed, including revoked-key rejection, pause/resume, cap-one dispatch,
artifact downloads and restoration of the original settings. Use the Terraform
outputs for the final deployment identity and access bundle. Measurements below
retain their original sources and clocks; snapshots remain experimental.
The [September 6 deployed release](../acceptance/scientific-fleet/evidence/final-h100-release-20260906.md)
was `adf1d842`, including the qualified catalog and MCP/admin fixes. It has been
superseded by the customer-trial fixes above. The current release also passed
zero-change three-stage Terraform post-apply plans.

## Start using the cluster

Use the private `inference-stack output --var-file terraform.tfvars` access
bundle for the public HTTPS admin portal, MCP endpoint, inference endpoint,
and credentials. The admin bootstrap token signs into the admin portal; it is
not a customer inference key. The scientific access token has the academic
tenant's scientific access. Do not distribute the admin token to participants.
See [access and observability](ADMIN_OBSERVABILITY_ACCESS.md) for the exact
output fields and Grafana credentials; no port forwarding is required for the
configured public endpoints.

Follow the [scientific API quick start](SCIENTIFIC_BATCH_API.md), including its
per-model examples, for this workflow:

1. Discover authorized models using `GET /v1/scientific-models` or MCP
   `list_scientific_models`. Scientific batch profiles are not the OpenAI
   `/v1/models` listing.
2. Upload and finalize input artifacts, then submit the model's canonical
   request with an idempotency key. Include `batch_id`, `correlation_id`, and
   `display_name` to identify a customer's workload.
3. Poll the operation and events. On success, download the output manifest and
   result artifacts and verify their digests. Success includes the profile's
   semantic validation, not merely a container exiting successfully.
4. Cancel an unwanted operation through the admin UI or operation cancel API.

Issue customer keys from Access, selecting their tenant and exact scientific
model IDs. Scientific-scoped key creation, authorized discovery and revocation
have passed live acceptance. Never assume a general serving key grants academic
model access. Native AlphaFold3 and BindCraft with
PyRosetta are restricted to the configured academic tenant; the deployment's
entitlement record is not a commercial license grant for other customers.
OpenFold3 is an independent alternative, not a renamed AlphaFold3 backend.

## Models and measured requests

The ten scientific profiles use pinned upstream/native runtime images, not NIM
containers. Their [catalog](../catalog/runtime/contracts/scientific-workload-profiles.json)
records source, image, artifact and recipe identities. The following H100
requests passed semantic validation, replay, and public artifact download on
`5fcc8323`; details and immutable receipt identities are in the
[varied-input fleet report](../acceptance/scientific-fleet/evidence/customer-readiness-h100-20260906.md).

| Public model ID | Tested request | Public workflow wall time |
| --- | --- | ---: |
| `proteina-complexa` | Three samples, seed 4 | 782.683 s |
| `boltzgen` | Concurrent customer/bulk requests, 20 candidates each | 812.502 / 810.991 s |
| `mosaic` | Two independent 44-residue binder shards, 30 steps | 778.817 s |
| `bindcraft` | Two independent designs | 499.329 s |
| `rfdiffusion` | Four independent 96-residue designs | 418.187 s |
| `esmfold2` | 76-residue ubiquitin | 284.166 s |
| `esmfold2-fast` | 76-residue ubiquitin | 104.244 s |
| `protenix-v2` | Ubiquitin, two seeds | 163.831 s |
| `alphafold3` | Ubiquitin | 84.788 s |
| `openfold3-openbind` | Ubiquitin, two seeds | 109.332 s |

These differing workloads are not a model-speed ranking or cold-start p50/p95.
Images and weights were partly warm; no snapshot was requested. Application
GPU occupied/active/idle intervals reconciled, but not every backend exposes
separate weight-loading and compilation phases.

Qwen and Cosmos also passed [general-serving checks](../acceptance/general-serving/README.md)
on separate releases, including two requests each on runtime `8bb53aab` in the
[final general-model canaries](../acceptance/general-serving/evidence/h100-8bb53aab-canary-20260906.md).
Qwen's six short hot requests on `29b7e01a` took
0.410–0.600 s from acceptance to durable completion; this is not maximum token
throughput, and the non-streaming public path did not measure TTFT.

Cosmos 3 Nano uses the native vLLM-Omni/Hugging Face route, not NIM. Its six
recovered-acceptance requests on `5fcc8323` passed: 25 frames at 448×256, with
MP4 validation. One replica-cold activation on existing capacity took 67.72 s
from acceptance to ready and 69.13 s to durable completion. Four hot operations
took 1.25–1.55 s from acceptance to durable completion, not client polling time.
The initial rollout/retry activation took 467.01 s to ready and 468.16 s to
durable completion; it is not a clean cold baseline. These ready clocks include
any queue/capacity/retry time, unlike the capacity-available fast-start clock.
Larger media outputs and perceptual quality were not qualified by those tests.

## Manage customers, priorities, and capacity

In the admin portal, use the model catalog and scientific operation details to
inspect backend identity, authorization, stages, attempts, artifacts, and
timings. Serving-model hot floors and ceilings belong to the live
[model configuration](../DYNAMIC_MODEL_CONFIGURATION.md). Scientific models
instead execute staged, queued Jobs; their availability is not a permanently
resident GPU worker. In Scientific runs, use model policies to pause new
dispatch or set maximum active runs globally or per tenant. Existing work drains,
accepted queued work remains durable, and resuming releases held work. A run
may contain multiple GPU shards: an active-run cap is not a GPU-count limit.

For batch submissions, choose an authorized `service_class`, normally
`customer-batch` or `bulk-backfill`. Presentation/interactive classes require
the corresponding tenant entitlement. Callers do not choose raw Kubernetes
priorities or queues. Kueue combines queue entitlements, borrowing, fair sharing,
and priority; higher priority is not a promise of immediate preemption across
every queue. See [queue and telemetry contracts](QUEUE_AND_GPU_TELEMETRY.md).

The measured RFdiffusion burst completed 18 bulk designs plus a delayed
customer request, reached 17 simultaneous GPU admissions across reserved and
preemptible capacity, and retried an evicted bulk attempt successfully. The
customer request completed in 334.211 s and bulk in 581.140 s. A subsequent
[admin-browser cancellation](../acceptance/scientific-fleet/evidence/optimized-stages-h100-29b7e01a-20260906.md)
stopped GPU processes in under 1.5 s and removed their pods in under 2.5 s after
acceptance, without shortening the configured 90 s termination grace period.

Terraform owns physical pool floors/ceilings and compatible GPU placement;
model concurrency cannot create capacity beyond that envelope. Zero floors
are supported but add node acquisition time. The architecture supports
heterogeneous GPU pools, while these scientific receipts qualify H100 only.
For presentations, consider keeping both required GPU and CPU capacity warm:
the measured CPU pool scale-from-zero alone took 102 s.

## Startup improvements and snapshot boundaries

The [optimized-stage report](../acceptance/scientific-fleet/evidence/optimized-stages-h100-29b7e01a-20260906.md)
records three requests each for mosaic and RFdiffusion on `29b7e01a`:

| Same workflow as the fleet test | First request, CPU pool initially zero | Two subsequent warm requests |
| --- | ---: | ---: |
| mosaic | 412.959 s | 120.004 / 124.448 s |
| RFdiffusion | 417.833 s | 108.160 / 108.162 s |

Their CPU finalizers now pull a 59 MB tools image in 3.656 s instead of a
multi-GB GPU image. RFdiffusion also avoids host-thread oversubscription.
These full workflows include capacity, execution, and output publication;
do not pool the first and warm repetitions into a cold-start percentile.
Other CPU stages still need their upstream model runtime. See the
[CPU-stage image contract](../components/control-plane/docs/scientific-cpu-stage-images.md).
The consolidated input-materialization path passed the final ten-model campaign.
These earlier paired numbers do not isolate its benefit. Some other CPU
preparation stages still pull their full model image: the live ESM policy test
measured 138–149 s cold pulls on two CPU nodes and 29–30 s GPU artifact checks.
Keep those costs separate from inference and from CPU/GPU capacity queueing.

Production scientific requests retain normal loading. The
[startup and snapshot evidence](../acceptance/scientific-startup/README.md)
separately proves:

- Companion startup/verification improved from a 13.761 s to 3.427 s median
  across three repetitions per variant; this is not full model startup.
- Persisted ESMFold2 restore into fresh pods passed three times, with exact
  model-tensor verification and changed full-settings requests. Median
  pod-to-ready was 11.453 s with the same H100/GPU UUID and warm OS file cache.
- One genuinely disk-cold restore read approximately 20.15 GB and took
  **324.646 s** to ready, versus approximately 15 s ordinary internal model
  loading. The clocks differ, but disk restore alone is already slower.
- The isolated original-request bridge preserved UID10001, command identity,
  artifacts and GPU release. Its roughly 21 s warm pod-to-completion results
  do not qualify another GPU, another model, or production controller use.

The experimental checkpoint PVC is retained for reproduction, with no live
probe worker; its exact identity and cleanup scope are in that report. There
is no claim of local NVMe on H100, reserved L4 RAM, portable GPU snapshots, or
production snapshot enablement for all models.

[Fast-start levels](../FAST_START_LEVELS.md) are customer timing targets, not
names for storage technologies: Off has no target; L1/L2/L3/L4 target
300/120/60/30 s from compatible GPU capacity being available to readiness.
Queue/node wait and request execution are separate. Requested, effective, and
qualified levels can differ; one fast probe cannot certify a level.

## Read usage and investigate delays

Use operation, attempt, tenant, opaque key identity, model, and workload IDs
to connect admin records with Grafana metrics, Loki logs, and Tempo traces.
The [access guide](ADMIN_OBSERVABILITY_ACCESS.md) explains the configured UI
links; raw observability services need not be publicly exposed.

Distinguish queue-reserved time, pod GPU-occupied time, application-active time,
and actual device utilization. Application-active does not mean CUDA kernels
were continuously busy; use DCGM for device utilization. Loading and grace-period
idle time should be attributed only where its boundaries were observed.
For example, the preempted burst's reconciled ledger still labels inferred
boundaries **estimated**. Missing data is unavailable, not zero. An aggregate
`artifact-load` interval can include image waiting and must not be reported as
pure weight-transfer time. These distinctions are necessary before using the
records for customer charging or optimization decisions.
