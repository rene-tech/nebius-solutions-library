# Dynamic model configuration

## Decision

Model lifecycle belongs in a Kubernetes-native control plane, not in Terraform.
Terraform should continue to own the infrastructure boundary: the cluster, GPU
node-pool envelopes, storage, registry, database, Gateway, operators, CRDs,
observability, and platform identities. Operators should be able to add, remove,
scale, warm, drain, and expose a model without running a Terraform plan, provided
the change fits inside an already-provisioned pool and policy envelope.

The recommended target is an FS2 `ModelDeployment` API and reconciler. The admin
API writes that custom resource, the reconciler renders the most suitable
upstream serving resource, and Kubernetes status remains the live source of
truth. PostgreSQL retains the append-only audit trail, approvals, idempotency
keys, and configuration revisions; it must not become a second scheduler.

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
| Kueue | Admission, quotas, fairness, and preemption for cache-fill, warm-up, benchmark, batch, and multi-node jobs | It is not the replica controller for a long-running serving Deployment |
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

A minimal resource should look like this; exact API names remain an
implementation detail until the CRD spike is accepted.

```yaml
apiVersion: inference.fs2.nebius.ai/v1alpha1
kind: ModelDeployment
metadata:
  name: qwen3-8b
  namespace: fs2-models
spec:
  artifact:
    modelRef: qwen3-8b
    revision: sha256:reviewed-model-manifest-digest
  runtime:
    profile: vllm
    image: cr.example/model-runtime@sha256:reviewed-image-digest
  placement:
    poolRefs: [h100-preemptible]
    acceleratorsPerReplica: 1
  availability:
    minReplicas: 0
    maxReplicas: 4
    idleSeconds: 900
    cachePolicy: node-local
  queue:
    localQueue: interactive
    priorityClass: standard
  exposure:
    openAI: true
    mcp: true
  rollout:
    strategy: rolling
```

The live configuration surface should expose these controls:

- enabled/draining state and immutable artifact/runtime revisions;
- zero or more hot replicas, maximum replicas, idle timeout, and optional
  scheduled warm windows;
- target request latency or queue depth, concurrency, and rollout strategy;
- one or more compatible existing pool references and accelerator count;
- cache tier and snapshot preference, with qualification status shown
  separately from intent;
- queue, priority, tenant visibility, OpenAI route, and MCP exposure;
- per-model request, token, concurrency, and GPU-time policy references.

`minReplicas` is the number of GPU replicas that stay hot. Setting it to zero
permits true cold operation. A cached-but-not-hot status means weights are on
the requested cache tier but no GPU replica is serving. The autoscaler may raise
replicas to `maxReplicas`; the cloud node scaler then supplies nodes up to the
Terraform-defined pool maximum. Operators must see the phases separately:
admitted, node pending, artifact localizing, runtime starting, warming, ready,
draining, and failed.

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

## Delivery sequence

1. Add the versioned `ModelDeployment` CRD, status conditions, admission rules,
   and a controller that initially renders the existing FS2 Deployment/KEDA
   resources. Adopt each current object with an explicit Terraform state removal,
   server-side-apply field-manager handoff, owner-reference change, and no-diff
   live check so Terraform and the controller are never concurrent writers.
2. Put the existing configuration plan/reconcile/rollback API over the CRD and
   add UI controls for hot floor, ceiling, idle policy, placement, cache, and
   exposure. Preserve PostgreSQL audit and role checks.
3. Reuse and qualify the KServe 0.20 CRD/resources already installed by the
   foundation, then add Gateway API Inference Extension and llm-d as optional
   Terraform-installed capabilities. Implement KServe renderers for qualified
   LLM and custom-runtime profiles, with conformance tests against the stable
   FS2 API.
4. Add llm-d and Dynamo renderer profiles only after live qualification on the
   target GPU/model matrix. Keep the current renderer as a supported fallback.
5. Make Kueue admission explicit for localization, warm-up, benchmark, batch,
   and multi-node startup jobs; verify that interactive request queueing remains
   visible and bounded while a zero-hot model activates.

Acceptance requires an authenticated operator to add a reviewed model, change
its hot floor from zero to one and back, observe a node scale from zero, see the
model become Ready and appear in MCP, roll back a revision, and remove the model
without a Terraform run. It must also prove that an attempted unknown pool or
unqualified accelerator placement is rejected without cloud mutation.

## Primary research sources

- [KServe LLMInferenceService configuration](https://kserve.github.io/website/docs/model-serving/generative-inference/llmisvc/llmisvc-configuration)
- [KServe 0.18 release: multi-node inference, llm-d, and model cache](https://kserve.github.io/website/blog/kserve-0.18-release)
- [KServe LocalModelCache](https://kserve.github.io/website/docs/model-serving/generative-inference/modelcache/localmodel)
- [Gateway API Inference Extension `InferencePool`](https://gateway-api-inference-extension.sigs.k8s.io/api-types/inferencepool)
- [llm-d architecture](https://llm-d.ai/docs/architecture)
- [Kueue and supported workload integrations](https://kueue.sigs.k8s.io/)
- [Kueue `ResourceFlavor`](https://kueue.sigs.k8s.io/docs/concepts/resource_flavor/)
- [KEDA scaling concepts](https://keda.sh/docs/2.20/concepts/)
- [LeaderWorkerSet concepts](https://lws.sigs.k8s.io/docs/concepts/)
- [NVIDIA Dynamo Kubernetes installation](https://docs.nvidia.com/dynamo/dev/kubernetes/installation/install-dynamo)
- [NVIDIA Dynamo snapshot startup](https://developer.nvidia.com/blog/nvidia-dynamo-snapshot-fast-startup-for-inference-workloads-on-kubernetes)
- [KServe ModelMesh](https://github.com/kserve/modelmesh-serving)

The research deliberately uses upstream project and vendor documentation as
the decision basis. Performance claims remain qualification targets until they
are reproduced on the selected Nebius GPU, storage, driver, and model revision.
