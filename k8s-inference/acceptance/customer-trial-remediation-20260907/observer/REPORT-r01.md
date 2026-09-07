# Remediation cohort r01 — observer and ordinary-client evidence

Scientific operations all completed, but this is **not a clean overall acceptance cohort**. Two short Qwen route-withdrawal windows and the independently observed false `0s` Protenix restore card remain recorded defects. A narrow local Qwen follow-up fix is tested but **not deployed**.

## Scope and completed observation

- Deployed source: `5fec52059`; control-plane image digest `6b6cdb1f0da4b63c312acb1237949588b7075421f7cb0bbfc2eee286639e317f`. Harness source at start: `1d6cff9f3fbf0f71ee5cedf41c469e08c1348209`.
- Existing H100 cluster `mk8scluster-e00j5z9te7x5dd9g6a`, project `project-e00rene`, region `eu-north1`. Two reserved 8-GPU nodes; existing preemptible pool remains min0/max2. No capacity limits, policies, images or live configuration changed by this observer.
- Scientific campaign: `2026-09-07T16:08:17.058652Z`–`16:33:09.949657Z`; all14 original operations passed. Exact receipts and scientific measurements: [workload report](../workload/REPORT-r01.md).
- 66 cluster samples, `16:07:35.717485Z`–`16:34:43.785891Z`, every25seconds. Four scheduled samples after science completion provide >90seconds recovery.
- 66 ordinary Qwen requests: HTTP33/33 and MCP33/33 successful; no client submission retries. Timestamp classification: 2 before science,59 during,5 after. Raw phase-file labels are preserved even where their update lagged.
- HTTP median1.956s, p95 2.475s, max2.610s; MCP median3.324s, p95 3.918s, max4.015s. These measure full non-streaming client requests, not TTFT or GPU decode.
- 17 discovery cycles,51 checks total, all successful: `/readyz`, `/v1/models`, `/v1/scientific-models`. Catalog remained14 serving plus10 scientific IDs.
- All264 sampled admin capacity/operations/telemetry/overview requests returned200. Median times respectively0.546/0.618/1.020/0.639s; p95 0.822/0.861/1.309/0.838s.
- No newly failed Pods, container restarts, node-pressure incidents, baseline-serving readiness losses or missing queried metric series. Six old failed Pods were excluded as baseline state.

Evidence correction during r02 preparation: r01's Pod/node/Kueue/DCGM coverage included academic workloads, but its two per-Pod CPU/RAM queries filtered only `fs2-system|fs2-models`. Academic-namespace Pod CPU/RAM was not collected. The earlier helper summary claiming that coverage was incorrect and has been corrected; no historical measurements are retrofilled. Node-wide metrics, GPU accounting and scientific outcomes are unaffected.

Machine-readable outputs: [cluster observation](r01-observation.json), [ordinary-client receipts](../experience/r01-interactive.json). Detailed records are allowlisted to the14 submitted operation IDs; cluster-wide utilization includes co-tenant work.

## Placement, elasticity and priority recovery

ESMFold2 triggered the existing preemptible group from0→1 at16:13:37Z. Its Pod was scheduled at16:15:40Z on `computeinstance-e00e83s20q858hchkd`, began pulling its runtime at16:16:37Z, and eventually completed in360.366s. Allocatable GPUs rose16→17; no manual capacity intervention occurred.

The formerly impossible second BindCraft placement now froze affinity to `h100-reserved-8x`, correctly excluding the1-GPU shape that cannot fit16CPU plus0.1CPU collector. Operation `b5c5aa76-ae94-4fd6-875f-4fd7fccee0fe` waited at Kueue admission from16:18:52Z, scheduled onto reserved node `computeinstance-e00p3acr87k9k4mckj` at16:22:16Z, and passed in594.455s. This was a legitimate fitting-pool quota wait, not an admitted-unschedulable job; no cancellation or manual retry.

The four-shard RFdiffusion bulk operation `ca30eecd-6aa2-49d2-9472-1c2a3d185876` passed in288.522s. Kueue events explicitly identify these three priority preemptions (priority0 preempting bulk−100):

| RF shard attempt1 | Event time UTC | Preempting work |
|---|---|---|
| design-000 |16:28:42| ESMFold2-Fast, Job `fs2-fold-main-a1-444978a81099` |
| design-001 |16:29:06| AlphaFold3, Job `fs2-inference-main-a1-6e6f5ad374a8` |
| design-003 |16:28:44| Qwen burst Pod UID `7a1dcb7a-421a-402d-a4ed-80b1f4f26cbb` |

All three released resources and succeeded on attempt2. Shard002 completed on attempt1. This demonstrates bounded priority-preemption recovery, **not** a cloud spot-interruption test.

## Startup policy and actual snapshot usage

Baseline and final scientific-policy payloads are identical. Proteina-Complexa, BoltzGen, Mosaic, BindCraft, RFdiffusion, ESMFold2, ESMFold2-Fast and AlphaFold3 had no explicit snapshot startup policy selected. Their snapshot options or qualified catalog entries are not proof of snapshot usage in this cohort.

Protenix-v2 retained its `sample-structure` policy: backend `cuda-criu`, bundle `protenix-v2-h100-cuda-criu-20260907-r2`. Operation `18efdb3c-fa99-42a7-9a39-876d22a2021b`, attempt `79dd47f4-7384-5f0f-943f-ff4d0a1686b8`, emitted `scientific_snapshot_request` with mechanism `cuda-criu-restored` at16:10:27.946846795Z. Logged steps: CRIU2.644533s, CUDA restore0.517366s, unlock0.025011s, all returncode0.

Its terminal ledger correctly records3.946846 restore GPU-seconds,16.053154 active-compute GPU-seconds,33 scheduler-occupied GPU-seconds, reconciliation delta0 and no data gaps. Restore accounting covers the supervisor/container boundary through the restore marker, not CUDA-copy alone. The admin browser nevertheless showed a false0s restore duration; root/admin lane owns that separate display repair. It must be checked after the next deployment.

## Accounting and capacity interpretation

All48 campaign attempt ledgers are terminal and reconciled:45succeeded,3preempted. Aggregate phase GPU-seconds:

| Phase | GPU-seconds |
|---|---:|
| Active compute |3184.787806|
| Image pull |383|
| Artifact load |230|
| GPU restore |3.946846|
| Resident idle |35|
| Unclassified / cooldown grace |0 / 0|

Total quota-reserved4151.713131 GPU-s; scheduler-occupied3836.734652; device-allocated3761.073254; occupied-idle651.946846. These are application/scheduler accounting clocks, not proof of continuous kernel activity or a billing invoice.

Cluster-wide sampled DCGM hardware-busy integral was807.439315 GPU-s versus25071.519683 scheduler-requested GPU-s across1626.396682 observed seconds. The large difference includes existing hot co-tenant models and application CPU/loading/wait phases; it must not be attributed entirely to this customer. At-most1% GPU-utilization allocations are similarly an observation, not a causal classification.

Reserved GPU node minimum root headroom remained57,187,274,752 and64,069,681,152 bytes. The new preemptible node changed from309,075,415,040 to272,558,497,792 free bytes as images/artifacts arrived (~36.52GB difference). No DiskPressure occurred. Across sampled nodes, maximum CPU busy72.49%, memory used24.56%; disk-use percentage alone would overstate the risk without those byte counts.

## Remaining Qwen defect and local follow-up

Two natural burst Pods were observed Pending while the original fixed hot Pod UID `2c04bb56-77ea-4838-8198-256330c55b07` stayed Ready:16:14:41Z and16:28:51Z. The original scale-up readiness repair therefore exercised a real transition. However, brief scale-down acknowledgement gaps still withdrew the model:

- Earlier retained bridge log: withdraw/Desired at16:15:01.161Z, publish/Ready at16:15:06.312Z (~5.15s; one CP replica's pair retained).
- Later retained bridge logs: all three CP replicas withdrew at16:29:18.508–608Z and republished16:29:23.723–754Z (~5.2s).
- The hot Pod stayed Ready with0restarts; desired model generation6 and spec digest were unchanged. No sampled client landed in either withdrawal window, so66/66 successful requests **does not establish uninterrupted routability**.
- Direct HPA inspection retained `ScalingActive=False`, `ScalingDisabled`, transition time16:29:16Z; `AbleToScale=True` remained unchanged. ScaledObject subsequently acknowledged `HPAActive=True/ScalingDisabled`.

The source-level cause is the global readiness gate's strict autoscaler-install predicate: HPA and ScaledObject acknowledge idle state asynchronously. Exact historical ScaledObject condition contents during the gap were not captured, so that specific timing detail is an evidence-supported inference, not a claimed recovered snapshot. A regression reproduces `Desired` when the strict predicate is forced on an otherwise healthy fixed-hot + explicitly idle HPA state.

Local follow-up in `model_deployment_controller.py` permits only this idle acknowledgement lag **for serving readiness when an independently current fixed hot Deployment is ready**. Exact desired-inventory digest/ownership, scaler Ready, HPA owner/target/generation/AbleToScale, explicit target/HPA desired0, and relinquished replica-field ownership are still required. Bootstrap/handoff writes and all-cold publication remain strict. Negative controls cover stale/no hot runtime, foreign/wrong-target/stale HPA, HPA/scaler errors and unreleased replica ownership. No broad RuntimeError masking or auth changes.

Focused validation: 107 control-plane tests passed in 32.12s; 21 registry/harness/export tests passed in 1.93s; Ruff clean. One existing Starlette deprecation warning. The workload agent independently reviewed this narrow delta and ran 61 tests, finding no blocking issue; its two suggested nonzero-replica negative controls were added and passed in the final 107-test run. This new follow-up remains uncommitted and **not deployed**; root must integrate it with the admin display fix, deploy, then run two complete clean cohorts before declaring the bounded scenario 10/10.

For the next cohorts, inspect retained `model_publication_changed` logs across the entire campaign, not only the 25-second samples: the first five-second withdrawal was missed by point sampling. Do not infer uninterrupted availability solely from successful ordinary-client requests.

## Private evidence and cleanup

Private root: `/home/tux/.local/state/k8s-inference-dual-acceptance/h100/releases/trial-customer-remediation-20260907/r01/observer/`. Do not publish raw logs or credential bundles.

- `samples.jsonl`, `baseline-context.json`, `final-context.json`, `sampler-completed.json`.
- `protenix-switch-job-r01.json`: SHA256 `e74d58d663afd254900dbc583e52fc229cdaff6ca087a6a4b38a93ef6be6c16b`.
- `bindcraft2-fit-queued-r01.json`: `ff9ad8d4033ea0ff2ceed6153a2673d72ebad14a003f388066fe13a22b811613`; `bindcraft2-fit-running-r02.json`: `934593a02fd2e041ceb8575d4fc09420f106fb856ded0f2fa6d8456227c6e7a8`.
- `rf-shard-000-preemption.json`: `bd278d581130fd6220cd2de885eb6fde4788cf77e8899d56047625a2f656d5ee`; `rf-shard-001-preemption.json`: `37fa6c4e54cc1b6901f50850d5cc4aeec1f99e99f888aff349905a2e877bf79f`; `rf-shard-003-preemption.json`: `3ce912a279fee6c8e81caec551d89f477c6ee420e26c7990211b9db2195cb187`.
- `qwen-burst-transition-loki.json`: `649c8ee4ac37f284788894dc0cac881afb05889c8105b3378de2c558498a29f6`; `qwen-earlier-burst-publication-loki.json`: `7c20a614d972083c4705cf891c8cca2fba6d026ebde4e830ee16887509afb0f3`; `qwen-post-scale-down-state.json`: `f164569e590e961dec1dc6e5d3827302ff37f702d101332aa95eca8cee245898`.

Final sample has no campaign-owned nonterminal Pods; scientific receipts confirm all resources released. Existing17-GPU capacity was left to its normal autoscaler. Observer PID3414289 and background PID3414321 stopped cleanly; do not reuse those PIDs. No task-owned traffic is still running.

Root/admin agent sessions reported a model-capacity error before the expected final stop signal. After >90seconds completed recovery, this observer stopped only its own test processes to avoid unbounded traffic and queued the handoff. Root alone still owns commit/deploy/Slack. No r02 is authorized or running.
