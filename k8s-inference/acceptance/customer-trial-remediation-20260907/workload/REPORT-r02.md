# Customer trial r02 — failed cohort, explicitly interrupted

The unchanged 14-case campaign produced **12 complete customer passes, one failed HTTP 409 submission, and one RFdiffusion bulk request blocked by a server reconciliation defect**. All nine model/profile variants produced at least one valid customer result, but the platform did not deliver a smooth unattended batch experience. This is not a clean cohort and does not satisfy the two-consecutive-clean-runs gate.

First client start: **2026-09-07 20:10:47.879537 UTC**. Failed-cohort boundary: **20:40:14 UTC**. Production source: `5f5061b28ee71a59432492a1bdf6106428a85367`; campaign repository source: `024aad0728815ebdebfe178912df4a3b1421a574`. Original fixtures, bytes, parameters, priorities, snapshot policies, and maximum four client threads were unchanged. The campaign retained the RF→Protenix→Mosaic sequential prefix and the real four-shard lower-priority RF request.

## Customer timings

Seconds below are individual observations, not percentiles or cold-start benchmarks. Client wall includes input uploads, submit/replay, five-second status polling, semantic validation and two verified downloads. Local slot wait occurs **before submission**, relative to the mixed-phase start at 20:15:30.431866 UTC; it is not server queueing. Accepted→terminal includes server scheduling and execution, not just GPU compute.

| Case | Exact runtime variant | Accepted→terminal (s) | Client wall (s) | Local slot wait (s) | Customer outcome |
| --- | --- | ---: | ---: | ---: | --- |
| RFdiffusion switch | rfdiffusion-v1-1-0 | 90.314 | 97.932 | — | Passed |
| Protenix v2 switch | upstream-v2-0-0 | 63.899 | 70.587 | — | Passed; actual CUDA/CRIU restore |
| mosaic switch | mosaic-boltz2-proteinmpnn-v1 | 106.558 | 113.974 | — | Passed |
| BoltzGen 1 | upstream-v0-3-2 | 958.877 | 968.700 | 0.000 | Passed |
| BindCraft 1 | v1-5-3-pyrosetta-academic | 597.263 | 605.768 | 0.000 | Passed |
| Proteina-Complexa 1 | upstream-dev-20260827 | 254.339 | 262.475 | 0.001 | Passed |
| ESMFold2 | biohub-v3-4-0 | 357.245 | 366.676 | 0.001 | Passed |
| BoltzGen 2 | upstream-v0-3-2 | 821.996 | 830.044 | 262.558 | Passed |
| BindCraft 2 | v1-5-3-pyrosetta-academic | 584.341 | 590.306 | 366.749 | Passed; reserved-pool quota wait |
| Proteina-Complexa 2 | No successful client receipt | — | 3.542 | 605.851 | **Failed: initial submit HTTP 409** |
| mosaic 2 | mosaic-boltz2-proteinmpnn-v1 | 309.792 | 315.828 | 609.414 | Passed |
| RFdiffusion four-shard bulk | No terminal result receipt | — | — | 925.263 | **Blocked; local client stopped** |
| ESMFold2-Fast | biohub-v3-4-0; distinct image identity | 89.776 | 97.734 | 957.074 | Passed |
| AlphaFold3 | upstream-v3-0-4 | 69.043 | 76.011 | 968.783 | Passed |

The detailed [partial measurements](results-r02/partial-measurements.json) retain exact operation IDs, runtime/image/recipe identities, per-stage attempts and scheduling, clocks, poll/submit/download latency and raw-file hashes. Original successful and failed receipts are copied unchanged; the missing RF terminal receipt and missing runner aggregate were **not** manufactured.

## What passed

- **1,041 scientific HTTP exchanges:** 986×200, 28×201, 26×202 and the preserved 1×409. There were 894 status polls; maximum measured poll response was 1.190 seconds.
- **13 exact same-operation idempotent replays**, including the initially accepted RF bulk. The failed Proteina submit was not retried or replayed.
- **24 hash-verified downloads** across the 12 successful clients. Total measured download-response time was 11.201 seconds; maximum individual response was 0.585 seconds. Semantic checks passed for every successful client.
- All **36 attempts in successful customer result receipts** succeeded and released resources. The separately identified Proteina operation additionally succeeded and released all four attempts. This is not a claim that every campaign resource was released: RF remains unreconciled.
- BindCraft 2 retained the full 16 CPU + 100m collector and 96Gi + 256Mi requests. It correctly waited **196 seconds** for reserved quota, then scheduled at **20:24:56 UTC** on reserved node `computeinstance-e00p3acr87k9k4mckj`; container start was 20:25:05 UTC. It was not sent to the impossible-fit 1×H100 pool. No intervention or resource lowering was used.
- Protenix used a **real restored CUDA/CRIU snapshot**, not merely an enabled policy. Its container started at 20:13:06 UTC; the `cuda-criu-restored` marker arrived at **20:13:10.196746735 UTC**. The corresponding reconciled ledger records **4.196746 restore GPU-seconds**, **16.803254 active GPU-seconds** and **35 scheduler-occupied GPU-seconds**, with no gaps. The restore interval includes container/supervisor preparation; it is not a standalone CUDA memory-copy benchmark. The fixed admin phase view showed 4.2 seconds.
- Browser observation independently verified automatic ESMFold2 state progression, real result download, and BindCraft queue→admission→compute progression without refresh or policy changes. Independent normal-service observations and final recovery belong to the observer and experience reports, not to the scientific client's counters.

## Failures that must be fixed before another clean cohort

### 1. Proteina submission committed work but returned HTTP 409

`batch-07-proteina` received HTTP 409 after 0.499462 seconds of submit transport time, with no operation ID returned. There was no retry. The server nevertheless committed operation `d6ddbe20-6c2b-4c2e-ab65-03442c9c1b82` at **20:25:39.761496 UTC**; it succeeded at **20:29:43.523512 UTC**, published its result and released all four attempts.

Ownership is proved by offline reconstruction of the unchanged final uploaded-input request: its exact deterministic idempotency key matches the server operation. [Association evidence](proteina-submit-409-r02.json) preserves that proof and the original failure. No successful submit receipt, result validation, download or replay is claimed for this client case. A four-thread client limit no longer proves at most four active server operations after this accepted-but-error response: the harness freed the failed client slot while its server operation continued.

The retained client trace does not include the error response body. The evidence proves persistence/response inconsistency, not the precise underlying SQL/exception cause. A narrow source follow-up should examine durable append→`_materialize_admission` and `BatchRepositoryConflictError` translation in scientific batch service/repository, including concurrent outbox materialization; do not assume the scientific runtime failed.

### 2. RF priority requeue left completed work permanently nonterminal

Operation `daae227f-f7a0-4fe7-9912-1a95c675c3d9` was accepted at **20:30:59.018864 UTC**. Retained Kueue events prove lower-priority RF shards 003 and 000 were preempted by ESMFold2-Fast and AlphaFold3, respectively. Those same `a1` Jobs had replacement Pods; do not relabel a Pod replacement as an application-level attempt-2 success.

Three surviving Jobs were Complete=True by **20:34:57 UTC**, with both scientific and artifact-collector containers exiting zero. Shard 002 was preempted and its resource release was eventually recorded. Nevertheless the last public read at **20:40:09.473651 UTC** still reported the operation running and result unpublished. The UI reflected the stale backend state.

The observer retained the exact repeated exception: `_fence_same_workload_requeue` clears `pod_uids` but retains lifecycle evidence, causing `ValueError: Pod lifecycle evidence must uniquely bind an observed Pod UID`. This prevents reconciliation. The three public active attempts and missing final result cannot be counted as finished merely because their containers completed. See [observer incident evidence](../observer/R02-INCIDENTS.md).

### 3. Admin stopped polling before result publication

The browser lane observed a separate Protenix terminal-state/result-publication race: the first terminal state preceded artifact and semantic-validation publication, but the page stopped polling and needed navigation to expose the later artifacts. The earlier restore-duration display fix passed; this is a new, distinct issue owned by the admin lane. See [publication-race evidence](../experience/TERMINAL-PUBLICATION-RACE.md).

## Bounded stop and handoff

The confirmed permanent RF controller exception could not recover under unchanged source. The manager was inactive, and the original 7,200-second per-operation deadline would otherwise have kept the client polling until approximately 22:31 UTC. At **20:40:14 UTC**, only the exact task-owned local scientific child process was terminated; its wrapper exited **241**, and both PIDs were verified absent. This is an interrupted test, **not runner exit 0/1 or a completed aggregate**. [Stop evidence](bounded-stop-r02.json) records the action. No public cancellation, client retry, production code deployment, capacity/policy/limit change or manual workload workaround was performed.

RF server cleanup remains **incomplete**. Root must fix/deploy the reconciliation and submit-response defects or explicitly authorize cleanup of that exact operation before another cohort. The browser closed at 20:42:15.747 UTC. Cluster/normal-service observers stopped at **20:42:23 UTC**, after **129 seconds and six scheduled observation cycles** beyond the failed-cohort boundary. All local task-owned test workers are stopped. No r03 was started. Do not resume a new cohort until root confirms deployment, old task-owned operation disposition and fresh baseline readiness.

The observer's final independent counters were **77/77 ordinary requests passed** (39 HTTP, 38 MCP, including five after the scientific client stop), **78 cluster samples**, and **312/312 admin API GETs HTTP 200**. No new failed Pods, restarts or pressure were observed. Complete whole-window Qwen publication logs contained **zero route withdrawals**; one Localizing sample continued publishing the verified fixed-hot route as activatable and returned to Ready. This confirms the prior Qwen withdrawal fix on this window, but cannot make the failed scientific campaign clean. Final cluster evidence still retained the running RF operation and three Succeeded Pods; complete server cleanup is not claimed.

The offline partial exporter writes only evidence artifacts and never modifies raw receipts or sends requests. Five remediation/exporter tests plus four original campaign tests passed; scoped Ruff and `git diff --check` passed. All 53 raw-file manifest hashes and copied JSON contents were verified after export. Root owns review/commit/deployment and the final customer-readiness verdict.
