# Qwen burst startup qualification — 2026-09-08

The deployed startup-retention fix passed the combined bounded checks: a fresh-node burst survived idle demand through image loading and actual GPU restoration; a second burst on the now-prepared node restored and served five useful public requests. No production setting, limit, hot floor, driver or runtime recipe was changed during qualification. This is a standalone qualification, not part of the unchanged 25-second customer-cohort sampler.

Deployed source: `bc264980f3fedc33c8fdc6d59c93096d8db9ccaf`. Cluster: `mk8scluster-e00j5z9te7x5dd9g6a`, project `project-e00rene`, region `eu-north1`. Private receipts are beneath `h100/releases/trial-customer-remediation-20260907/`, in `qwen-startup-20260908-r01`, `qwen-startup-20260908-r02` and `qwen-startup-20260908-r01-evidence`.

## Results

| Measurement | First run: fresh preemptible node | Second run: same prepared node |
|---|---:|---:|
| Actual public-request T0 (UTC) | 07:42:31.269151 | 07:53:00.127627 |
| First observed burst Ready (UTC) | 07:50:51.528257 | 07:54:05.055639 |
| T0 to first observed Ready | 500.259 s | 64.928 s |
| Public requests | 180/180 passed | 53/53 passed |
| Hot / burst successful-request counter increments | 180 / 0 | 48 / 5 |
| Actual restore mechanism | `cuda-criu-restored` | `cuda-criu-restored` |
| CRIU command duration | 47.964 s | 31.827 s |
| CUDA restore command durations, summed | 9.776 s | 9.693 s |
| CRIU + CUDA + unlock commands, summed | 57.769 s | 41.551 s |
| Natural post-Ready burst removal | Observed by 07:51:26.180567 | Observed by 07:55:19.897351 |

First-observed readiness includes sampling uncertainty. Restore command sums are not full request startup: provisioning, scheduling, image pulling, init transitions and readiness are included only in the T0-to-Ready measurement. Restored counters began at two historical successes; these were explicitly subtracted rather than counted as new work.

The first run deliberately retained its incomplete useful-serving gate. Its 180 requests had all drained before Ready, so the existing short idle policy naturally removed the newly Ready burst. The separately authorized follow-on helper was launched too late and refused before submitting any request because no Ready burst remained. That operator/harness coordination failure was reported immediately. The first helper was stopped after all its operations were terminal; its exit143 is not rewritten as a completed successful qualification. The second run used a fresh output directory/T0 and automatic readiness/counter observation, stopped admissions after proof, drained and completed its bounded recovery with exit0 at 07:55:49.559162 UTC. It was not a retry of a failed customer request.

The superseded operator-timed follow-on source is retained recoverably only in private `qwen-startup-20260908-r01-evidence/qwen_startup_followon.py` (SHA256 `88e81336b0ae3528c78febf79e4210868410b04b486e1017d055824ca8c2d212`). It is not a recommended reusable runner: its guard refused and no follow-on traffic ran. The committed automatic qualifier is the successful path. The startup and full-window harness tests passed (15 tests); the new read-only metric capture helper passed compilation and Ruff checks.

## What the fresh-node run proves

The burst Pod `qwen3-8b-b300-burst-h100-1x-6d6575886f-7gb9j` (UID `b14f84a8-3ab6-49e9-97ec-68e59c5190e1`) scheduled onto newly created `computeinstance-e00jndasb6nz4eah4g` at 07:45:17. The full regional vLLM image was still required:

`cr.eu-north1.nebius.cloud/e00akg9ndpx77eaexh/fs2-models/vllm-openai@sha256:2286e8533ca8b6bc777594bae30524f1426ba46ca21797524e06df6a94b06635`

The image pull took **165.694 seconds**. Its Kubernetes event reported an image size of **8,634,306,308 bytes**; this is a runtime-reported image size, not measured network bytes or snapshot size. The image is used by the first `snapshot-local-address` init as well as the main runtime. Subsequent snapshot-tools image preparation and init-container transitions also contribute to total startup.

At 07:47:52, and again at 07:49:32, the retained exact ScaledObject queries returned **ordinary operation demand=0 and startup retention=1** while the burst was not Ready. The Pod was not prematurely deleted after its prior 30-second idle cooldown. Startup protection ended after Ready; later both demand and retention returned to zero and the burst was naturally removed. This directly exercises the formerly failing idle-during-image-pull condition without increasing the existing startup deadline or node/replica ceilings.

The address init's eventual timestamps and stdout show successful subsecond completion at 07:48:04; an earlier stale Pod-status sample appeared to show it still running. A roughly 43-second transition before the next init image pull remains unclassified. Neither interval is falsely labeled CUDA restore time.

## Actual useful burst attribution and service continuity

The second burst Pod `qwen3-8b-b300-burst-h100-1x-6d6575886f-cdpwd` (UID `9b9b4280-0604-496a-8903-e6f893cb2149`) has both actual restoration logs and successful public-serving evidence. Its counter rose **2→7**, while the unchanged hot Pod rose **431→479**. The total, 5+48, exactly matches the 53 unique semantically verified public responses. No direct Pod inference was used and no other scientific/ordinary test traffic was active. This is isolated aggregate per-Pod attribution, not a claim that each operation ID was individually matched to a runtime log.

One read-only per-Pod metrics proxy observation raced with normal Pod termination. It is retained as a monitoring limitation, not hidden or counted as a failed model request. The helper's `observation_errors=0` counts outer observation failures, not this per-Pod teardown-time metrics field.

Continuous read-only observation covered **07:42:30.944506–07:56:23.392768 UTC**, including both runs and recovery. All 34 sampled sets of four admin GETs returned200. The original hot Pod remained Ready; no test policy changes or forced node scale-down occurred. The complete [publication-window export](qwen-startup-20260908-publication-window.json) contains three contiguous successful, nontruncated Loki windows, 12 publication events, no parse errors and **zero recorded route withdrawals** across all control-plane replicas. This is bounded evidence, not an uptime SLA.

All standalone traffic and its cluster observer are stopped. The next unchanged scientific cohort, r03, started only after these gates passed; its receipts and measurements are separate.
