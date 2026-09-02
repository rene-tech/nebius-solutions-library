# NVIDIA Dynamo ModelExpress integration

FS2 supports NVIDIA ModelExpress as an optional operator mechanism. It is not
a customer fast-start level by itself. A model remains `Off`, `L1`, `L2`,
`L3`, or `L4` according to retained benchmark evidence compatible with the
fields enumerated below; `Hot` remains observed ready state.

## Upstream decision record

The implementation is pinned to ModelExpress **v0.5.1**, upstream commit
`eb5011575dcf56327578634f93a2ec2f7b5416fd` (2026-08-20). Primary sources:

- [v0.5.1 release](https://github.com/ai-dynamo/modelexpress/releases/tag/v0.5.1)
- [ModelExpress deployment guide](https://github.com/ai-dynamo/modelexpress/blob/v0.5.1/docs/DEPLOYMENT.md)
- [architecture and fallback chain](https://github.com/ai-dynamo/modelexpress/blob/v0.5.1/docs/ARCHITECTURE.md)
- [runtime path selection](https://github.com/ai-dynamo/modelexpress/blob/v0.5.1/README.md#runtime-path-selection)
- [Kubernetes-service backend limits](https://github.com/ai-dynamo/modelexpress/blob/v0.5.1/docs/K8S_SERVICE_BACKEND.md)
- [v0.5.1 client implementation](https://github.com/ai-dynamo/modelexpress/tree/v0.5.1/modelexpress_client/python/modelexpress)
- [NVIDIA Dynamo model-caching guide](https://docs.nvidia.com/dynamo/v1.4.0/knowledge-base/kubernetes/model-loading/model-caching)
- [NVIDIA Network Operator RDMA resource configuration](https://docs.nvidia.com/networking/display/kubernetes2610/deployment-guide-kubernetes.html)

The central coordinator with the Kubernetes metadata backend is the managed
default. The decentralized `k8s-service` backend is not exposed: upstream says
it assumes a stable homogeneous pool and does not support mixed revisions or
hot-swap/refit workflows. That contradicts FS2's heterogeneous-pool and exact
revision requirements. An externally operated central coordinator can use
either Kubernetes or Redis metadata.

ModelExpress transfers post-processed GPU weights between compatible workers
with NIXL/RDMA. When that path is unavailable, upstream can try ModelStreamer,
GDS, and finally the native loader, depending on which storage inputs and
runtime features are configured. This is useful on any FS2
accelerator for which the selected runtime, CUDA/driver stack, transport and
model are compatible; FS2 does not encode a B300-only GPU allowlist. A fallback
working is not proof of P2P use or of a fast-start target.

## Runtime compatibility

| Runtime | Upstream v0.5.1 path | FS2 adapter | Notes |
| --- | --- | --- | --- |
| vLLM | `--load-format modelexpress`; native in vLLM 0.23+, client package still required | Integration-ready | The configured digest-pinned image must embed the ModelExpress client Python distribution `modelexpress==0.5.1`; FS2 injects the supported server variables and runtime arguments. No such image digest is shipped or claimed qualified by this change. |
| SGLang | `--load-format remote_instance` with the ModelExpress backend | Not yet rendered | Requires a separately qualified template/argument adapter before it can be selected. |
| TensorRT-LLM | Native MX checkpoint integration added in v0.5.1 | Not yet rendered | Requires an exact TensorRT-LLM template and protobuf/runtime qualification. |
| NVIDIA NIM / opaque vendor servers | No generic server-side acceleration | Unsupported unless rebuilt | Deploying only the ModelExpress coordinator cannot change an opaque NIM loader. Use a vendor-supported client image or a separately qualified open runtime. |

No existing model is silently opted in. Before adding a model to the map, its
digest-pinned runtime image must contain the matching ModelExpress client and
must accept the rendered vLLM arguments. FS2 rejects NIM, custom servers,
vLLM-Omni and other runtime kinds even if tfvars labels them `vllm`. The image,
artifact revision and manifest, template, GPU count, accelerator pool set,
endpoint, metadata backend, managed coordinator repository and digest, adapter
and client version are hashed into
`configDigest`. The per-pool transport mode, RDMA extended-resource name and
quantity, NIXL backend and NIC pin are also hashed, so evidence from a prior
client binding cannot qualify the current one.

ModelExpress v0.5.1 does not include the concrete accelerator class in its
native source identity. FS2 therefore fails closed when one enabled model spans
more than one accelerator class and sets the supported `MX_MODEL_REVISION`
discriminator from `configDigest`, accelerator class and effective NIXL
backend. The same model may still transfer between always-hot and preemptible
pools of the same class and backend. Different models can use different GPU
classes in one heterogeneous cluster. A single ModelExpress model cannot span,
for example, H100 and B300 until an upstream-compatible cross-architecture
identity mechanism is qualified.

## Terraform configuration

Configure only `terraform.tfvars`:

```hcl
deployment = {
  # ...normal target, pools, models and application pins...
  acceleration = {
    model_express = {
      enabled         = true
      deployment_mode = "managed"
      server_image = {
        repository = "nvcr.io/nvidia/ai-dynamo/modelexpress-server"
        digest     = "sha256:<verified-image-manifest-digest>"
      }
      cache = {
        enabled  = true
        size_gib = 100
      }
      models = {
        qwen3-8b = {
          runtime_adapter = "vllm"
          transport = {
            mode                   = "nixl-rdma"
            rdma_resource_name     = "nvidia.com/rdma_shared_device_a"
            rdma_resource_quantity = 8
            nixl_backend           = "UCX"
            nic_pin                = "auto"
          }
        }
      }
    }
  }
}
```

Managed mode installs the vendored v0.5.1 chart, cluster-scoped CRDs,
namespaced RBAC, an RWO server cache, and no client metrics scrape target. The
server runs on the CPU system pool; GPU workers run the client. Its `Recreate`
strategy avoids a rolling-update multi-attach deadlock with the RWO cache. With
`cache.enabled = false`, the same writable path is backed by an ephemeral
`emptyDir`. Set `deployment_mode = "external"`, provide an explicit
`endpoint = "host:port"`, omit `server_image`, and supply exactly one
NetworkPolicy route: either a coordinator namespace plus Pod labels, or one or
more coordinator CIDRs. Kubernetes NetworkPolicy cannot authorize an FQDN, so
external mode fails closed without that explicit route. `enabled = false`
creates no running ModelExpress resources and does not alter model manifests.

The RWO volume backs the coordinator's model-cache API; it is not mounted into
GPU workers and is not a GPU snapshot. This integration accelerates later
compatible replicas through the P2P client path. A first replica still follows
the runtime's configured storage/native path. ModelStreamer object-store URIs,
GDS mounts and snapshot mechanisms remain separate exact runtime inputs.

Transport has a per-model default and optional per-pool overrides because
heterogeneous pools can expose different device-plugin resources. The default
`transport.mode = "fallback"`
does not request an RDMA resource or add `IPC_LOCK`; it permits the upstream
loader and fallback chain but does not claim an RDMA fast path. For
`mode = "nixl-rdma"`, set the exact Kubernetes extended resource advertised by
that cluster. FS2 requests the configured quantity in both requests and limits,
adds `IPC_LOCK`, injects the Kubernetes Pod identity and NIXL/UCX tuning, and
records the complete binding in controller status. The resource name is
deliberately not derived from the GPU type: NVIDIA Network Operator lets the
other device plugins and clouds use different qualified names. `LIBFABRIC` is
available for EFA-style runtimes; `UCX` is the InfiniBand/RoCE default.

`IPC_LOCK` is required by the upstream RDMA path, but Kubernetes Pod Security
`baseline` and `restricted` policies reject containers that add it. A namespace
used for `nixl-rdma` must therefore have an explicitly reviewed admission
policy that permits this capability. `fallback` mode does not add it.

The controller adds one owned NetworkPolicy per rendered pool segment. It
selects the exact ModelExpress transfer-group label, preserves normal inference
ingress from `fs2-system`, DNS and HTTPS fallback traffic, and permits only the
bounded per-rank peer port ranges plus the explicitly configured coordinator
route. A binding change produces a new transfer group; reconciliation prunes
the stale owned policy. Disabled models retain their original policy behavior.

The upstream chart had a stale `0.3.0` default at tag v0.5.1. The vendored copy
records its source and narrow integration patches in
`charts/addons/modelexpress/UPSTREAM.md`. The
public NGC manifest digest was not retrievable anonymously during this spike,
so FS2 deliberately has no mutable or guessed default: operators must supply a
verified digest when managed mode is enabled.

Helm installs files under `crds/` only when a CRD is absent; it neither upgrades
nor deletes those cluster-scoped definitions. Before upgrading the vendored
ModelExpress version, compare and apply the pinned CRDs as an explicit platform
migration. Disabling the release leaves those definitions behind, without any
running ModelExpress workload.

## Status and observability

The controller publishes `status.fastStart.mechanisms.modelexpress` with the
requested binding, exact config digest, pool set, and whether the desired
render has converged. It exports
`fs2_model_controller_modelexpress_configured{model,pool,deployment_mode,runtime_adapter,transport_mode}`.
ModelExpress emits structured server logs, which the existing cluster log
collector ingests without a separate logging stack. The v0.5.1 server does not
expose a Prometheus endpoint, and an unmodified multi-rank client does not
provide a qualified single scrape endpoint. This integration therefore creates
neither a server `ServiceMonitor` nor a client `PodMonitor`.

Upstream v0.5.1 does not expose a stable, per-ModelDeployment record of the
selected strategy, transferred bytes, handshake phases, or fallback reason via
a qualified scrape contract. Those fields therefore stay `Unavailable` in the
admin console. The controller configuration gauge and structured coordinator
logs remain observable. Readiness or a fast pod start is never relabeled as a
P2P transfer.

For `mechanism = "modelexpress"`, benchmark receipts must additionally carry
`mechanismConfigDigest` equal to the active binding. The current evaluator
directly compares measurement basis, accelerator class, optional exact pool,
accelerator count, artifact manifest, runtime image, template digest, cache
tier, snapshot digest, expiry, and this ModelExpress digest. Evidence also
carries and groups by `compatibilityTupleDigest`, but the evaluator does not yet
independently compare every richer receipt field such as driver build, CUDA
build, runtime environment, or source commit. That remaining generic identity
work is tracked by `fs2-fast-start-exact-runtime-evidence-identity-r20260902`;
this integration does not overstate it as complete.

## Acceptance and rollback

This change intentionally does not modify the active H100 or B300 clusters.
On a disposable GPU cluster with a verified client and server image:

1. Apply with the mechanism disabled and retain the normal-loader baseline.
2. Enable one vLLM model in `fallback` mode and verify the rendered digest,
   both server variables, `--load-format modelexpress`, controller metric,
   structured server logs, and semantic inference. Confirm transfer telemetry
   remains explicitly `Unavailable` rather than inferred.
3. After verifying the cluster-advertised RDMA resource, select `nixl-rdma`
   and start a second compatible replica. Retain upstream transfer evidence plus
   FS2 capacity-to-semantic-ready timing for at least 20 failure-free attempts.
4. Repeat for every qualified accelerator class, RDMA resource and pool
   topology; keep fallback evidence separate.
5. Interrupt the donor and coordinator independently, confirm the documented
   fallback, then disable the block and verify the normal runtime render.

Rollback is a single tfvars change: set
`deployment.acceleration.model_express.enabled = false` and apply. The exact
server, controller-owned policies, client environment and load-format changes
are removed; Helm's cluster-scoped CRD definitions remain because Helm does not
upgrade or uninstall resources placed under `crds/`. Model data in an
external object store is unaffected. Terraform destroys the managed PVC
according to the deployment lifecycle, so copy anything intended for retention
before rollback.
