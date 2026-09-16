# MindGuard public classifier qualification — 2026-09-16

Both pinned public checkpoints run on task-owned Scientific AI previews with an
immutable runtime image and a native 32768-token context. Both are reachable from
the existing control-plane pod. This is **functional GPU qualification**, not
clinical validation or a completed 4B/8B quality recommendation.

The Sword testset's exact Parquet file still returns authenticated HTTP 403. Its
separate dataset gate must be accepted for the configured HF identity. The private
MindGuard v2 clinician weights/configuration have not been supplied. Neither public
checkpoint is a v2 substitute, clinician, patient simulator or rubric judge.

## Runtime and measurements

Image: `sha256:2286e8533ca8b6bc777594bae30524f1426ba46ca21797524e06df6a94b06635`.
Observed vLLM 0.28.0, PyTorch 2.13.0+cu130, Transformers 5.15.1, CUDA 13.0 and NVIDIA
driver 580.173.02. Both use BF16, tensor parallel size 1, eager execution, temperature
0, seed 0, at most 15 generated tokens, and 0.8 GPU memory utilization.

The initial 4096-context size comparison ran sequentially on the same physical L40S
GPU. Both then passed native 32k context checks, and the final hot deployments each
use a separate L40S. Final 32k results below use ten generated functional cases,
three repetitions per case, three warmups excluded from metrics, and 30 measured
requests per row. They do not estimate clinical accuracy. All ten distinct cases
and both pinned model-card examples returned the expected label/category.

| 32k serving profile | Serial mean | Serial p95 | Serial throughput | Concurrency 4 throughput |
| --- | ---: | ---: | ---: | ---: |
| MindGuard-4B | 312 ms | 330 ms | 3.20 requests/s | 10.26 requests/s |
| MindGuard-8B | 310 ms | 326 ms | 3.23 requests/s | 10.30 requests/s |

The same-GPU 4096 baseline was similarly close: 4B 309 ms / 3.24 requests/s and 8B
312 ms / 3.20 requests/s. This sample does not establish a useful short-context 4B
latency advantage. Keep both IDs selectable for comparison; choose the final default
only after the gated Sword testset and actual MindEval transcript checks. At longer
synthetic contexts the single 4B boundary probes were faster, but those are not a
repeated throughput comparison or clinical-quality evidence.

| Exact prompt tokens (delivered tokenizer/template) | 4B result | 8B result |
| --- | --- | --- |
| 8,186 | completed, 0.77 s | completed, 1.03 s |
| 16,380 | completed, 1.11 s | completed, 1.39 s |
| 32,734 | completed, 2.61 s | completed, 3.15 s |
| 33,992 | explicit HTTP 400 error; no classification | explicit HTTP 400 error; no classification |

The maximum model length includes input and output. Boundary checks preserve all
input and never silently truncate it. Exact configuration, tokenizer, template and weight files were
SHA-256 verified inside the serving containers: 14 files / 16,105,837,094 bytes for
4B and 17 files / 32,778,906,570 bytes for 8B. Expected hashes are in `artifacts/`;
verification receipts and request-level results are in `evidence/`.

Startup clocks are distinct from request latency. 4B's first cold image+weights
startup took 301 seconds: image pull 166 seconds, hydration 38 seconds and runtime
to ready 91 seconds. Its warm-image/warm-weights 4096 restart took 80 seconds. 8B's
first startup reused the image and hydrated cold weights: 131 seconds overall,
38-second hydration and 91-second runtime start. Both native 32k warm-cache profile
starts took 91 seconds in the recorded staging pods. These are observed individual
starts, not a startup distribution or snapshot restore claim. Both runtime and KV
cache fit one L40S; GPU memory samples and runtime allocation logs are recorded
separately from request measurements.

An initial 8B port-forward connection was attempted before its HTTP server was
ready and exited. The resulting transport-error report is retained as
`8b-portforward-startup-failure.json`; readiness was confirmed, the tunnel reopened,
and successful measurements rerun. Failed calls are never counted as safe results.

## Live resources and handoff

- Project `project-e00rene`, cluster `mk8scluster-e00j5z9te7x5dd9g6a`, context
  `fs2-storage-h100`, namespace `fs2-models`, region `eu-north1`. These are internal
  task-owned previews; tenant ownership and retention are supplied by the enclosing
  authenticated workshop run, not by a fabricated runtime tenant identity.
- 4B Deployment `fs2-mindguard-r20260916-4b`, UID `648a6df9-ff72-42d5-90ab-f1e3ed7fa13f`,
  one regular L40S on `computeinstance-e00ax3mgt7y3a0asa6`.
- 8B Deployment `fs2-mindguard-r20260916-8b`, UID `753c17f7-3df0-4067-9616-b85d136be909`,
  one regular L40S on `computeinstance-e00sa78kng1kwhej6q`.
- Existing free regular L40S capacity was used because the preemptible H100 nodes
  were NotReady. No quota or node-group changes were made.
- Task PVC `fs2-mindguard-r20260916-cache`, backing volume
  `pvc-6d9e82f0-de52-4de0-ba46-56f3804bf523`; task Secret
  `fs2-mindguard-r20260916-hf` is downloader-only. Neither weights nor credentials
  are committed.
- Internal endpoints are
  `http://fs2-mindguard-r20260916-4b.fs2-models.svc.cluster.local:8000/v1` and
  `http://fs2-mindguard-r20260916-8b.fs2-models.svc.cluster.local:8000/v1`.
- Pod labels satisfy the existing control-plane egress policy. No shared network
  policy or shared service was changed. The normal CP-to-model service inference
  smoke passed for both IDs and their exact native context/revision paths.

Both task-owned previews and their cache/Secret are intentionally retained for
workshop integration; the two GPUs remain in use. Parent task owns CP/router/UI
rollout and full customer-path acceptance. Remove a preview from use by scaling only
its named task Deployment to zero; preserve the cache while artifact-dependent
qualification continues. Rollback must not target shared services or unrelated
model deployments. Local diagnostic tunnels can be stopped without affecting pods.

The authenticated `/v1/mindguard/assess` contract returns per-user-prefix observation
and errors, with existing request telemetry and explicit `observational_unbilled`
usage. Budget-constrained keys are unsupported until normal durable queue/meter
integration exists. The workshop must persist the full observation under its run;
observer unavailability must not become a safety label or terminate clinician
evaluation. No automated content enforcement is introduced.
