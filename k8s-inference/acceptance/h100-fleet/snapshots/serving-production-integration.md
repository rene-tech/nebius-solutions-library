# Optional serving snapshots: H100 qualification and integration

Normal loading remains the default. These are real CUDA+CRIU restores into
fresh Pods after deleting the donor, not weight caches or retained live GPUs.
Both models passed three matched normal/restore pairs and two previously
unseen inputs per trial. Images, model revisions, precision, context/output
bounds and original generation arguments are unchanged. Both sides use the
same standard-asyncio / legacy TCPStore compatibility variant.

| Model | Normal container→ready, median | Restore container→ready, median (range) | Normal / restore Pod-create→ready, median |
| --- | ---: | ---: | ---: |
| Qwen3-8B | 99.801s | 51.493s (45.442–64.496s) | 106.671s / 59.170s |
| Cosmos3-Nano | 59.020s | 28.082s (27.824–49.224s) | 64.906s / 36.343s |

These pairs retain existing images, localized weights and shared-filesystem
caches. They do **not** measure node acquisition, first image pull, controlled
disk-cold reads or guaranteed RAM residency. HTTP polling includes transport
and observation delay; container timestamps have one-second resolution.
Request-specific compilation is not implicitly counted as warmed readiness.
The separate both-full-outputs clock includes two sequential requests and
validation; it is not first-output latency.

Machine-readable reports:
[Qwen](qwen3-8b-h100-20260907.json) and
[Cosmos](cosmos3-nano-h100-20260907.json).
Each binds runtime/GPU/driver/kernel identities, all six timings, input/output
hashes and publication verification. The first separate Cosmos RWO-remount
restore took330.335s; that observed storage-read penalty is not hidden by the
shared-cache results.

## Existing configuration contract

Configure `deployment.dynamic_models.gpu_snapshots` in `terraform.tfvars`.
`bundle_files` accepts paths relative to `k8s-inference`, for example
`["acceptance/h100-fleet/snapshots/qwen3-8b-bundle.json"]`, or absolute files.
This avoids copying generated compatibility metadata into customer settings.
`bundles` is keyed by each entry's `bundle_id`; `cache` has `claim_name`,
`storage_class_name` and `size_gib`. Terraform creates the RWX claim and exact
source ConfigMaps. Set `cache.manage_claim = false` to reuse a claim already
owned by `scientific_batch.gpu_snapshots` or another stack; do not import the
same PVC under two Terraform addresses. `adopt_existing = true` adopts retained
qualification ConfigMaps and, when managed here, the existing cache claim.
Adoption preserves the checkpoint bytes; it does not hydrate an empty cache.
An unpopulated/incompatible bundle with `Prefer` falls back to normal loading.

Put the full [Qwen bundle](qwen3-8b-bundle.json) or
[Cosmos bundle](cosmos3-nano-bundle.json) object under its existing infrastructure
envelope qualification's `gpuSnapshotBundles[bundle_id]` map. Terraform owns
this registry, the shared PVC and the exact ConfigMaps. No separate serving
request API is introduced. To select an option, use the existing
ModelDeployment cache fields:

```json
{
  "tier": "SharedFilesystem",
  "snapshotPreference": "Prefer",
  "snapshotRef": {
    "name": "qwen3-8b-h100-cuda-criu-20260907-r12",
    "digest": "sha256:980908af74a2fda33647d819daf556a300eafb652e97467538d14e84ffa81ae2",
    "strategy": "CudaCheckpoint"
  }
}
```

`Prefer` falls back to the original model command when preparation or
CUDA/CRIU restore fails. `Require` does not silently choose normal loading.
`Never` with no snapshotRef produces the unchanged normal template. Do not
combine this qualified path with a second loader mechanism or ModelExpress;
that combination has not been measured.

In the admin console, open **Live model deployments**, select the existing
deployment, and choose **Fast start → Model startup path**. The qualified bundle
selector fills these digest-bound fields; **Snapshot fallback** selects Prefer
or Require. The normal default is unchanged. Choosing a snapshot disables
competing automatic/mechanism selection and leaves pools, GPU counts and replica
bounds unchanged. Select compatible pools first if a bundle choice is disabled.
Normal loading clears the snapshot reference. A retired or incompatible stored
choice remains visible until the operator explicitly replaces it. Use the
existing validate, preview and apply flow; selecting a draft alone never changes
the cluster. The model inventory links to this control and keeps measured clocks
and cache conditions separate from target-level qualification.

The capabilities API publishes `gpu_snapshot_choices` only when the installed
envelope and renderer accept the exact bundle/runtime/pool tuple. The inventory
receives that same installed registry from `ControllerFiles`; its selectable
status additionally requires the deployed runtime identity to match the actual
qualification receipt. Future model IDs can register their independently
qualified serving bundles without adding a model-specific API or UI branch.

Validation binds the exact model revision, artifact manifest, runtime image,
original command, one-GPU placement, qualified GPU class and snapshot receipt.
The H100 bundle does not authorize B300 or other untested GPUs. Actual driver,
kernel, GPU and frozen source compatibility are checked inside every Pod;
configured availability does not mean that every invocation used a restore.

Snapshot Pods must not inherit `fsGroup`: kubelet can recursively alter the
shared checkpoint's captured file modes. The renderers retain that group as
`supplementalGroups` instead. An optional `worker_log` entry holds its original
manifest checksum/mode/owner. Only the verified per-Pod log copy is normalized;
the frozen source and shared bundle are never changed. A missing or changed log
uses the existing fallback decision. See the
[production Protenix correction](protenix-production-metadata-fix-20260907.json)
for the reproduced failure and real restored scientific-output proof.

## Filesystem and process integration

`serving_snapshot.py` applies the tested bridge to the existing Pod spec.
The controller still owns scheduling, replica counts, GPU resources, Services
and autoscaling. Only the main runtime startup changes; the Cosmos bounded
JSON adapter and both models' public inference contracts remain intact.

- Mount the immutable captured bundle read-only; use per-Pod writable scratch
  for logs, generated kernels, runtime cache and `/tmp`.
- Preserve the seven exact source files in `fs2-fleet-snapshot-serving-v7`.
  The separately mounted `serving_entrypoint.py` is outside that captured
  source directory, so integrating readiness/fallback does not alter the
  source bytes referenced by a captured process.
- Retain sparse named shared-memory backing files with their captured bytes,
  permissions and ownership. The publication verifier reads and compares every
  file before correcting cross-filesystem creation-mode differences.
- Restore the donor's private socket address only on the fresh Pod's private
  loopback interface, using the bounded NET_ADMIN init helper. No host network,
  host address, CNI or cluster-wide network changes are made.
- Retain original startup/readiness timing thresholds. The runtime probe
  requires a completed CUDA restore (or normal-fallback selection) **and**
  ordinary application HTTP health; an early restored CPU API cannot become
  Kubernetes Ready while CUDA state is still restoring.

The pinned tools image and source/entrypoint/network/address ConfigMap names,
source digests and shared PVC subpaths are in each bundle object. Existing
Protenix snapshot sources and its separate bundle remain unchanged.

## Operator inventory

`build_model_inventory(..., serving_snapshot_bundles=...)` accepts the flattened
typed envelope registry serialized as ordinary JSON objects. It joins that
registry with the current deployed image, model revision, GPU class/count and
the packaged capability receipt. A registry without a matching deployed
identity cannot advertise a selectable snapshot. Other-GPU measurements are
suppressed rather than presented as that GPU's expected startup time.

The scientific `snapshot_bundles` argument and Protenix matching are unchanged.
Root integration owns the shared Terraform/API/admin rollout and its live
selection check; isolated qualification is not presented as that deployment.

## Production-transform live acceptance

[Exact-transform acceptance](serving-production-transform-h100-20260907.json)
passed with the original serving templates and new readiness/fallback bridge:
Qwen restored in50.448s after container start and answered both unseen inputs.
Cosmos restored across nodes onto a newly autoscaled preemptible H100 in34.784s
after container start and produced both complete new-seed videos. The latter
took414.784s from Pod creation:380s was node/image/init acquisition, including
a173.231s first pull of the9.19GB runtime image. Snapshot startup does not remove
that image dependency.

An intentional missing snapshot on a second new preemptible node selected
ordinary loading and passed both full video outputs. Runtime readiness took
61.201s after container start; the complete node/image/init path was461.201s.
The exact readiness exec succeeded in all three probes. All test GPU Pods and
CPU transfer holders were removed afterward; the immutable bundles remain.
