# FS2 cold-start cache quick wins

The customer performance-class measurement and promotion boundary is defined
in [`FAST_START_BENCHMARK.md`](FAST_START_BENCHMARK.md). It keeps GPU capacity
wait separate from model startup and supports both token and media workloads.

This optional Kustomize package keeps selected exact, already-qualified runtime
images resident on explicitly opted-in B300 nodes. The retained direct-model
manifests also put Hugging Face downloads and CUDA/Triton/Torch/vLLM compile
artifacts on their existing model PVCs. Runtime cache directories are keyed by
the complete runtime image digest, exact model-content identity, pinned
580.173.02 host driver, and SM103 target, so an image, model, driver, or GPU
change cannot silently reuse incompatible generated code. A driver rollout
must change `fs2.nebius/compile-cache-abi` and the path segment together.

After every canonical route has passed semantic acceptance, use the
[post-acceptance cold-start and snapshot plan](POST_ACCEPTANCE_COLD_START_PLAN.md)
and its machine-readable benchmark contract before enabling another mechanism.
The current production fallback remains conventional startup; the planning
contract grants no hardware or placement support.
Benchmark receipts must validate against the closed
[`post-acceptance-benchmark-receipt.schema.json`](post-acceptance-benchmark-receipt.schema.json)
shape and pass `validate_post_acceptance_receipt.py`; they extend the existing
signed T0-to-call-2 qualification receipts and are not route authority by
themselves.

The executable follow-on is documented in
[`COLD_START_OPTIMIZATION_IMPLEMENTATION.md`](COLD_START_OPTIMIZATION_IMPLEMENTATION.md).
Its machine matrix covers all 16 routes, its raw phase observations retain
missing measurements, and its separate Terraform root sequences the exact
zero-to-Ready-to-call-1-to-call-2-to-zero attempts without putting tokens or
semantic payloads in Terraform state. The same matrix partitions Deployment
identity into thirteen exact complete tuples and three explicitly blocked
unresolved model-content digests, and denies runtime log markers for every
route except the reviewed Evo source-instrumented observation path.

Use the [live KEDA elasticity acceptance](LIVE_ELASTICITY_ACCEPTANCE.md) for
the shorter model-qualification gate. It records scale-to-zero, request
admission, KEDA/HPA activation, Pod/Node/model readiness, semantic completion,
shared-cache outcome, return to zero, and bounded failure cleanup in linked
private and identity-hashed public receipts.

## DCGM cadence for the conventional baseline

The standard Terraform profile leaves `enable_dcgm_cold_start_campaign=false`:
DCGM collection and Prometheus scraping remain at 30 seconds, the scrape
timeout remains 25 seconds, and no metric-relabel narrowing is installed.
That profile has a conservative 60-second minimum nominal proxy window
because a Prometheus scrape reads DCGM Exporter's latest cached value; its
timestamp is not a hardware-source timestamp.

The r28g baseline lifecycle must not toggle this setting outside Terraform.
After semantic acceptance and before the first baseline attempt, save and
review a `transition-dcgm-campaign-on` plan with
`enable_dcgm_cold_start_campaign=true`, validate it with
`tests/verify_plan.py` in `transition-dcgm-campaign-on` mode with
`--enable-dcgm-cold-start-campaign`, and apply that exact saved plan. The
campaign profile updates only `DCGM_FI_DEV_GPU_UTIL` and
`DCGM_FI_DEV_FB_USED` every second and scrapes every second; all other exporter
fields retain their 30-second collection cadence. Its temporary metric relabel
keeps only those two DCGM series, so normal temperature, power, XID, and other
DCGM series are intentionally absent from Prometheus during the measurement
window. This reduces the 30x scrape-ingestion increase but is not a normal
observability profile.

A Prometheus sample becomes eligible as a nominal scrape proxy only at
`T0 + collection interval + scrape interval`. The campaign's minimum nominal
window is therefore two seconds. DCGM Exporter drops the DCGM hardware-source
timestamp before Prometheus stores the sample, so even a later scrape can
repeat a stalled pre-T0 cache value. Receipts consequently remain classified
`NOMINAL_SCRAPE_PROXY` with
`UNOBSERVED_HARDWARE_SOURCE_TIMESTAMP` and the explicit
`DCGM_SOURCE_TIMESTAMP_UNOBSERVED` instrumentation gap. They must never be
reported as exact post-T0 hardware attribution.

The collector derives both intervals from a canonical, private cadence binding
that joins the reviewed saved-plan hash and exact Terraform output to the
observed ConfigMap, ServiceMonitor, and converged exporter DaemonSet identities.
It rejects caller-supplied cadence, identity mismatch, attempts shorter than
the nominal floor, and missing in-window proxy samples without extending the
attempt or estimating GPU use. After the final baseline attempt, save, review,
verify, and apply the inverse `transition-dcgm-campaign-off` plan with the
campaign flag absent, then require an exact standard-profile no-op plan. This
restores 30-second collection/scraping and removes the temporary metric
narrowing before ordinary operation or teardown. Exact hardware timestamps
require a later direct-DCGM instrumentation task.

This package does not create GPU nodes. Kubernetes DaemonSet Pods are ignored
for scale-from-zero decisions by the node autoscaler. Each DaemonSet owns one
image and also requires an image-specific `cache.fs2.nebius/image-*` node
label, so installing the package alone schedules nothing. Put only the desired
digest-prefix labels in the managed node-group template for an intentional
warm/minimum-capacity pool; do not label a generic burst pool for every image.
Keeping the small shell container running pins that image against ordinary
image garbage collection. Budget node image-disk space and alert on
image-filesystem pressure before enabling any label.

## Weight acquisition and localization

The canonical acquisition plan remains
`catalog/runtime/contracts/artifact-acquisition.json`; this directory does not add a
second downloader or cache writer. Public Hugging Face models are acquired by
the signed, suspended `render_artifact_acquisition_job` and published
atomically under:

```text
/mnt/fs2-serve-cache/models/{model_id}/sha256/{content_digest}
```

The shared prerequisite is the existing `fs2-models/fs2-cache` RWX PVC. The
existing `render_localization_job` may copy an admitted content digest to:

```text
/var/lib/fs2-serve/cache/models/{model_id}/sha256/{content_digest}
```

only after a reviewed local-PV/PVC lifecycle binds the exact node, PVC UID, and
activation generation. Raw local-disk formatting, `hostPath`, and an
unreviewed node-local directory remain forbidden.

The historical Qwen3-8B provider-block acquisition and qualification cohort
remains distinct evidence and is not relabeled. The portable Qwen3-8B and
Cosmos3-Nano Deployment templates now use a conventional inline localizer that
is valid on both their checked-in RWO PVCs and the deployment renderer's shared
RWX PVC. It serializes the first writer, verifies the exact artifact once,
publishes the payload and deterministic receipt atomically, and mounts the
result read-only in the runtime. Later replicas perform only receipt plus
path/type/size checks, so they do not redownload or rehash model weights. The
full protocol is documented in
[`../general-media/SHARED_CACHE_FAST_START.md`](../general-media/SHARED_CACHE_FAST_START.md).

This path does not claim local-NVMe weight placement, a persistent compiler
cache, or GPU-process snapshot restore. The cold-start matrix still records no
new timing result until the rendered shared-filesystem path is benchmarked and
its replacement-node behavior produces retained evidence.

NIM cache ownership is unchanged. NIM artifacts stay under the NIM Operator's
`NIMCache`; the FS2 localizer must never write those paths.

## Rollout prerequisites

Before applying this optional package:

1. Verify every referenced image index and recorded `linux/amd64` leaf digest
   is readable through the managed node service account, and that `/bin/sh`
   can run as the manifest's non-root UID. The retained cluster uses node-level
   registry authorization; this package deliberately does not reference the
   absent `fs2-runtime-registry` imagePullSecret.
2. Verify `fs2-models` exists. This package contains no credentials or
   pull-secret values.
3. Verify enough image-filesystem space, including unpack transients, exists
   for each selected image. Keep only the current and rollback generations and
   remove stale keepers after a controlled rollout.
4. For weight pre-staging, keep the model runtime at zero, run only the
   catalog-rendered suspended acquisition Job after its admissions pass, and
   verify the exact signed receipt before runtime activation.
5. Roll the direct download-on-start Deployments one at a time. Their existing
   RWO PVC identities and Hugging Face cache locations must remain unchanged.
   Keep `replicas: 1` and `strategy: Recreate`; a second Pod cannot safely
   share these RWO write caches.

The opt-in node labels use the first 32 hexadecimal characters of the full
digest; the Pod image and annotations retain the complete identity:

| Label | Value |
| --- | --- |
| `cache.fs2.nebius/image-vllm` | `2286e8533ca8b6bc777594bae30524f1` |
| `cache.fs2.nebius/image-evo2` | `5bee4a3103f4111a5ff4dc597d2e052b` |
| `cache.fs2.nebius/image-nv-segment-ct` | `834b6694b7e096c393193d12306ef9b3` |
| `cache.fs2.nebius/image-sdxl` | `8ea08b1a5eabf0ed9c5193e7f49c5546` |
| `cache.fs2.nebius/image-boltz2` | `ec4ccb67476f0783d1b7569593623186` |
| `cache.fs2.nebius/image-genmol` | `c0ce8cab57295b6ba2fc4be17d5f5a78` |

On 2026-08-27 the retained kubelet allowed five parallel image pulls. The six
unique one-B300 images represented about 40.98 GiB compressed before layer
sharing and unpacking. Enable image labels sequentially and wait for each
holder to become Ready; recheck kubelet pull concurrency, registry bandwidth,
and imagefs headroom before using a multi-image warm profile. Never apply every
label blindly to a zero-floor burst node group.

Render and validate without contacting a cluster:

```bash
kubectl kustomize k8s-inference/models/cold-start > /tmp/fs2-cold-start.yaml
kubectl apply --dry-run=client -f /tmp/fs2-cold-start.yaml
python3 -m unittest discover \
  -s k8s-inference/models/cold-start/tests -p 'test_*.py'
```

After a supervised apply and node-group label rollout, wait only for the
selected DaemonSets, then canary a single model restart and compare image-pull,
artifact-load, compile, and readiness timestamps. DaemonSet rolling updates
are bounded to one unavailable holder. Removing a label or DaemonSet releases
image GC pinning; reverting the direct manifests restores ephemeral compile
caches. Neither rollback deletes model PVC data or formats local storage.

## Expected boundary

Once an image, exact model content, and compile artifacts have each been
populated, a same-PVC Pod restart should avoid registry transfer, Hugging Face
download, and repeated compiler-cache generation. The change does not shorten
GPU-node provisioning, the first-ever artifact acquisition, PVC attach time,
or device-memory weight loading. No numeric latency reduction is claimed until
the request-to-ready benchmark records it on the retained B300 tuples.

HOME and XDG stay on the existing bounded `runtime-cache` emptyDir for GLM,
CXR, Segment-CT, and SDXL; only compiler outputs move to the model PVC. Monitor
PVC free space and remove superseded ABI/image/model cache directories only
while the corresponding `Recreate` Deployment is at zero. Never delete a
model PVC merely to clear generated code because the current storage class has
a `Delete` reclaim policy. The two-generation value is a retention target, not
an automated GC controller; capacity alerts and supervised cleanup remain
rollout prerequisites.
