# Live deployment acceptance history

This document records the dated live acceptance of the customer-facing
`k8s-inference` Terraform interface. The original 2026-08-31 exercise used two
preemptible GPU deployments. The retained H100 deployment was subsequently
extended with fixed capacity-block nodes, shared model storage, public HTTPS,
and dynamic-model qualification. Historical measurements remain labeled with
their original topology. The document contains no credentials, Terraform
state, kubeconfig, or private environment values.

## Current retained H100 topology (2026-09-05)

The retained `k8s-inference-h100` deployment is in `eu-north1`. Its GPU
capacity is heterogeneous by capacity policy while keeping one accelerator
class, and it now carries two CPU-only planes for scientific batch work:

| Pool | Capacity | Node bounds | GPUs per node | Local NVMe |
| --- | --- | --- | --- | --- |
| `h100-reserved-8x` | regular, strict capacity block | fixed at 2 | 8x H100 80 GB | disabled |
| `h100-1x` | preemptible | 0..2 | 1x H100 80 GB | disabled |
| `batch-cpu` | regular, elastic CPU (`cpu-d3` 8 vCPU / 32 GB) | 0..2 | none | none |
| reference-data CPU pool | regular (`cpu-d3` 32 vCPU / 128 GB) | fixed at 2 | none | none |

The `batch-cpu` pool hosts scientific configure, preprocessing and aggregation
stages and scales from zero; the reference-data pool hosts the AlphaFold 3
data pipeline against the retained 2 TiB reference filesystem. Both GPU pools
mount the shared model cache and the reference-data filesystem. The staged
scientific batch controller, JobSet 0.12.0, Kueue 0.17.8 with the
`general-cpu`, `inference-accelerators` and `reference-data-cpu` cluster
queues, and the dedicated scientific artifact bucket are all Terraform-owned
from the same `terraform.tfvars`. Section
[Scientific fleet acceptance](#scientific-fleet-acceptance-2026-09-05) records
the ten qualified scientific profiles; the serving-model material below is
unchanged.

Both selected models, `qwen3-8b` and `cosmos3-nano`, carry the same durable
`placement.poolRefs = ["h100-1x", "h100-reserved-8x"]` desired revision. That is
the server-authoritative create default of every compatible pool, so each model
receives durable reserved placement and preemptible burst wherever the envelope
allows it. Qwen has a one-replica hot floor and Cosmos a zero-replica floor;
each has a ceiling of two. Qwen's hot replica is admitted on the reserved
capacity-block pool `h100-reserved-8x`; its second, autoscaled replica bursts
onto the preemptible pool `h100-1x`. Cosmos, at a zero hot floor, has only a
preemptible burst segment, so activating it from zero provisions an `h100-1x`
node. A regular CPU system pool hosts Kubernetes and platform services. The
platform database is CloudNativePG inside the cluster, not Nebius Managed
PostgreSQL.

The active placement therefore drives the observed cold-start cost, and an
operator controls it live. Cosmos activating from zero onto a fresh preemptible
node measured 444.020 seconds end to end, dominated by preemptible node
provisioning and a fresh 9.19 GB runtime image pull. The same model revision
activating on the already-running reserved pool, where the image is cached and
weights are on the shared filesystem, reached Ready in 91.169 seconds in the
[historical shared-cache elasticity receipt](#historical-h100-shared-cache-elasticity-receipt-2026-09-02).
Narrowing a model's `poolRefs` to `h100-reserved-8x` through the admin console
(live model policy, no Terraform) trades the preemptible cost saving for that
faster warm-node activation; widening it again restores cheap preemptible burst.
Because placement is runtime material, the controller applies such a change as a
cold cutover: the model is drained to zero, re-placed while cold, and re-enabled.
That whole cutover is live and reversible inside the existing Terraform-owned
capacity envelope, and never requires a Terraform run.

## Scientific fleet acceptance (2026-09-05)

Ten cancer-immunotherapy scientific profiles are live on the retained H100
deployment as qualified batch models: `alphafold3`, `bindcraft`, `boltzgen`,
`esmfold2`, `esmfold2-fast`, `mosaic`, `openfold3-openbind`,
`proteina-complexa`, `protenix-v2` and `rfdiffusion`. Each is submitted
through the public `POST /v1/models/{model_id}:submit` route or the MCP
`submit_scientific_run` tool and returns one terminal, semantically validated
result document; [Scientific batch API quick start](docs/SCIENTIFIC_BATCH_API.md)
describes the customer contract. AlphaFold 3 and BindCraft are licensed
academic profiles and are visible only to the academic tenant's scientific
token; the general serving token discovers the other eight.

### Final live-surface acceptance

Exact source `87d3aacc039c02c6e9ac239ad0be693fa39ffef1` (tree
`f2157fdce45232de15ebd1135c4e599982173a37`) was deployed through the
`inference-stack` Terraform wrapper as control-plane OCI index
`sha256:6df5ba12ca40d86dd1bea9717ce8bc19de8393596a757595cd41bfa84a0156d0`.
The registry image carries those exact revision and tree labels. The
value-suppressed live-surface receipt (SHA-256
`84820d86addd4869d0c52275b48e6ed746d53cfff84998f8e6e8b17252f25da4`) passed
every check: owner-only access bundle with distinct admin, inference and
scientific credentials; TLS 1.3 under normal trust; admin, `/readyz`, Grafana,
Alertmanager and Tempo all HTTP 200; gateway, model controller, admin console
and the two-node GPU observer on the exact digests; three active cluster
queues, seven local queues, four resource flavors and the five scientific
priority classes; all ten scientific profiles `qualified` with nine
observability launches enabled; and both MCP catalogs scoped per token with
private zero-TTL discovery. The committed
[`acceptance/live-surface`](acceptance/live-surface/README.md) runner
reproduces that acceptance from the outside and additionally proves the
OpenAI catalog, the HTTP scientific discovery route and one real chat
completion.

### Immutable ten-model cold-start campaign

The final campaign ran three complete fleet repetitions at parallelism eight
against the public endpoint with
`acceptance/scientific-fleet/run_coldstart_benchmark.py`, bound to the
reviewed H100 environment qualification set (SHA-256
`9ee09fb7351a437ee60d00567da8cb6e35a66ae80bfebe340793ad579b36c8c5`, pools
`h100-reserved-8x` and `h100-1x`). Result: 30 of 30 attempts succeeded with a
passed semantic validation, 30 of 30 carried an exact, reconciled
`application_observed` GPU lifecycle with no data gaps, every attempt was
admitted on `h100-reserved-8x`, and the desired model state was byte-identical
before and after (snapshot SHA-256
`77ae8653645081b5fe80c0619132ca894481a3f54f770607fe35fe67470a625e`). The
canonical receipt SHA-256 is
`42b429f33d0f16cea08bb9689e08c70808c2fb3da734d570f3cd4819c3a45d59` and its
validation seal SHA-256 is
`eda49ee72898ef5e26bed2b3e4d8da571d7f6fb6fbf16f38c798ec0a5d50ad74`; both
remain in operator custody because they name operation and workload
identities.

Medians over the three repetitions, in seconds, as measured by the public
operation clock and the scientific controller events:

| Model | Capacity wait | Image pull | Artifact localization | Active compute | Accepted to validated result | Scheduler-occupied GPU s | Active GPU s | Occupied-idle GPU s |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| `alphafold3` | 1.9 | 2.5 | 48.3 | 35.7 | 99.3 | 61 | 34 | 27 |
| `bindcraft` | 1.5 | 3.2 | 47.2 | 395.3 | 456.9 | 415 | 389 | 26 |
| `boltzgen` | 5.0 | 10.3 | 159.9 | 683.8 | 899.0 | 769 | 641 | 124 |
| `esmfold2` | 24.7 | 3.0 | 70.4 | 33.0 | 135.2 | 81 | 29 | 52 |
| `esmfold2-fast` | 1.6 | 3.0 | 69.6 | 38.8 | 114.9 | 80 | 27 | 53 |
| `mosaic` | 2.5 | 7.1 | 67.4 | 77.4 | 341.8 | 105 | 78 | 27 |
| `openfold3-openbind` | 28.3 | 2.6 | 344.6 | 50.2 | 434.2 | 74 | 48 | 25 |
| `proteina-complexa` | 3.2 | 5.2 | 232.1 | 222.7 | 481.0 | 199 | 147 | 52 |
| `protenix-v2` | 1.2 | 2.9 | 51.3 | 86.5 | 151.0 | 111 | 84 | 27 |
| `rfdiffusion` | 1.3 | 3.2 | 231.1 | 61.2 | 309.8 | 84 | 58 | 25 |

Dispersion is preserved in the receipt rather than averaged away. The first
repetition of six models paid a much larger artifact localization than the
second and third (ESMFold2 409.7 s then 70.4 and 68.5 s; ESMFold2-Fast 219.8
then 68.0 and 69.6 s; Mosaic 383.3, 67.4 and 42.7 s; OpenFold3 487.6, 344.6
and 42.8 s; Proteina-Complexa 523.8, 116.9 and 232.1 s; RFdiffusion 287.2,
231.1 and 156.8 s), so a first request after a fresh publication is
materially slower than steady state. Queue wait was under 10 ms for every
attempt, and the elastic CPU stages were the only capacity waits above a few
seconds (ESMFold2 and OpenFold3 waited 24.7 and 28.3 s for a `batch-cpu` node
to join). The dedicated API cold-start scalar and a runtime/model-load boundary
remain truthfully unavailable in the public operation contract; the receipt
carries measured localization, scheduler, active/idle GPU and first
semantic-result clocks instead, and no attempt was joined to an exact
fast-start cache tier (`not-observed`).

Benchmark implications for the next optimization round, none of which changes
the accepted contract: artifact localization dominates every model's
wall-clock outside active compute and is repeated per attempt even on the
shared-filesystem cache tier; BoltzGen's twenty-design campaign and BindCraft's
one-design trajectory are compute-bound and would not benefit from a cache;
occupied-idle GPU seconds (25 to 124 s per attempt) are the localization and
teardown windows in which a GPU is held but not computing.

### Known limitations after this acceptance

- Public and MCP model listings now report the accelerator class of the pool a
  dynamic model is admitted on; Prometheus `gpu_class` labels for those two
  serving models still carry the canonical catalog class from their original
  qualification.
- The Kueue pod webhook occasionally denies the first Pod of the per-minute
  control-plane maintenance Job with `Job.batch ... not found`; the Job
  controller retries within the same minute and every maintenance Job
  completes, so this is event noise rather than a missed run.
- A second Nebius-managed `cilium-operator` replica stays Pending on the
  single-node system pool because its host ports are already bound; this is
  provider add-on scheduling and does not affect the platform.
- Leftover qualification Jobs from other review-state tasks remain in
  `fs2-models` (RFdiffusion `r13`, Mosaic `v6`, one Proteina forward pass)
  until their owning tasks close; they hold no GPU.

## Historical 2026-08-31 topology

Both original deployments were created from the same repository and the same
`inference-stack` facade. Their desired state differed only in private copies
of `terraform.tfvars` and secret environment values.

| Deployment | Region | Accelerator pool | Local NVMe | Selected model |
| --- | --- | --- | --- | --- |
| `k8s-inference-b300` | `us-north1` | preemptible 8x B300, one-node floor and ceiling | kubelet ephemeral storage | `glm-5-2-fp8` |
| `k8s-inference-h100` | `eu-north1` | preemptible 1x H100, zero-node floor and two-node ceiling | disabled | `qwen3-8b`, `cosmos3-nano` |

## Configuration boundary

Model selection is controlled by the following block in the customer file:

```hcl
deployment = {
  profiles = {
    models = "full_catalog"
  }

  models = {
    selection = "explicit"
    enabled   = ["cosmos3-nano", "qwen3-8b"]

    pool_overrides = {
      "cosmos3-nano" = "h100-reserved-8x"
      "qwen3-8b"     = "h100-reserved-8x"
    }

    scaling = {
      mode = "keda"
      hot  = ["qwen3-8b"]
      overrides = {
        "cosmos3-nano" = { min_replicas = 0, max_replicas = 2 }
        "qwen3-8b"     = { min_replicas = 1, max_replicas = 2 }
      }
    }
  }

  dynamic_models = {
    enabled                  = true
    writes_enabled           = true
    workload_owner           = "controller"
    bootstrap_model_ids      = ["cosmos3-nano", "qwen3-8b"]
    fresh_install            = false
    handoff_receipt          = "sha256:33e3e2a0431e72abec993066b8d49530ad29a553d047846d4bd897bc07c58620"
    fast_start_evidence_file = "/private/run/fast-start-evidence.json"
  }
}
```

That receipt belongs to this dated retained configuration. Another deployment
uses the exact `dynamic_model_handoff_receipt` emitted by its own successful
`released` apply.

Use `selection = "profile"` with an empty `enabled` set to deploy the complete
selected catalog profile. Model definitions live in `catalog/runtime`; a new
catalog entry can then be selected and placed without editing the Terraform
implementation. Accelerator platform, preset, capacity type, node bounds,
driver, local storage, model placement, scaling, edge, and observability are
likewise tfvars inputs.

## Endpoint contract

Terraform exposes `mcp_endpoint_url`, `admin_web_interface_url`,
`inference_base_url`, and `grafana_url` as workload outputs. The explicit
`inference-stack output` command returns those endpoints together with the
admin bootstrap token, the scoped MCP/inference token, Grafana credentials,
and the kubeconfig command. The initial dual-cluster acceptance used
`edge.mode = "internal-only"`. The retained H100 deployment was converged
through the same Terraform interface to `edge.mode = "public"`; its outputs
contain the allocated public IP and require no foreground process or local port
forward. This handoff does not claim a current B300 endpoint.

For an internal-only deployment, the operator starts its foreground transport
with:

```bash
./inference-stack proxy --var-file /private/path/terraform.tfvars \
  --run-root /private/path/run
```

The proxy reads only named non-secret Terraform outputs, creates the two
Kubernetes port-forwards described by the Terraform contract, and terminates
them on shutdown. A remote tester must tunnel the operator-proxy port over SSH,
or deploy `edge.mode = "public"`.

The 2026-09-01 public-edge exercise first proved routing with an explicitly
selected staging certificate; that historical certificate was not a
browser-trusted release result. The retained H100 edge now uses production
IP-ACME. A verified TLS request to `/admin/` returned HTTP 200 on 2026-09-02,
and no local proxy or port-forward process is needed. The exact address and
credentials are emitted from the private run state rather than copied into this
public document.

## Live evidence

### Historical H100 runtime exercise (2026-08-31 through 2026-09-01)

- The run-scoped Managed Kubernetes cluster and GPU node group IDs are retained
  in local Task Deck evidence rather than in this public export.
- During this exercise, one preemptible H100 80 GB node was Ready; no local disk
  was configured. This is not the current capacity-block topology described
  above.
- The first scheduled-to-Ready path took 23 minutes 57 seconds while recovering
  from an initial stale volume attachment and a cross-region image pull.
- Once the vLLM container started, model Ready took 1 minute 48 seconds. The
  runtime reported 2.67 seconds to load weights, 29.69 seconds to compile, and
  about 5 seconds for CUDA graph capture.
- An authenticated OpenAI-compatible chat request returned the exact requested
  marker `H100_EDGE_READY`; catalog discovery and MCP `tools/list` also passed
  through the Terraform-owned edge.
- A 256-token direct-runtime streaming sample measured 0.3181 seconds to first
  token, 155.35 generated tokens/second after first token, and 130.22
  tokens/second end to end. The phase-1 control plane currently rejects
  `stream=true`, so this is runtime evidence rather than an edge-streaming SLO;
  the admin measurement correctly remains unavailable until gateway streaming
  and token instrumentation are enabled.
- Adding `cosmos3-nano` through the same private `terraform.tfvars` scaled the
  preemptible H100 pool from one to two nodes without replacing the cluster,
  Qwen Deployment, or Qwen cache PVC. The new Pod triggered autoscaling at
  18:24:53 UTC, scheduled at 18:27:00, started its containers at 18:30:10, and
  became Ready at 18:51:07 with zero restarts.
- The first Cosmos cold start was acquisition-bound: the 9.19 GB runtime image
  pulled in 2 minutes 41.532 seconds and the public 34,986,890,561-byte model
  populated a new 64 GiB persistent cache before CUDA initialization. Once the
  cache was complete, seven weight shards loaded in 5.17 seconds; the model
  runner initialized in 10.633 seconds using 30.237 GiB, and vLLM-Omni's dummy
  warm-up pipeline took 6.99 seconds. Scheduled-to-Ready was 24 minutes 7
  seconds; a recreated Pod can reuse the retained cache and should not be
  represented by that first-population baseline.
- Two distinct 448x256, 25-frame, eight-step video requests passed the pinned
  semantic validator. The direct adapter request reported 894 ms total and
  produced a 141,224-byte MP4; the authenticated MCP request completed in 3.44
  seconds end to end and produced a distinct 406,976-byte MP4. Both carried the
  pinned model revision, correct frame/fps metadata, valid ISO BMFF structure,
  and matching payload SHA-256 values. MCP exposes the qualified runtime as
  `cosmos3_nano_generate_media_native`.

### Historical H100 shared-cache elasticity receipt (2026-09-02)

The retained qualification receipt is
[`h100-qwen-cosmos-elasticity-qualification-20260902.json`](catalog/profiles/evidence/h100-qwen-cosmos-elasticity-qualification-20260902.json),
with SHA-256
`1cd246c27c5a4c4cc639a189c5b5fc33a8fcd7080f6b621f4bd1bc2c9d5401a6`.
It binds the exact model revisions, artifact and runtime digests,
`nvidia-h100-sxm5-80gb`, the `h100-reserved-8x` placement, KEDA/HPA
transitions, cache outcome, semantic results, and restored zero floor.

| Model | Cache localization | Activation to Ready | Warm semantic completion | Completion to zero floor |
| --- | ---: | ---: | ---: | ---: |
| `cosmos3-nano` | cache hit, 0.027 s | 91.169 s | 10.366 s | 36.908 s |
| `qwen3-8b` | cache hit, 0.009 s | 134.26 s | 0.749 s | 39.191 s |

Both runs started and ended with zero replicas and endpoints and completed two
distinct semantic calls. The artifact retains `qualification` in its historical
filename, but these single attempts are functional evidence rather than a
qualified customer fast-start level under the current 20-success contract.
They do not claim a CUDA/GPU snapshot restore result, and they do not transfer
unchanged to another accelerator, runtime image, model revision, cache tier, or
pool.

### H100 fast-start exploratory campaigns (2026-09-02)

The current evidence collector binds the converged `ModelDeployment` revision,
owned Deployment/Service/ScaledObject identities and generations, immutable
runtime and artifact digests, exact H100/driver/CUDA tuple, capacity state,
request contract, and semantic validator. Kubernetes observations are polled,
so the reported boundary is conservative to the observation cadence rather
than a sub-second profiler trace.

| Model | Capacity state and pool | Result | Model-start p50 / p95 | First-request semantic p50 / p95 | Observed / qualified |
| --- | --- | ---: | ---: | ---: | --- |
| `qwen3-8b` | prepared-node process-cold, `h100-reserved-8x` | 3/3 PASS | 113.444 / 124.422 s | 114.263 / 125.437 s | `L1` / `Off` |
| `cosmos3-nano` | prepared-node zero-Pod, `h100-1x` | 3/3 PASS | 67.821 / 68.929 s | 82.771 / 84.561 s | `L2` / `Off` |

The Qwen aggregate SHA-256 is
`4f6616afd09b012e9c903be8883d55a06e4d25bea479df0f83019f0317f75ef9`;
its internal receipt digest is
`10682c8ff68ed5f4f3df3547cdfea2f98b29436e7cd40123962ac85c72b9645e`.
The Cosmos aggregate SHA-256 is
`c49a226982720159d375813d6abeef153ae16dd9d314e506d2c3306ffb96bf4f`;
its internal receipt digest is
`7e6ea1f17345449246abd1cd0b23e1cb331af3a7426b9aa1e0d710afd8f71957`.
Both are complete and failure-free exact tuples, but each has only three
successful attempts. The live controller therefore exposes the percentiles as
exploratory evidence with `InsufficientBenchmarkSamples` and retains
`qualifiedLevel=Off`.

The strict Cosmos probe decoded and validated the native MP4 envelope on every
cold attempt. Generation goodput p50/p95 was 1.672/1.688 frames/s. A separate
two-call acceptance retained the same Ready backend for its second call: the
warm request completed in 12.551 seconds for the checked-in 25-frame contract,
or 1.992 contract-derived frames/s. That legacy warm harness validates a
non-empty native result; the cold campaign supplies the stronger MP4 oracle.
Qwen's independent 90-request direct-runtime streaming baseline measured
62.937-62.940 output tokens/s and 15.685-16.907 ms p95 TTFT across three
repetitions.

Failures were not removed. Earlier campaigns retain the original Cosmos 403,
Qwen operation-identity mismatch, the fresh-node Cosmos attribution failure
after 158.041 seconds of capacity wait and 282.700 seconds of model start, and
a Pod/EndpointSlice read-skew failure. The last two led to collector-only fixes
at `7ab796d4` and `2948e204`, followed by wholly new campaign identities. The
fresh-node attempt also recorded a 162-second pull for the 9.19 GB runtime
image, demonstrating that a GPU snapshot cannot by itself satisfy a level when
the runtime image is absent.

The compact evidence projection SHA-256 is
`2be316631b89f63bd5a1eff2e3c803ebfcd6a8c93def0c8d14300b415a33f1c3`.
Terraform revision 23 mounted it through an immutable model-controller envelope
without changing nodes, model specifications, storage, routes, or credentials;
a subsequent plan was empty. The admin workflow then changed only Cosmos
`fastStart` from Fixed `L2` to Automatic `Off` through `L4` with
`AllowLowerLevel`. Generation 5 reconciled without changing artifact, runtime,
placement, scaling, cache, queue, exposure, or lifecycle fields. With no
20-sample qualified path, the policy correctly assigned `Off` and reported
`MissingDataMinimum`; Cosmos stayed Cold at floor 0. Final live floors remained
Qwen 1, Cosmos 0.

### Historical B300 / GLM-5.2-FP8 exercise (2026-08-31)

- The run-scoped Managed Kubernetes cluster and GPU node group IDs are retained
  in local Task Deck evidence rather than in this public export.
- One preemptible 8x B300 node was Ready with local NVMe exposed as kubelet
  ephemeral storage.
- The pinned model source is `zai-org/GLM-5.2-FP8` revision
  `ba978f7d347eaf65d22f1a86833408afdb953541`.
- The initial 1 TiB model-cache PVC could not be provisioned while the retired
  predecessor cluster still consumed tenant network-disk capacity. No quota or
  limit was changed. Deleting that exact predecessor released its disks; the
  new PVC then bound and localization began.
- The Nebius scale-from-zero synthetic node currently advertises boot-disk
  ephemeral capacity but not the future host-local NVMe. It therefore rejects
  GLM's 768 GiB localization request before creating a zero-hot node. The live
  acceptance uses a tfvars-only one-node floor. Supporting zero-hot for this
  shape requires a provider autoscaler correction or an activator design; the
  workload reservation was not weakened.
- A retained, unattached predecessor disk was independently verified to contain
  147 of the 150 pinned model files; the three missing metadata/license files
  total only 13,656 bytes. It is a useful read-only recovery source, but the
  current public schema cannot select an existing CSI disk or object-store URI.
  A future typed per-model artifact override should carry the non-secret source
  identifier, revision/content digest, expected inventory, and Secret reference
  while keeping credentials out of tfvars and state. The historical CUDA/CRIU
  object upload is partial and is not a valid weight source.
- The first durable-cache population completed without restart: the pinned Hub
  download took about 27 minutes 24 seconds, followed by about 30 minutes 34
  seconds to copy 755.66 GB from the network PVC to local NVMe. That copy
  sustained about 412 MB/s; it measures the durable-storage path, not the local
  disks' ceiling.
- vLLM then reached Kubernetes Ready in 15 minutes 55 seconds. A live loader
  warning showed that automatic safetensors prefetch was disabled for the EXT4
  local cache. A bounded 16-worker read-only prefetch consumed all 141
  safetensor shards (755,632,050,320 bytes) in 23.886 seconds at 31.636 GB/s
  and accelerated the remaining load. Because it began during the run, the
  reported 472.16-second weight-load phase is a mixed baseline and must not be
  relabeled as optimized cold start. Post-load profile, KV-cache creation, and
  first DeepGEMM/FlashInfer warmup took 336.20 seconds. Persisting the proven
  prefetch and compatibility-keyed JIT caches remains the main cold-start
  integration gap; this acceptance does not claim the earlier 90-second goal.
- An authenticated edge request returned the exact marker
  `GLM52_FS2_FINAL_OK` with zero reasoning tokens. A separate 256-token direct
  vLLM streaming sample measured 0.736 seconds TTFT, 5.728 generated
  tokens/second, 5.657 end-to-end tokens/second, and 45.253 seconds total. It is
  one exploratory runtime sample, not a gateway or production percentile.

## Retention and cleanup

The H100 cluster is the retained deployment for user testing. The B300 material
above is historical acceptance evidence; the B300 deployment was skipped for
this handoff and this document publishes no current B300 endpoint. The
superseded `fs2-serve-usn1` cluster was deleted after its exact identity was
verified, while its separately managed source registry and artifact bucket
were retained. A second archived disposable stack was destroyed from exact
delete-only plans; its three Terraform states are empty, while its shared
network, subnet, and source registry remain.

Final post-commit applies, no-drift plans, endpoint rechecks, and CI status are
recorded in the Agent Task Deck handoff so this source document does not need a
second provenance-changing commit after live convergence.
