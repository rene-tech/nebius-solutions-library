# Dynamic model configuration

## Decision

Model lifecycle belongs in a Kubernetes-native control plane, not in Terraform.
Terraform should continue to own the infrastructure boundary: the cluster, GPU
node-pool envelopes, storage, registry, database, Gateway, operators, CRDs,
observability, and platform identities. Operators can add, update, scale, warm,
drain, and expose a model without running a Terraform plan, provided
the change fits inside an already-provisioned pool and policy envelope.

The implemented baseline is an FS2 `ModelDeployment` API and reconciler. The
admin API records a desired revision and projects that custom resource, the
reconciler renders the selected serving resources, and Kubernetes status
remains the live source of truth. PostgreSQL retains desired revisions,
idempotency records, and status history; it does not become a second scheduler.

This is an incremental replacement for Terraform-owned model Deployments. It
does not require replacing the current gateway, identity, metrics, queueing, or
model runtime in one migration.

## Why this composition

| Component | Recommended responsibility | Boundary |
| --- | --- | --- |
| FS2 `ModelDeployment` CRD and reconciler | Stable, runtime-neutral customer API; policy; validation; rendering; conditions; rollback | The only writer of generated serving objects |
| KServe `LLMInferenceService` | Large language-model serving, multi-node topology, workload-variant autoscaling, and llm-d integration | Preferred renderer target for compatible LLM runtimes |
| KServe `InferenceService` / `ServingRuntime` | Scientific, imaging, embedding, and custom HTTP/gRPC runtimes | Used where the LLM API is the wrong abstraction |
| Gateway API Inference Extension and llm-d | Request-aware routing using queue, KV-cache, LoRA, and load signals | Routing and scheduling within a running model service |
| KEDA or KServe workload-variant autoscaling | Replica floor/ceiling, event-driven activation, and scale from zero where the chosen runtime supports it | Pod replicas, not cloud node-group limits |
| Kueue | Admission, quotas, ResourceFlavor selection, fairness, and preemption for batch jobs and individual serving Deployment Pods | KEDA/Deployment remain replica owners; Kueue gates each Pod and accounts quota |
| KServe `LocalModelCache` plus FS2 cache/snapshot adapters | Durable-to-node-local artifact localization and explicit readiness | Cache presence is separate from a hot GPU replica |
| NVIDIA Dynamo Kubernetes Platform | Optional optimized backend for qualified large-model profiles, including disaggregation and snapshot-aware startup | Not the universal customer API; non-NVIDIA and non-LLM runtimes remain supported |
| ModelMesh | Optional high-density loading for many small, compatible models | Not the default for very large multi-GPU models |
| Nebius managed node-group autoscaler | Adds/removes nodes within Terraform-defined pool bounds when serving pods are unschedulable | Pool shape, reservation, capacity mode, and maximum remain infrastructure policy |

KServe 0.20 exposes `LLMInferenceService` configuration for single- and
multi-node workloads and workload-variant autoscaling. Its 0.18 release added
LeaderWorkerSet-based multi-node inference and namespace-scoped model caching.
The Gateway API Inference Extension exposes `InferencePool` and an endpoint
picker that can make model-aware decisions from pending requests, active LoRA
adapters, and KV-cache state. llm-d composes those Kubernetes APIs with vLLM
and documents both KEDA and HPA-based autoscaling. These are preferable to a
new proprietary scheduler because they preserve replaceable upstream seams.

NVIDIA Dynamo is worth retaining as a selectable execution profile: its
Kubernetes operator supplies deployment and scaling CRDs and NVIDIA documents
snapshot-accelerated startup. It should sit behind the FS2 model contract rather
than define it. BioNeMo services, medical-imaging services, media generation,
and future non-NVIDIA runtimes do not all share Dynamo's serving assumptions.

## Customer API

Operators use the admin console at `/admin/model-deployments` or its
same-origin API rather than hand-authoring `ModelDeployment` resources. The
server returns the model, pool, queue, runtime, cache, and tenant choices from
the installed Terraform envelope, then carries one proposal through the same
preview and apply path used by bootstrap:

```text
GET  /admin/api/v1/model-deployments:capabilities
POST /admin/api/v1/model-deployments:validate-preview
POST /admin/api/v1/model-deployments:plan-preview
POST /admin/api/v1/model-deployments:apply
GET  /admin/api/v1/model-deployments/{name}/status
POST /admin/api/v1/model-deployments/{name}:drain
POST /admin/api/v1/model-deployments/{name}:rollback
POST /admin/api/v1/model-deployments/{name}:reconcile
```

The resulting resource uses the shipped
`inference.fs2.nebius.ai/v1alpha1` schema. Its required sections are
`modelRef`, `tenantId`, `lifecycle`, `artifact`, `runtime`, `placement`,
`availability`, `cache`, `queue`, `rollout`, `exposure`, and `policy`; values
such as immutable digests and renderer template references come from the
server-published configuration options.

The live configuration surface exposes these controls:

- enabled/draining state and immutable artifact/runtime revisions;
- zero or more hot replicas, maximum replicas, idle timeout, and optional
  scheduled warm windows;
- target request latency or queue depth, concurrency, and rollout strategy;
- one or more compatible existing pool references and accelerator count;
- cache tier and snapshot preference, with qualification status shown
  separately from intent;
- an optional fast-start performance class (`Off`, `L1`..`L4`) or automatic
  bounds, with the qualified and effective level shown separately from the
  requested one;
- queue, priority, tenant visibility, OpenAI route, and MCP exposure;
- per-model request, token, concurrency, and GPU-time policy references.

`minReplicas` is the number of GPU replicas that stay hot. Setting it to zero
permits true cold operation. A cached-but-not-hot status means weights are on
the requested cache tier but no GPU replica is serving. The autoscaler may raise
replicas to `maxReplicas`; the cloud node scaler then supplies nodes up to the
Terraform-defined pool maximum. Operators must see the phases separately:
admitted, node pending, artifact localizing, runtime starting, warming, ready,
draining, and failed.

### Fast-start performance classes

`spec.fastStart` is optional and backward compatible; an absent or default
policy asks for nothing and keeps every existing revision ETag stable. A level
is a startup-time target measured from GPU capacity being available until
semantic endpoint readiness. Capacity wait and total end-to-end time are
separate measurements and never count against a level. `Hot` (a ready replica)
is derived runtime state, not a configurable cold-start level.

| Level | Target |
| --- | --- |
| `Off` | no startup-time target |
| `L1` | at most 300 seconds |
| `L2` | at most 120 seconds |
| `L3` | at most 60 seconds |
| `L4` | at most 30 seconds |

```yaml
fastStart:
  mode: Fixed | Automatic            # default Fixed
  level: Off | L1 | L2 | L3 | L4     # Fixed target, default Off
  minimumLevel: Off | L1 | L2 | L3 | L4   # Automatic lower bound, default Off
  maximumLevel: Off | L1 | L2 | L3 | L4   # Automatic upper bound, default L4
  fallbackPolicy: AllowLowerLevel | RequireTarget   # default AllowLowerLevel
```

`Fixed` targets exactly `level`. `Automatic` starts at its configured minimum
and evaluates at most every five minutes using payload-free one-hour and
seven-day request and idle-gap aggregates, qualified model-start p95, configured
mechanism costs, and persisted promotion/demotion hysteresis. It can select
only a level supported by qualified paths inside `[minimumLevel,
maximumLevel]`. A level is qualified only by compatible benchmark evidence in
the Terraform-owned envelope
(`qualifications.<modelRef>.fastStartEvidence`) for the complete v2 runtime
identity: artifact/source, image, rendered argv/environment, reviewed
cluster/GPU/driver/CUDA/storage environment, pool/capacity/startup scenario,
cache/mechanism/snapshot, and semantic measurement contract, with at least 20
successful samples, no failed or timed-out attempts, and a nearest-rank p95
model-start time within the target. Mechanism names such as
regional caches, snapshots, host RAM residency, or ModelExpress are operator
detail carried by the evidence; a name alone never qualifies a level. With
several pools, the slowest pool binds the deployment.

`AllowLowerLevel` deploys at the best qualified level and reports the
shortfall as `Fallback`; `RequireTarget` rejects the revision with
`fast_start_target_unqualified` when the fixed level or the automatic lower
bound is not qualified. Changing `spec.fastStart` is a live policy change and
needs no cold cutover.

`status.fastStart` keeps `requestedLevel`, `qualifiedLevel`, `assignedLevel`,
and `effectiveLevel` apart. The effective level is claimed only once the
desired render has converged; until then the previously effective level, if
any, is carried forward for that same `effectiveIdentityDigest`.
`selectedIdentityDigest` identifies the current qualifying evidence.
`pools[].retainedPaths` preserves LegacyUnbound, expired and mismatched
evidence with bounded JSON-path reasons without allowing it to qualify.
`modelStart`, `capacityWait`, and `endToEnd` carry
latest/p50/p95 seconds per binding pool and per pool in `pools[]`, and are
omitted entirely when no compatible evidence exists. The `FastStartQualified`
condition is `True` for `NoTarget` and `Qualified`, `False` for `Fallback` and
`Unqualified`.

Benchmark receipts are not pasted into `terraform.tfvars`. Use
`models/cold-start/project_fast_start_evidence.py` to create a compact JSON
projection and set only
`deployment.dynamic_models.fast_start_evidence_file` to its absolute path.
Qualification also requires reviewed absolute
`fast_start_environment_qualifications_file` and
`fast_start_measurement_contracts_file` inputs. Omitting either fails closed;
Terraform never derives live driver/CUDA or semantic-validator facts from a
catalog declaration.
Changing that file content changes the immutable envelope digest and rolls the
controller without rewriting any live `ModelDeployment` policy.

The cluster-wide economic inputs are
`deployment.dynamic_models.fast_start_wait_second_value` and
`deployment.dynamic_models.fast_start_mechanism_hourly_costs`. The controller
combines them with demand history to compare multiple qualified mechanisms.
An unlisted mechanism has zero additional hourly cache cost; this is a cost
default only and never fabricates timing evidence. The existing operation
latency spans request acceptance through readiness and therefore is not used
as a fast-start target-miss signal. That signal remains disabled until the
capacity-available boundary is stored explicitly.

Automatic mode evaluates only mechanisms compatible with the revision's
existing artifact, runtime template, cache tier, snapshot identity, accelerator
class, and accelerator count. It does not mutate those desired fields. A
different physical path is selected through a reviewed live revision once its
template and evidence are in the Terraform-owned envelope; provisioning a new
storage or pool capability still belongs to Terraform.

### Implemented heterogeneous elasticity

`poolRefs` is a bounded policy set inside the Terraform-owned envelope, not a
request for one arbitrarily chosen largest pool. The legacy renderer partitions
the single global interval `[0, maxReplicas]` into exact-pool workload segments:

- fixed hot segments consume only regular/reserved pools whenever the policy
  contains one; an all-preemptible policy necessarily keeps its floor there;
- autoscaled burst segments consume preemptible pools first;
- if the requested ceiling is larger than preemptible capacity, separately
  bounded regular-pool overflow segments use the remaining declared capacity;
- a single-pool policy retains one Deployment and one KEDA scale owner; and
- `maxReplicas == minReplicas` produces a fixed Deployment and no ScaledObject.

The server-authoritative create option defaults `poolRefs` to every compatible
pool so a newly selected model immediately receives both durable hot placement
and preemptible burst where available. The admin form exposes those choices as
independent checkboxes; an operator may deliberately narrow the non-empty set.

Because burst prefers preemptible capacity, a zero-hot-floor model activated
from cold provisions a fresh preemptible node and pulls its runtime image before
serving. That is the cheapest steady state but the slowest cold start. An
operator who wants faster cold activation can narrow `poolRefs` to an
already-running regular/reserved pool, where the image is cached and shared-cache
weights are already localized; this trades preemptible cost savings for
warm-node activation latency. Placement is runtime material, so the controller
applies the change as a cold cutover (drain to zero, re-place while cold,
re-enable); the whole cutover is live and reversible and needs no Terraform run.
The retained H100 measurements for exactly this trade-off are recorded in
[LIVE_ACCEPTANCE.md](LIVE_ACCEPTANCE.md#current-retained-h100-topology-2026-09-02).

The envelope must give every pool the unique
`accelerator.fs2.nebius/pool-id=<poolRef>` node selector. Each generated
Deployment has that exact selector and the pool's accelerator resource name.
Kueue evaluates the required placement against ResourceFlavor
labels before admitting each Deployment Pod, so a hot segment cannot be
admitted against a preemptible flavor when compatible regular pools are in the
policy (an all-preemptible policy necessarily keeps its floor there), and a
burst segment cannot consume the reserved pool unless it is the explicit
regular-overflow segment. A requested ordinary or scheduled hot floor that is
larger than the selected regular capacity fails as an infrastructure-required
change instead of spilling silently onto preemptible nodes. Every burst
Deployment has exactly one KEDA ScaledObject. Its PromQL demand interval is
offset and capped, so multiple HPAs cannot double-count demand and their maximum
replicas plus the fixed floor equals the model's global `maxReplicas` exactly.

The primary Service selects the controller's model identity across all hot and
burst Pods. Controller discovery, rollout readiness, draining, and replica
status aggregate every segment; `status.placements` exposes each Deployment's
pool, role, desired, ready, and available counts. This is GPU-neutral: pools may
use different accelerator resource names because each segment renders the
resource request from its own envelope record. During drain, the controller
retains the observed fixed-hot boundary and autoscaled total until active work
is proven absent, so withdrawing publication cannot silently replace a hot
workload with a burst workload.

Warm windows are controller-evaluated rather than duplicated as KEDA cron
triggers. On every bounded reconciliation poll, the controller evaluates the
validated cron expression in its IANA time zone and applies `durationSeconds`
to the fixed hot segments. This supports durations that cannot be represented
faithfully as a second cron expression and leaves KEDA as the sole owner of
burst scale subresources. Queue and WorkloadPriorityClass labels are present on
every generated Deployment; `maxQueueSeconds` remains the FS2 operation
admission deadline rather than being misrepresented as a Kueue Pod timeout.

## Ownership and safety rules

Avoiding dual ownership is essential:

1. Terraform installs the CRD, controller, upstream operators, pool policy, and
   an optional initial catalog. It does not manage individual
   `ModelDeployment` objects after migration.
2. The FS2 reconciler owns generated serving resources with a dedicated
   server-side-apply field manager. Users and Terraform do not patch those
   generated objects.
3. Admission validates image/model digests, pool compatibility, GPU budgets,
   tenant permissions, and bounds before accepting a revision.
4. A change that needs a new GPU shape, node pool, capacity block, VPC/storage
   resource, controller, or cluster-wide privilege enters an explicit
   `InfrastructureRequired` condition and produces a reviewed Terraform handoff.
5. Every mutation uses an optimistic revision/ETag, records actor and before/after
   policy in PostgreSQL, and can roll back to a prior accepted revision.
6. MCP and the public model catalog expose only reconciled `Ready` models allowed
   by the caller's PAT. Desired state alone never advertises a model as usable.

GitOps remains valuable for promotion and disaster recovery, but it should be
an optional export/import and approval path, not the only editing path. An admin
change can create a signed revision immediately; environments that require
review can configure the same controller to accept only revisions promoted by
their GitOps pipeline.

## Terraform bootstrap contract

The implemented root input is intentionally small. Customers select the model
catalog, model IDs, image overrides, pool overrides, KEDA floor/ceiling, and GPU
pools once; `deployment.dynamic_models` only selects the writer and optional
initial desired revisions:

```hcl
dynamic_models = {
  enabled                                    = true
  writes_enabled                             = true
  workload_owner                             = "controller"
  bootstrap_model_ids                        = ["cosmos3-nano", "qwen3-8b"]
  fresh_install                              = true
  fast_start_evidence_file                   = "/absolute/reviewed/fast-start-evidence.json"
  fast_start_environment_qualifications_file = "/absolute/reviewed/environment-qualifications.json"
  fast_start_measurement_contracts_file      = "/absolute/reviewed/measurement-contracts.json"
}
```

The exact retained H100 qualification for those two models is recorded in
[`h100-qwen-cosmos-elasticity-qualification-20260902.json`](catalog/profiles/evidence/h100-qwen-cosmos-elasticity-qualification-20260902.json).
It qualifies shared-cache scale-to-zero for the recorded reserved-H100 tuple;
it is not a GPU-snapshot qualification or a generic qualification for every
H100 pool.

Terraform derives the controller's immutable `InfrastructureEnvelope` and
`LegacyTemplateBundle` documents from the effective accelerator-pool contract,
selected catalog records, digest-pinned mirrored images, rendered manifests,
tenant, LocalQueue, and model scaling settings. Neither JSON document is a
tfvars input. Pool resource names and selectors come from the generic
accelerator contract, so H100, H200, B200, B300, GB300, RTX PRO, MIG, and mixed
clusters use the same path once the exact model/runtime placement is qualified.

Qualification is a strict join, not an inference. A model enters the controller
envelope only when its catalog cache has a real `platform-verified` artifact
manifest, the immutable base catalog source is qualified and license/entitlement
checked, the retained runtime projection matches its exact revision, image
digest and Service, the accelerator compatibility binding is enabled and
hardware-validated, and the renderer uses that same digest-pinned image.
Missing NIM cache manifests and declaration-only GPU candidates are never
replaced by synthetic digests. They remain on the static Terraform path and
appear with explicit failed checks in `dynamic_model_contract.ineligible_models`.
The envelope keeps the artifact revision and manifest digest as one immutable
map entry and carries `scaleToZeroQualified` independently; runtime admission
must reject a zero floor when retained elasticity evidence is false.

Initial desired revisions are submitted by a bounded in-cluster bootstrap Job
through the same authenticated `plan-preview` and `apply` endpoints as the
admin console. The Job is create-only: if a model already has a durable desired
revision with the same immutable model and tenant identity, it leaves that
revision untouched. A bootstrap request with a zero hot floor is rejected until
the retained projection explicitly qualifies elasticity; set the model hot or a
positive `min_replicas` override when only runtime qualification exists.

The ownership modes are mutually exclusive:

- `terraform`: existing serving manifests and ScaledObjects remain under
  Terraform; controller writes and bootstrap must be off.
- `released`: renderer-supported serving objects and static routes for the
  qualified controller subset are absent. Terraform retains unqualified models
  and unsupported infrastructure/security GVKs. It also retains every
  per-model shared-cache PVC so localized weights survive the cutover.
  Controller writes remain off. The completed apply emits a content-bound
  handoff receipt.
- `controller`: Terraform does not render those qualified serving identities;
  ineligible models remain statically owned. A fresh install must say
  `fresh_install = true`; an existing deployment must instead supply the exact
  receipt emitted by the prior `released` apply.

This conservative existing-cluster transition avoids double ownership but is
not yet the zero-downtime Claim protocol described below: the controller still
rejects Claim without a live UID/state/field-manager verifier. Operators must
drain before the release apply. The limitation is explicit rather than silently
stealing fields or pretending a destructive Terraform removal was adoption.
Before a dynamic desired revision exists, rollback by restoring
`workload_owner = "terraform"`, clearing bootstrap/fresh/receipt and applying.
After one exists, returning that model identity to Terraform ownership is not
automated in this release; keep it controller-owned. Before removing a selected
model from tfvars, drain and disable its durable live revision. Bootstrap is
intentionally create-only and Terraform does not become the ongoing
desired-state writer. Hard deletion remains disabled.

## Implemented baseline

The shipped source now includes:

- the versioned `ModelDeployment` CRD and a controller that renders the existing
  FS2 Deployment, Service, KEDA, queue, cache, and publication resources;
- durable desired revisions plus authenticated list, create/apply, edit, drain,
  rollback, reconcile, status, and history workflows in the admin API
  and console;
- exact-pool heterogeneous placement, fixed regular-capacity hot segments,
  independently bounded preemptible burst segments, KEDA demand scaling, and
  Kueue ResourceFlavor placement inside the Terraform-owned envelope;
- Ready-gated dynamic OpenAI and MCP publication with withdrawal while a model
  is disabled, draining, failed, or no longer Ready; and
- shared-cache localization state and separate hot, cold, cached, loading,
  draining, failed, and infrastructure-required status projections.

The current renderer deliberately preserves the existing FS2 workload shape.
The installed KServe 0.20 Standard-mode controller is not yet used as the model
renderer, and Gateway API Inference Extension, llm-d, Dynamo, and ModelMesh are
not implied by enabling dynamic models.

## Optional renderer extensions

Future renderer profiles can use KServe `LLMInferenceService` or
`InferenceService`, Gateway API Inference Extension with llm-d, or NVIDIA
Dynamo without changing the FS2 admin API. Each profile still needs live
qualification for its exact model, image, GPU topology, cache, and placement.
Kueue coverage can likewise expand from serving Pods to localization, warm-up,
benchmark, batch, and multi-node startup jobs.

## Live acceptance

Acceptance requires an authenticated operator to add a reviewed model, change
its hot floor from zero to one and back, observe a node scale from zero, see the
model become Ready and appear in MCP, roll back a revision, and drain the model
without a Terraform run. It must also prove that an attempted unknown pool or
unqualified accelerator placement is rejected without cloud mutation.

## Primary research sources

- [KServe LLMInferenceService configuration](https://kserve.github.io/website/docs/model-serving/generative-inference/llmisvc/llmisvc-configuration)
- [KServe 0.18 release: multi-node inference, llm-d, and model cache](https://kserve.github.io/website/blog/kserve-0.18-release)
- [KServe LocalModelCache](https://kserve.github.io/website/docs/model-serving/generative-inference/modelcache/localmodel)
- [Gateway API Inference Extension `InferencePool`](https://gateway-api-inference-extension.sigs.k8s.io/api-types/inferencepool)
- [llm-d architecture](https://llm-d.ai/docs/architecture)
- [Kueue and supported workload integrations](https://kueue.sigs.k8s.io/)
- [Kueue Deployment integration](https://kueue.sigs.k8s.io/docs/tasks/run/deployment/)
- [Kueue `ResourceFlavor`](https://kueue.sigs.k8s.io/docs/concepts/resource_flavor/)
- [KEDA scaling concepts](https://keda.sh/docs/2.20/concepts/)
- [LeaderWorkerSet concepts](https://lws.sigs.k8s.io/docs/concepts/)
- [NVIDIA Dynamo Kubernetes installation](https://docs.nvidia.com/dynamo/dev/kubernetes/installation/install-dynamo)
- [NVIDIA Dynamo snapshot startup](https://developer.nvidia.com/blog/nvidia-dynamo-snapshot-fast-startup-for-inference-workloads-on-kubernetes)
- [KServe ModelMesh](https://github.com/kserve/modelmesh-serving)

The research deliberately uses upstream project and vendor documentation as
the decision basis. Performance claims remain qualification targets until they
are reproduced on the selected Nebius GPU, storage, driver, and model revision.
