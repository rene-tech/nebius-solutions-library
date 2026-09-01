# Dual preemptible-cluster acceptance

This document records the 2026-08-31 live acceptance of the customer-facing
`k8s-inference` Terraform interface. It deliberately contains no credentials,
Terraform state, kubeconfig, or private environment values.

## Accepted topology

Both deployments were created from the same repository and the same
`inference-stack` facade. Their desired state differs only in private copies of
`terraform.tfvars` and secret environment values.

| Deployment | Project | Region | Accelerator pool | Local NVMe | Selected model |
| --- | --- | --- | --- | --- | --- |
| `k8s-inference-b300` | B300 acceptance project | `us-north1` | preemptible 8x B300, one-node floor and ceiling | kubelet ephemeral storage | `glm-5-2-fp8` |
| `k8s-inference-h100` | H100 acceptance project | `eu-north1` | preemptible 1x H100, zero-node floor and two-node ceiling | disabled | `qwen3-8b`, `cosmos3-nano` |

The regular CPU system pool hosts Kubernetes and platform services; only GPU
workers are preemptible. The platform database is CloudNativePG inside each
cluster, not Nebius Managed PostgreSQL.

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
      "cosmos3-nano" = "h100-1x"
      "qwen3-8b"     = "h100-1x"
    }
  }
}
```

Use `selection = "profile"` with an empty `enabled` set to deploy the complete
selected catalog profile. Model definitions live in `catalog/runtime`; a new
catalog entry can then be selected and placed without editing the Terraform
implementation. Accelerator platform, preset, capacity type, node bounds,
driver, local storage, model placement, scaling, edge, and observability are
likewise tfvars inputs.

## Endpoint contract

Terraform exposes `mcp_endpoint_url` and `admin_web_interface_url` as workload
outputs. The initial dual-cluster acceptance used `edge.mode = "internal-only"`.
The retained H100 deployment was subsequently converged through the same
Terraform interface to `edge.mode = "public"` on 2026-09-01; its outputs now
contain the allocated public IP and require no foreground process or local port
forward. The retained B300 acceptance remains internal-only.

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

The H100 public-edge acceptance created only the Terraform-owned IPv4 allocation
and worker ingress rule before updating the existing control-plane Helm release
in place. The Envoy Gateway and both HTTPRoutes reported accepted/programmed,
the staging IP certificate became Ready, `/admin/` returned HTTP 200 over HTTPS,
unauthenticated `/v1/models` returned HTTP 401, and an authenticated MCP stream
returned HTTP 200. All previous loopback proxy and port-forward processes were
then stopped. The exact address and credentials remain in the private run state,
not in this public repository.

## Live evidence

### H100 / Qwen3-8B

- The run-scoped Managed Kubernetes cluster and GPU node group IDs are retained
  in local Task Deck evidence rather than in this public export.
- One preemptible H100 80 GB node was Ready; no local disk was configured.
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

### B300 / GLM-5.2-FP8

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

The two accepted clusters remain running for user testing. The superseded
`fs2-serve-usn1` cluster was deleted after its exact identity was verified;
the separately managed source registry and artifact bucket were retained. A
second archived disposable stack was destroyed from exact delete-only plans;
its three Terraform states are empty, while the shared network, subnet, and
source registry remain. Terraform never requested a quota increase.

Final post-commit applies, no-drift plans, endpoint rechecks, and CI status are
recorded in the Agent Task Deck handoff so this source document does not need a
second provenance-changing commit after live convergence.
