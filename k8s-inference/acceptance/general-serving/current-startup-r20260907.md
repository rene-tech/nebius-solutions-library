# Current H100 startup benchmarks, September 7, 2026

Both models passed three fresh-process trials using cached container images,
fresh emptyDir runtime caches, their existing shared model PVCs, and unchanged
live deployment specifications. Cosmos also passed one separate trial that
provisioned a new preemptible node and downloaded the runtime image.

| Model / cache cohort | n | Process → application ready, median (range) | Pod → Kubernetes ready, median (range) | Pod → first validated output, median (range) |
|---|---:|---:|---:|---:|
| Qwen3-8B, cached image | 3 | 100.03 s (98.95–102.05) | 110 s (110–111) | 114.46 s (113.81–114.68) |
| Cosmos3-Nano, cached image | 3 | 57.40 s (57.16–58.17) | 60 s (60–61) | 64.41 s (64.26–64.86) |
| Cosmos3-Nano, new node + image pull | 1 | 66.10 s | 444 s | 449.55 s |

These are isolated-replica measurements. There was no public activation
request, so public request-to-ready is unmeasured. The process clock starts at
the runtime container's `startedAt` and ends at its timestamped `Application
startup complete` log. Kubernetes readiness includes probe cadence; its
timestamps have one-second granularity. Pod-to-first-output also includes
readiness observation and establishing the direct pod connection.

Individual cached-image trials:

| Model | Trial | Process → application ready | Pod → Kubernetes ready | First request → complete valid output |
|---|---:|---:|---:|---:|
| Qwen3-8B | 1 | 102.050283 s | 111 s | 0.446361 s |
| Qwen3-8B | 2 | 100.034718 s | 110 s | 0.431733 s |
| Qwen3-8B | 3 | 98.948326 s | 110 s | 0.432240 s |
| Cosmos3-Nano | 2 | 57.401380 s | 60 s | 1.617105 s |
| Cosmos3-Nano | 3 | 57.162242 s | 60 s | 1.623002 s |
| Cosmos3-Nano | 4 | 58.171234 s | 61 s | 1.613595 s |

All seven trials used one NVIDIA H100 80GB HBM3, SM 9.0, driver 580.159.04,
with BF16 serving settings retained. Qwen ran on the existing reserved H100
pool; Cosmos retained its preemptible `h100-1x` selector. GPUs were separately
allocated; nodes were shared with other authorized workloads. The source pod
specification was checked unchanged for every clone. Model/image identities:

| Model | Model revision | Runtime image digest | Logged vLLM version |
|---|---|---|---|
| Qwen3-8B | `b968826d9c46dd6066d109eabc6255188de91218` | `sha256:2286e8533ca8b6bc777594bae30524f1426ba46ca21797524e06df6a94b06635` | 0.28.0 |
| Cosmos3-Nano | `7a312c868bcce8e40b3eb40861300a9d0ba3fde1` | `sha256:6d2630c7d637b699557573f2c3fee8df5d4d0cd718977aa22549ed6a6ef30587` | 0.25.0 |

The new-node Cosmos trial's timeline illustrates the additional startup phases:

| Event, September 7 UTC | Time | Elapsed from pod creation |
|---|---|---:|
| Pod created | 05:52:06 | 0 s |
| Existing autoscaler requested 0→1 node | 05:52:36 | 30 s |
| New node registered | 05:53:59 | 113 s |
| Node Ready | 05:54:19 | 133 s |
| Pod scheduled; image pull began | 05:54:40 | 154 s |
| Image pulled | 05:57:03 | 297 s |
| Localizer container began | 05:57:05 | 299 s |
| Localizer finished | 05:57:16 | 310 s |
| Model runtime began | 05:58:15 | 369 s |
| Application ready | 05:59:21.099 | 435.099 s |
| Kubernetes ready | 05:59:30 | 444 s |
| First valid video received | 05:59:35.547 | 449.547 s |

The image pull was 142.693 s for 9,186,624,472 bytes. The model localizer
reported an existing-PVC cache hit. The 59 s gap between init completion and
model-container start is retained as container-startup overhead; these artifacts
do not establish its lower-level cause. Shared-weight page-cache residency was
not forced, so none of these measurements establish disk-cold weight loading.

Qwen returned the exact fixed fixture text in all three trials. All four
Cosmos outputs passed the model/revision/envelope/hash validator and independent
PyAV 15.1.0 decoding: H264, 25 distinct frames, 448×256, 24 fps. Seed 2407,
eight inference steps and guidance 6.0 were unchanged. Logs retain the real
GPU generation and startup phases. Qwen's 2.93–4.21 s weight-loading phase and
28.11–29.01 s reported torch compilation are components of the larger startup.

All seven benchmark deployments and pods were deleted with ownership checks.
Production Qwen remained 1/1 ready at generation 4, and production Cosmos
remained 0 replicas at generation 20. Node scale-down remains owned by the
existing autoscaler; no capacity limits, quotas, drivers, GPU modes, or model
policies were changed.

[Reproducible method](current-startup-method.md),
[redacted measurements and artifact hashes](current-startup-r20260907.json).
Private raw pod/events/node/log/output receipts are retained under
`/home/tux/.local/state/fs2-startup-benchmark-r20260907/general/` in
`qwen3-8b`, `cosmos3-nano`, and `cosmos3-nano-supplement`.
