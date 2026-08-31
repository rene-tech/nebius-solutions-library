# Qwen3-8B on one B300

> Import note: reusable manifests, locks, qualification code, and tests are
> preserved here. Generated live evidence was intentionally not imported; the
> measurements below remain historical context from source commit
> `b5dbd9ec7cdab67d8110b2b2b4c675091954e7c1`, not a fresh qualification.

This directory is the lean, direct-Kubernetes handoff for the exact public
model `Qwen/Qwen3-8B@b968826d9c46dd6066d109eabc6255188de91218` on one
physical NVIDIA B300 GPU. It exposes only a `ClusterIP` OpenAI-compatible vLLM
API. Public authentication, MCP routing, and custom admission are deliberately
outside this slice.

## Immutable inputs

- Model: [Qwen/Qwen3-8B revision b968826d](https://huggingface.co/Qwen/Qwen3-8B/tree/b968826d9c46dd6066d109eabc6255188de91218), Apache-2.0, BF16, no quantization.
- Runtime: [vLLM 0.28.0](https://github.com/vllm-project/vllm/releases/tag/v0.28.0), source commit `2cf0a6915ce544dc493a0990f2ea38d81601128a`.
- Upstream linux/amd64 image: `docker.io/vllm/vllm-openai@sha256:2286e8533ca8b6bc777594bae30524f1426ba46ca21797524e06df6a94b06635`.
- Regional mirror: `registry.example.invalid/k8s-inference/models/vllm-openai@sha256:2286e8533ca8b6bc777594bae30524f1426ba46ca21797524e06df6a94b06635`.
- Image config: linux/amd64, CUDA `13.0.2`, vLLM build commit `2cf0a691...`, and CUDA architectures `7.5 8.0 8.6 8.9 9.0 10.0 12.0`.

The upstream multi-arch `v0.28.0` index is
`sha256:61fc8a896b0a4fbbbdc063bc4b0dbc25ce98e02b5050c24aeb7830ac02039b14`.
The release notes also name a `v0.28.0-cu130` alias, but that alias returned
`MANIFEST_UNKNOWN` on 2026-08-27. The handoff therefore uses the extant default
CUDA 13 image and its platform manifest digest. The mirror must preserve that
digest byte-for-byte.

[`model.lock.json`](model.lock.json) is the source of truth for all 15 files,
16,397,461,266 total bytes, the five weight-shard SHA-256 values, license hash,
runtime identity, and serving parameters. `localize.py` downloads only that
exact revision and fails before vLLM starts if a file is missing, unexpected,
the wrong size, or the wrong SHA-256.

## Live result — 2026-08-27

The retained backend is `1/1 Ready` in `fs2-models` on cluster
`fs2-serve-usn1`. It uses one visible NVIDIA B300 SXM6 PC, compute capability
10.3, on the already-provisioned preemptible eight-B300 worker. vLLM reported
`arch=sm103`, BF16, `quantization=None`, TP1, and the exact served model name.

- Cold apply-to-Ready: **465.804 seconds**, including a **198.594-second**
  8.63 GB image pull, **126.067-second** 16.4 GB model download,
  **11.200-second** complete hash verification, and runtime load/warmup.
- Cold semantics: distinct `BLUE` and `42` responses in **0.556** and
  **0.555 seconds**.
- Warm recovery: SIGTERM to vLLM PID 1 preserved the Pod, Node, GPU and
  `emptyDir`; Ready returned in **62.201 seconds**. Post-restart responses were
  `BLUE` and `42` in **0.566** and **0.555 seconds**.
- The Pod has one intentional clean restart and remains Ready behind the
  internal `ClusterIP`. No public route or token was created.

Exact resource identities and machine-generated semantic evidence remain in
the recorded source commit. They are excluded from this reusable package
import; a new qualification run should write fresh evidence locally.

KServe CRDs became available on the retained cluster, but this expedited slice
does not claim or publish an unqualified second KServe path. The proven plain
Deployment/Service is the lean-release handoff; a KServe Standard wrapper can
consume the same immutable model/image lock in a separate qualification.

## Deployment contract

The Deployment:

- requests and limits exactly one `nvidia.com/gpu`;
- binds the retained `burst` / `b300-8x` / `preemptible` pool and tolerates only
  the established `dedicated=fs2-inference:NoSchedule` taint;
- uses BF16, tensor parallel size 1, no quantization, maximum model length
  32,768, prefix caching, and 80% GPU-memory utilization;
- uses separate startup/readiness HTTP and liveness TCP checks;
- disables service-account token mounting, drops Linux capabilities, and
  exposes only a `ClusterIP` Service;
- permits model localization over HTTPS and DNS, while ingress is limited to
  `fs2-models` and the future `fs2-system` namespace.

Render and test offline:

```bash
python3 -m unittest discover \
  -s k8s-inference/models/qwen3-8b-b300/tests -v
kubectl kustomize k8s-inference/models/qwen3-8b-b300
kubectl apply --dry-run=client -k \
  k8s-inference/models/qwen3-8b-b300
```

Deploy from the isolated task branch only after verifying that no existing
`fs2-models` workload would be replaced:

```bash
kubectl --context fs2-serve-usn1 -n fs2-models \
  get deploy,rs,pod,svc --ignore-not-found -o wide
kubectl --context fs2-serve-usn1 apply -k \
  k8s-inference/models/qwen3-8b-b300
kubectl --context fs2-serve-usn1 -n fs2-models \
  rollout status deployment/qwen3-8b-b300 --timeout=40m
```

The first Pod localizes into a 64 GiB `emptyDir`. The init-container receipt at
`/models/localization-receipt.json` records download and verification timing
and all realized hashes. The model container sets the Hugging Face and
Transformers offline flags, so runtime readiness cannot trigger a substitution
or late network fetch.

## Qualification boundary

This is an **already-provisioned preemptible node** cohort. It is not a
new-node, scale-from-zero, external durable-admission, or public endpoint
measurement. Record these clocks separately:

1. manifest apply T0;
2. Pod creation, scheduling, init start, download complete, hash verification
   complete, application container start, and Ready transition;
3. first internal request start, first byte where observed, and semantic
   completion;
4. restart trigger, container restart observation, Ready recovery, and the two
   post-restart semantic completions.

Run `qualify.py` through a bounded port-forward or inside the Pod network. Each
attempt validates `/v1/models`, sends two distinct deterministic requests with
thinking disabled, requires `BLUE` and `42`, and writes request hashes,
latencies, outputs, model identity, usage, and terminal status.

```bash
kubectl --context fs2-serve-usn1 -n fs2-models \
  port-forward service/qwen3-8b-b300 8000:8000
python3 k8s-inference/models/qwen3-8b-b300/qualify.py \
  --attempt cold-01 \
  --output k8s-inference/models/qwen3-8b-b300/evidence/cold-01.json
```

For warm restart evidence, terminate PID 1 in the `vllm` container and let the
Deployment restart it. This preserves the Pod UID and `emptyDir`, so it tests
runtime reload from localized bytes—not preemption or new-Pod recovery. A Pod
deletion or real preemption loses this cache and must download and verify all
16.4 GB again.

## Cache and scale-from-zero limits

The retained shared filesystem is attached to the managed node template but is
not exposed through a qualified Kubernetes StorageClass/CSI contract in this
slice. This workload therefore does not use hostPath, an unreviewed mount, or a
claim that the weights survive preemption. Pod recreation and scale from zero
re-run localization from Hugging Face. The zero-floor 1×B300 pool is not
activated or resized by this task; placement deliberately consumes one GPU on
the already Ready 8×B300 preemptible worker.

This conventional path is intentionally the first service, not the final
fast-start design. A later cache owner may replace the `emptyDir` only after it
has exact revision/hash custody, retained-storage qualification, and matched
cold/recovery evidence.

## Ownership and cleanup

The retained namespace resources are uniquely named `qwen3-8b-b300`; the
regional image is under `fs2-models/vllm-openai`. The lean release owner may
adopt both. If the backend is not adopted, remove the workload without touching
the cluster, GPU node group, shared registry, or sibling resources:

```bash
kubectl --context fs2-serve-usn1 delete -k \
  k8s-inference/models/qwen3-8b-b300
```

Registry image deletion is a separate destructive operation and is not part of
that command. Resolve the exact artifact ID and confirm that no adopted release
references the digest before any deletion.
