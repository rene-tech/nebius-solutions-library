# Post-acceptance cold-start and snapshot plan

Status: architecture spike decision, 2026-08-28. This document is planning
authority only. It does not enable a new startup mechanism, change a live
route, or claim support on untested hardware.

## Decision

Finish semantic acceptance of every canonical model first. Keep conventional
startup as the fail-closed production fallback for every exact runtime
variant. Optimize in this order:

1. Remove avoidable network and build work with immutable persistent model
   artifacts, exact compile/engine caches, and image pre-pull on intentional
   non-zero pools.
2. Benchmark artifact delivery and runtime-native fast loaders through the
   same external activation clock, including OCI image volumes and reviewed
   local-NVMe localization.
3. Offer vLLM level-1 sleep only as a resident-process latency tier. It is not
   scale-to-zero and does not survive node preemption.
4. Qualify CUDA/CRIU or Dynamo snapshots only for an exact single-GPU runtime
   tuple after the conventional path is accepted. A snapshot never inherits
   support from another model, runtime image, driver, GPU family, topology, or
   MIG profile.
5. Leave multi-GPU, MIG, GMS-plus-Snapshot, and cross-hardware restore disabled
   until their own measured campaigns pass.

The machine-readable companion
[`post-acceptance-benchmark-contract.json`](post-acceptance-benchmark-contract.json)
defines the clock, capacity states, exact tuple, cohort sizes, and render
defaults. It is deliberately marked `planning-only` and grants no placement or
hardware support authority. Its closed receipt shape is
[`post-acceptance-benchmark-receipt.schema.json`](post-acceptance-benchmark-receipt.schema.json),
and `validate_post_acceptance_receipt.py` enforces cross-field statistics,
deadline, attempt-order, capacity-mapping, and promotion invariants. This is an
extension of the existing signed FS2 evidence model, not a second route
authority.

## Current evidence boundary

The current catalog has 16 canonical models. All 16 enable only
`conventional`; snapshot, sleep/wake, and custom-runtime entries are either
gated or disabled by negative evidence. B300/SM103 is the current platform's
only hardware-observed accelerator class. That does not make every runtime
variant B300-qualified and it does not qualify any future H100, H200, B200,
GB300, or RTX PRO pool.

The current Terraform source already renders one KEDA `ScaledObject` for each
canonical GPU route, with KEDA as the sole replica writer. Older point-in-time
documents that describe an all-hot release with no `ScaledObject` are stale.
Kueue still owns asynchronous Jobs rather than these long-running model
Deployments. Re-freeze the actual replica/node state before every measured
campaign instead of inferring it from a prior retained-cluster audit.

Important retained observations are:

- The historical H100 generic CUDA/CRIU pipeline restored ProteinMPNN from both shared
  filesystem and object tiers, but its prepared Kubernetes-shaped
  T0-to-second-call p50 was about 78 seconds. A mechanism proof is not a
  latency win.
- The prior H100 sweep found one storage-matched MolMIM snapshot improvement:
  15.43163 seconds versus 24.147146 seconds for prepared conventional startup.
  It is historical H100 evidence only and cannot be promoted on B300.
- One historical Evo2 B300 restore took about 238.6 seconds in CRIU plus about
  14.6 seconds for GPU restore/unlock. It ran once, used overrides, and later
  exact-lineage attempts failed. It is not a qualified B300 path.
- The current GLM prepared-node observation is dominated by process startup,
  engine profiling, and warmup. Its TP8 topology makes current CRIU work
  multi-GPU and therefore disabled. Level-1 sleep would retain the process,
  CPU copy, node, and all claimed GPUs.
- Qwen's prepared-node observation separates image pull, Hugging Face
  localization, and runtime readiness. Its retained H100 CUDA/CRIU artifact is
  explicitly incompatible with B300. A new B300 donor would be a new lineage.
- The B300 local-disk storage campaign observed six 3.84 TB NVMe devices on one
  eight-GPU host and completed 18/18 storage/localization trials. This proves
  that exact host's storage lane, not a portable node-cache implementation.
  Nebius documents passthrough local disk as strictly ephemeral, including
  data loss on node restart.
- The current B300 MIG checkpoint task still classifies CUDA checkpoint on a
  MIG slice as unsupported upstream. MIG infrastructure support and CUDA
  checkpoint support are independent gates.
- The optional image-keeper package has seven exact-digest DaemonSets, but the
  current Terraform node-group templates do not supply their required
  `cache.fs2.nebius/image-*` labels. The package therefore proves a guarded
  render, not active image residency or a latency win.
- The generic snapshot pipeline, older storage module, BioNemo campaign, and
  node-local benchmark code live on exact historical task branches and are not
  shipped in this integrated source. Import only a reviewed minimal slice;
  never assume Task Deck evidence is deployable platform code.

Never pool these observations. They have different model, runtime, GPU,
storage, and external-clock boundaries.

## What is being optimized

The product cold-start clock is:

```text
T0 = durable activation request accepted
T1 = first unretried response accepted by the model's semantic oracle (call 1)
```

T0-to-call-1 is the product latency metric. The existing signed FS2
`qualification-cohort/v4` contract remains the promotion base and requires
T0-to-call-2. A successful attempt therefore needs two distinct semantic
responses; optimizing call 1 cannot hide a failed or stalled call 2.

Every attempt records the following events, or records a typed
`not-applicable` value:

```text
activation accepted
  -> capacity requested -> provider instance created -> Node Ready
  -> queue admission -> Pod scheduled -> storage attached
  -> image / ImageVolume pull start and end
  -> artifact localization start and verified end
  -> runtime process start -> weight load start and end
  -> engine build / PTX / framework compile start and end
  -> checkpoint restore start and end
  -> readiness accepted -> semantic call 1 accepted (T1)
  -> distinct semantic call 2 accepted
  -> return-to-zero accepted
```

Retries are new attempts. Donor creation, checkpoint creation, cache
population, prefetch, and compilation performed before T0 are still measured
as preparation cost and bytes. They may be amortized in a separate report, but
must not disappear from the product result.

The same mechanism must be measured separately in these capacity states:

| Capacity state | Node | Pod | Node-local data | Classification |
| --- | --- | --- | --- | --- |
| ready-Pod warm | present | present | maybe | warm reference, not cold start |
| resident sleep | present | present | maybe | resident latency tier, not cold start |
| prepared-node zero-Pod | present | absent | allowed and declared | prepared cold start |
| fresh-node zero-Pod | absent | absent | absent | true elastic cold start |
| preemption replacement | replacement required | absent | assumed lost | preemptible recovery |
| durable-cache-loss fallback | absent | absent | absent | conventional disaster fallback |

Reporting one state as another is a failed experiment.

The promotion mapping is explicit: `prepared-node-zero-pod` extends the
existing `prepared-node` cohort; `fresh-node-zero-pod` extends `new-node`;
`preemption-replacement` requires the same `new-node` base plus the signed
preemption receipt. Warm, resident-sleep, and cache-loss cells are supplemental
and cannot substitute for either signed base cohort.

## Mechanism review

| Mechanism | Practical role | B300 decision now | Heterogeneous decision |
| --- | --- | --- | --- |
| NIM Operator `NIMCache` | Persist NIM profiles on network storage and reuse them across Pod starts. | Adopt as the sole NIM artifact owner; do not let the FS2 localizer write NIM paths. | Requalify each NIM profile/runtime/GPU binding. Profile discovery is not semantic qualification. |
| NIM Operator `NIMBuild` | Prebuild a buildable TensorRT-LLM engine before serving. | Pilot only for an exact NIM/version/profile that exposes a buildable profile; bind engine output to the full ABI tuple. | Engines are GPU/runtime artifacts, not portable model weights. Build separately per accepted tuple. |
| Content-addressed SFS/provider-block PVC | Durable exact weights and tokenizer/preprocessor artifacts. | Adopt now through the existing acquisition receipts. Keep Qwen's protected Retain writer handoff separate. | Portable weights still require an independently qualified runtime variant on each GPU class. |
| Image keeper DaemonSet | Keep exact runtime layers on an already-existing, explicitly labeled node. | Useful only for an intentional hot/prepared pool. It does not create a scale-from-zero node. | Render per pool and image digest; never label every heterogeneous burst pool. |
| Local NVMe localization | Copy verified immutable artifacts from durable storage to fast ephemeral disk. | Pilot after the reviewed local PV/PVC controller exists. Raw formatting and `hostPath` remain forbidden. | Capability belongs to the typed pool, not a GPU name. Preemption/restart always exercises durable fallback. |
| KServe `LocalModelCache` | v1alpha1 controller-managed per-node model download and cache for KServe `InferenceService`; v0.20 publishes separate CRD/resources Helm charts and Kustomize. | Use the existing KServe cache pilot; do not install it as a second writer for current direct Deployments. | Opt-in lane only. Qualify eviction, node identity, credentials, scale-from-zero, and preemption for each pool. |
| OCI modelcar (`oci://`) | Model data in an OCI sidecar, exposed through a shared process namespace. | Benchmark only; it changes process-namespace and image-pull behavior. | Useful packaging option, not a universal speed guarantee. Bind the OCI digest and runtime semantics. |
| Native OCI ImageVolume (`oci+native://`) | Read-only OCI object mounted by kubelet without a modelcar sidecar. | The current FS2 Kubernetes 1.35 contract exposes this as beta, enabled by default, and requires a compatible container runtime such as containerd 2.1+. Pilot only after API-server, kubelet, runtime, credential, and digest-reporting preflight. Pull/unpack latency remains inside T0. | Kubernetes documents `image` volumes as stable only in v1.36. Never project that maturity backward onto the current 1.35 cluster. |
| vLLM native loaders | Safetensors strategies, sharded state, InstantTensor, Run:ai streamer, Tensorizer, or another exact loader plugin. | Compare only formats supported by the pinned FS2 runtime image. Record conversion time/bytes and semantic parity. | A loader result is specific to model format, runtime build, storage, GPU count, CPU/RAM, and topology. |
| vLLM level-1 sleep | Offload weights to CPU RAM and discard KV cache while retaining the server. | Experimental, non-production resident-tier research only. Online control requires vLLM development mode, whose security guidance says it must never be enabled in production and includes dangerous endpoints beyond sleep/wake. A private Service is not a production boundary. | It can support distributed workloads upstream, but every exact topology still needs a campaign. It does not release the Pod's Kubernetes GPU claim. |
| vLLM level-2 sleep | Discard weights and KV cache, then reload weights on wake. | Treat as a controlled reload experiment, not a snapshot. Compare against conventional restart. | Exact runtime-only; no portability claim. |
| CUDA checkpoint plus CRIU | Move CUDA state to host memory, checkpoint Linux process state, then restore. | Exact single-GPU B300 experiment only. Current catalog gates remain closed until a new donor and n>=20 promotion cohort pass. | CUDA permits GPU remapping only to a GPU with enough memory and the same chip type. FS2 remains stricter: exact GPU, driver, topology, runtime, and artifact tuple by default. |
| Kubelet Checkpoint API | Ask the CRI implementation to checkpoint a container. | Do not adopt for GPU fast start. It does not capture NVIDIA GPU state by itself and checkpoint memory may contain secrets. | CRI/runtime compatibility and secure artifact custody are separate prerequisites even for CPU-only debugging. |
| NVIDIA Dynamo Snapshot | Operator/Helm-managed CRIU and `cuda-checkpoint` flow using a privileged snapshot agent and `DynamoCheckpoint`. | Isolated amd64 single-GPU vLLM/SGLang pilot after model acceptance; compare with the existing pipeline. Require driver 580.xx+, CUDA 13 for B300, a qualified RWX or sequential RWO checkpoint lane, and a chart runtime socket matching containerd or CRI-O. Do not install the all-in-one stack into the generic retained platform. | Upstream calls the feature preview; TensorRT-LLM is limited to an experimental single-GPU aggregated text worker, multi-GPU needs 590.xx+ and remains disabled here, multinode is work in progress, and specialized workers are excluded. GB300/arm64 cannot inherit this amd64 prerequisite receipt. |
| Dynamo GPU Memory Service | Keep weights owned by a GPU memory service for same-node engine recovery. | Experimental resident lane only. It is not preemption or node-failure recovery. | Requires its own DRA and backend qualification. Current upstream guidance says not to combine GMS with Snapshot. |
| Dynamo KV offload/KVBM | Preserve/reuse request KV blocks across GPU, host, SSD, or remote tiers. | Useful later for TTFT and long-session efficiency, but exclude it from model cold-start winner selection. | It does not localize or restore model weights; benchmark under a separate request-cache program. |

### Artifact hierarchy

Keep artifact types separate because their compatibility and lifecycle differ:

1. **Source artifacts:** exact weights, tokenizer, preprocessors, configs, and
   licenses. Prefer immutable revisions and per-file/content manifests.
2. **Delivery artifacts:** OCI model images/ImageVolumes, Hugging Face cache
   trees, NIMCache profiles, and content-addressed PVC/SFS copies.
3. **Generated artifacts:** TensorRT engines, Triton/Torch/vLLM compile caches,
   PTX/cubin products, and loader-specific sharded formats. Key these by the
   full runtime and accelerator ABI.
4. **CPU/process checkpoints:** CRIU pages, namespaces, files, cgroups, sockets,
   and process metadata. Treat them as sensitive memory artifacts.
5. **GPU checkpoints/resident state:** CUDA process state or GMS-owned weights.
   These are the narrowest and least portable layer.
6. **Request state:** KV-cache blocks. This affects repeated-prefix/request
   latency, not first model load.

Safetensors is a safe, fast source format and vLLM exposes several faster load
strategies, but neither fact makes a particular storage/runtime combination
faster. Benchmark the exact option; do not convert proprietary NIM caches or
weights without entitlement and runtime support.

There is no generic portable Kubernetes GPU-memory snapshot in this design.
The CUDA checkpoint API moves a process's GPU memory contents into host memory
and requires a CPU-side checkpoint system; restore remains hardware- and
runtime-constrained. GMS instead retains weight ownership in GPU memory and is
a same-node resident service, not a durable image. For either approach,
artifact/resident bytes and effective transfer bandwidth create a physical
lower bound that must be measured. A faster CUDA toggle does not erase CRIU
pages, storage localization, node provisioning, or semantic readiness.

## Hardware qualification policy

| Accelerator target | Current status | Allowed statement |
| --- | --- | --- |
| B300 | hardware-observed current profile; SM103 receipts exist | Conventional is the only enabled startup mechanism. Local NVMe is storage-observed on one eight-GPU host. Snapshot/sleep/custom lanes remain gated or negative per exact runtime variant. |
| H100 | declared future pool plus historical task evidence | Historical results may select experiments; they do not enable a current render. |
| H200 | declared future pool plus historical task evidence | Same rule as H100; matching compute capability is not enough for artifact reuse. |
| B200 | declared future pool only | No model, loader, engine, or snapshot support claim until live exact qualification. |
| GB300 | declared future grouped-pool target only | No model or snapshot support claim. Discover CPU architecture, GPU/DRA/ComputeDomain topology, runtime images, and storage live. |
| RTX PRO 6000 | declared future pool only | No model or snapshot support claim. Qualify PCIe topology, memory, driver, runtime, and exact model variants independently. |

Each candidate must produce a signed or hash-bound receipt containing at least:

- exact model, weight, tokenizer/preprocessor, semantic-oracle, and semantic
  request-contract digests;
- exact runtime variant, source/provenance identity digest, image digest,
  command/arguments, environment contract, execution identity, and
  loader/engine format;
- host CPU architecture, GPU vendor/product/chip/compute capability/memory,
  GPU count, topology inventory digest, MIG mode/profile, typed accelerator-pool
  receipt, and allocated GPU identity;
- exact driver, CUDA, kernel, container runtime, CRIU, checkpoint utility, and
  compile-cache ABI;
- artifact manifest/content digests/bytes, storage class/mode, hash-bound
  node/PVC identity, and capacity state;
- every phase timestamp, semantic result, failure, cleanup, and return-to-zero
  result.

Any absent or changed field denies artifact reuse and falls back to
conventional startup. GPU UUID belongs in bounded receipts, not Prometheus
labels.

The closed receipt schema rejects unknown fields, missing identities, duplicate
attempt IDs or receipt digests, incorrect capacity-to-cohort mappings, and
aggregate statistics that do not recompute from the raw attempts. The
executable validator reopens those invariants; schema validation alone is not
promotion authority.

## Staged benchmark program

### Stage 0: instrumentation and controls

- Require the all-model acceptance receipt before a route enters this program.
- Capture conventional n>=3 exploratory cells for every capacity state.
- Emit OpenTelemetry spans for the event sequence and Prometheus histograms for
  phase durations. Correlate queue/Kubernetes/provider events and DCGM GPU
  utilization/memory without secret or high-cardinality identity labels.
- Verify first-call and second-call semantics independently. Lazy compile on
  the first request stays inside the first-response result.
- Pre-register a positive attempt deadline. A timeout is a failed attempt, not
  a censored latency sample.

Exit: complete clocks, zero silent retries, semantic oracle passes, cleanup and
return-to-zero receipts pass.

### Stage 1: durable caches and prebuild

- NIM variants: compare uncached NIM, ready `NIMCache`, and eligible exact
  `NIMBuild` output.
- Independent/Hugging Face variants: compare durable content-addressed SFS or
  provider-block weights plus exact compile caches.
- Keep cache population/prebuild duration, GPU hours, bytes, and retained
  storage outside T0 but in the amortization report.
- Destroy/recreate the Pod and replace/preempt the node to prove ownership and
  durability.

Exit: no second writer, exact digest verification, restart/preemption success,
and conventional fallback after cache loss.

### Stage 2: delivery and loader challengers

- Run the existing KServe LocalModel/OCI pilot on one accepted single-GPU LLM.
- Compare storage initializer/PVC, modelcar, native ImageVolume, SFS, and
  reviewed NVMe localization with the same weights and capacity state.
- For the exact vLLM image, test only supported loader formats. Include format
  conversion in preparation cost and immutable artifact custody.
- Run n>=3 alternating candidate/control attempts before choosing any promotion
  candidate. Promotion requires strict control/candidate alternation and at
  least 20 raw attempts per arm.

Exit: candidate meets the preregistered absolute and relative p95 T0-to-call-1
effects, T0-to-call-2 does not regress, and semantic/failure rates, fresh-node,
and preemption cells pass.

### Stage 3: resident sleep/wake

- Start with an accepted Qwen exact runtime variant on a dedicated minimum
  capacity pool. Run level-1 sleep/wake, conventional process restart, and
  ready-Pod warm controls.
- Measure sleep time, wake time, host RAM retained, GPU memory released, GPU
  allocation still claimed, first/second response, and controller failure.
- Keep the development endpoints loopback-only in an isolated experiment. Do
  not create a Service for the vLLM development listener or classify this lane
  as production-capable; upstream also exposes dangerous non-sleep development
  operations on that listener.
- Test idle timeout, concurrent activation, failed wake, forced Pod restart,
  node preemption, and conventional fallback.

Exit: experimental resident-tier evidence only. This stage can never qualify a
production route or zero-hot-node behavior.

### Stage 4: exact single-GPU snapshot

- Start only after Stages 0-2 and the exact B300 route are accepted.
- Preflight amd64, driver 580.xx or newer, B300 CUDA 13, the exact CRI socket,
  and qualified RWX or sequential RWO checkpoint storage before installing the
  isolated Dynamo slice.
- Use Qwen or another accepted single-GPU runtime with a deterministic oracle;
  do not use TP8 GLM, MIG, multimodal, diffusion, or a NIM with a negative donor
  until its own blocker is resolved.
- Run the current FS2 CUDA/CRIU pipeline and Dynamo Snapshot as independent
  candidates. Pin chart/tool/image source and render digests.
- Measure donor startup/warmup, checkpoint time and bytes, publication,
  localization, CRIU restore, CUDA restore, readiness, T1, cleanup, and artifact
  deletion/retention.
- Exercise same node, replacement same-class node, preemption during restore,
  corrupt/truncated artifact, OOM, tuple mismatch, network/socket mismatch, and
  conventional fallback.

Exit: n>=20 attempts per arm in strict control/candidate alternation for the
winning exact tuple, all failures ranked after successful durations, registered
call-1 effect thresholds met, no call-2/semantic/failure regression, and a
successful replacement-node cell. Otherwise keep the mechanism disabled.

### Stage 5: heterogeneous and distributed qualification

- Repeat Stages 0-4 independently for each hardware/runtime variant that has
  capacity. Do not copy a B300 receipt to another pool.
- Multi-GPU begins only after the driver-610 task proves the exact process tree,
  NCCL quiesce/reinit, dump, restore, and two semantic responses.
- MIG begins only after the separate MIG task records vendor support and an
  exact raw/model-level pass. Infrastructure support alone is insufficient.
- GMS, DRA, GB300 ComputeDomains, and multi-node paths remain independent
  experiments; no compound stack is promoted from a single component test.

## Promotion gate

A mechanism may become a rendering candidate only when all of these are true:

1. The exact model route and runtime variant are already accepted.
2. The candidate and conventional control use the same capacity and storage
   state, alternate attempt order, and have at least 20 attempts per cell.
3. The positive deadline, minimum absolute p95 improvement, and minimum relative
   p95 improvement are registered before the first attempt. Both effect
   thresholds must pass.
4. Raw attempt receipts are retained. Nearest-rank percentiles rank every failed
   or timed-out attempt after all successful durations, p95 is withheld below
   n=20, and median absolute deviation is the declared dispersion statistic.
5. Attempted/passed/failed counts, failure-ranked p50/p95, median absolute
   deviation, bytes, memory, and every phase recompute from the raw receipts;
   preparation cost is disclosed.
6. Candidate p95 T0-to-call-1 meets both registered effects, p95
   T0-to-call-2 does not regress, and semantic and failure rates do not regress.
7. Fresh-node, preemption replacement, cache-loss fallback, corrupt artifact,
   tuple mismatch, and return-to-zero tests pass.
8. The exact artifact and compatibility receipt is immutable and available;
   secrets and in-memory request content are absent.
9. The conventional fallback still works with the candidate controller/chart
   unavailable.
10. An independent reviewer verifies evidence custody, hashes, denominator,
   Terraform plan, rendered Helm/Kustomize output, and cleanup.

Snapshot production promotion has an additional deferred acceptance gate owned
by `fs2-snapshot-artifact-custody-hardening`. That follow-up must define and
prove a credentialless pre-traffic donor, namespace/tenant isolation,
least-privilege RBAC and the privileged-agent exception, encrypted checkpoint
storage, ResourceQuota/PVC byte limits, default-deny network policy, retention
TTL, and deletion receipts. This deferred implementation does not block offline
architecture work or an isolated non-production experiment; it does block
snapshot production promotion.

## Terraform and Helm realization

Do not add GPU SKU conditionals to model manifests. Add an optional
`cold_start_profiles` map keyed by exact runtime variant and reference a typed
accelerator pool ID:

```hcl
cold_start_profiles = {
  qwen3_8b_vllm_exact = {
    runtime_variant              = "qwen3-8b/vllm/<image-digest>/<weights-digest>"
    accelerator_pool_id          = "qualified-pool-id"
    mechanism                    = "conventional"
    qualification_receipt_sha256 = "<receipt-sha256>"
    enabled                      = false
  }
}
```

The real contract must use full digests rather than the abbreviated example.
Terraform rejects an enabled profile when the pool, runtime variant, mechanism,
or qualification receipt is disabled, missing, or mismatched.

Ownership remains split:

- **Infrastructure state:** typed node pools, min/max/zero floor, capacity type,
  local-disk capability, durable storage, and stable scheduling labels.
- **Foundation state:** exactly one owner for optional CRDs/controllers,
  pinned Helm chart version, values, rendered manifest digest, and upgrade
  policy. Dynamo Snapshot, KServe LocalModel, and DRA remain off by default.
- **Workload state:** exact model/runtime binding, PVC/cache CRs, optional
  checkpoint CRs, Jobs, Services, and receipt references. Secret values are
  external references only.

Each optional component needs fresh-create, second-plan no-op, supported
upgrade, rollback, and destroy acceptance. CRDs have one explicit owner and
must not be duplicated between Helm and Terraform resources. Render and lint
charts without a cluster, then use a server-side dry run before any isolated
pilot. No optional chart may install another GPU driver, device plugin,
scheduler, autoscaler, or observability stack implicitly.

Autoscaling semantics remain explicit:

- `min_nodes = 0` exercises fresh-node and preemption paths; no keeper or local
  cache is assumed.
- `min_nodes > 0` may opt into image keepers and NVMe localization, with cache
  capacity/eviction monitoring.
- a resident sleep profile requires a live Pod/node and reports its claimed GPU
  separately from GPU memory released;
- queue admission, Pod replicas, and node-group size each retain one owner.

## Operational metrics

Use the platform's OpenTelemetry, Prometheus, and DCGM path rather than adding
a snapshot-specific telemetry stack. Required low-cardinality dimensions are
model ID, runtime variant ID, mechanism, accelerator class, capacity state,
preemptible/on-demand, outcome, and phase. Exact Pod/node/GPU/PVC/artifact
identities stay in hash-bound evidence receipts and logs with bounded
retention.

Alert on activation deadline, queue wait, provider/node readiness, image and
artifact pull, storage attach, localization verification, engine build,
restore, semantic failure, GPU memory/usage, imagefs/local-disk pressure, cache
eviction, repeated fallback, preemption recovery, and failure to return to
zero.

## Work ownership and follow-ups

Reuse the existing task boundaries:

- `fs2-serve-blackwell-cold-start-challengers`: canonical model matrix and
  external-clock evidence.
- `fs2-gaie-kserve-cache-pilot`: KServe LocalModelCache, modelcar, and native
  OCI delivery comparison.
- `fs2-storage-tiers` and `fs2-node-local-bench`: storage/localization and
  Kubernetes-versus-node-local evidence.
- `fs2-driver610-criu`: single-/multi-GPU driver and CRIU qualification.
- `fs2-mig-cuda-checkpoint-spike`: MIG infrastructure and unsupported checkpoint
  experiment.
- `fs2-snapshot-pipeline`: retained generic CUDA/CRIU implementation and
  evidence contract.

Create separate post-acceptance pilots for the two uncovered decisions:

- an integrated full-catalog v2 evidence packet that refreshes exact current
  release/runtime identities and extends the existing four-model clock to all
  16 routes before mechanism trials;
- an isolated Dynamo Snapshot single-GPU B300 comparison without the all-in-one
  platform bundle.

The existing Blackwell challenger retains ownership of its bounded Qwen/GLM
sleep/wake cells; do not create a competing sleep controller. Neither new pilot
starts before its exact route is accepted.

## Primary sources

- NVIDIA CUDA checkpoint API, including Linux restriction and same-chip restore
  rule: <https://docs.nvidia.com/cuda/cuda-driver-api/group__CUDA__CHECKPOINT.html>
- NVIDIA `cuda-checkpoint` functionality and limitations:
  <https://github.com/NVIDIA/cuda-checkpoint>
- CRIU CUDA plugin implementation:
  <https://github.com/checkpoint-restore/criu/blob/criu-dev/plugins/cuda/cuda_plugin.c>
- Kubernetes Kubelet Checkpoint API and memory-artifact security warning:
  <https://kubernetes.io/docs/reference/node/kubelet-checkpoint-api/>
- Kubernetes v1.35 OCI `image` volume beta semantics:
  <https://v1-35.docs.kubernetes.io/docs/concepts/storage/volumes/#image>
- Kubernetes v1.35 release note, including the compatible container-runtime
  prerequisite: <https://v1-35.docs.kubernetes.io/blog/2025/12/17/kubernetes-v1-35-release/>
- Current Kubernetes OCI `image` volume semantics, stable from v1.36:
  <https://kubernetes.io/docs/concepts/storage/volumes/#image>
- KServe v0.20 release, including native OCI ImageVolume support:
  <https://kserve.github.io/website/blog/kserve-0.20-release>
- KServe OCI modelcars:
  <https://kserve.github.io/website/docs/model-serving/storage/providers/oci>
- KServe LocalModel installation:
  <https://kserve.github.io/website/docs/install/localmodel-install>
- KServe LocalModelCache:
  <https://kserve.github.io/website/docs/model-serving/generative-inference/modelcache/localmodel>
- vLLM sleep mode: <https://docs.vllm.ai/en/latest/features/sleep_mode/>
- vLLM load formats and Safetensors strategies:
  <https://docs.vllm.ai/en/latest/api/vllm/config/load/>
- Hugging Face cache layout and immutable-revision behavior:
  <https://huggingface.co/docs/huggingface_hub/guides/manage-cache>
- Safetensors format: <https://huggingface.co/docs/safetensors/index>
- NVIDIA NIM Operator caching:
  <https://docs.nvidia.com/nim-operator/latest/cache.html>
- NVIDIA NIM engine build/cache:
  <https://docs.nvidia.com/nim-operator/latest/nim-build.html>
- NVIDIA Dynamo Snapshot prerequisites, Helm flow, topology matrix, and
  limitations:
  <https://docs.nvidia.com/dynamo/dev/kubernetes/operations/cold-start-optimizations/dynamo-snapshot>
- NVIDIA Dynamo compatibility matrix:
  <https://docs.nvidia.com/dynamo/dev/reference/compatibility>
- NVIDIA Dynamo KV-cache offload:
  <https://docs.nvidia.com/dynamo/dev/kubernetes/kv-cache-offloading/overview>
- NVIDIA Dynamo KVBM scope:
  <https://github.com/ai-dynamo/dynamo/blob/main/docs/components/kvbm/README.md>
- NVIDIA Dynamo GMS/shadow-engine limitations:
  <https://docs.nvidia.com/dynamo/kubernetes/fault-tolerance/shadow-engine-failover>
- Nebius Managed Kubernetes local-disk Terraform contract:
  <https://docs.nebius.com/terraform-provider/reference/resources/mk8s_v1_node_group>
