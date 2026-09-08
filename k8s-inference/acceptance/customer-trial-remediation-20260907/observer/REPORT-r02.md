# r02 observer handoff — failed cohort, management must resume

**Not ready for a 10/10 customer verdict.** The scientific client campaign ended with 12 passed cases, one HTTP-409 failure, and one blocked/stopped RF bulk case. The manager was idle when the permanent controller error was diagnosed. Only exact local test processes were stopped; no public cancellation, production repair, policy change, or capacity-limit increase was performed.

Deployed runtime source: `5f5061b28ee71a59432492a1bdf6106428a85367`. Cluster: `mk8scluster-e00j5z9te7x5dd9g6a`, `project-e00rene`, `eu-north1`. Observation: **20:10:14.931204–20:42:23.253060 UTC**, 2026-09-07. Scientific clients started 20:10:47.879537Z and were stopped at 20:40:14Z after the demonstrated blocker. That last timestamp is **not scientific server completion**.

## Outcome and remaining work

- Proteina's second submit returned 409 without an operation ID although server work was persisted. Exact reconstructed-request/idempotency-key equality proves ownership of operation `d6ddbe20-6c2b-4c2e-ab65-03442c9c1b82`. It later succeeded and released all four attempts, but the customer request remains failed.
- RF four-shard operation `daae227f-f7a0-4fe7-9912-1a95c675c3d9` remains running in the API. Three Jobs/Pods finished successfully but were not reconciled/released by the scientific controller. The fourth attempt was preempted and its Job/Pod removed. The same-workload requeue fence clears `pod_uids` without clearing/reconciling corresponding lifecycle evidence, raising `ValueError: Pod lifecycle evidence must uniquely bind an observed Pod UID` repeatedly. Root owns repair and cleanup; no next cohort was started.
- The browser lane found a separate terminal-before-artifact-publication polling race and retained three recovered browser `ERR_NETWORK_CHANGED` read failures. Its source changes and evidence are separate. The earlier Protenix false-zero restore-duration issue is fixed and passed live.

Exact incidents and raw receipt hashes are in [R02-INCIDENTS.md](R02-INCIDENTS.md). Scientific results/HTTP/download evidence are in [the workload report](../workload/REPORT-r02.md). These failures must remain visible when repeating acceptance.

## Normal service and route publication

| Check | Result |
|---|---|
| Ordinary traffic | 77/77 passed, no submission retries |
| HTTP | 39/39; median 1.936 s, p95 2.220 s |
| MCP | 38/38; median 3.340 s, p95 3.803 s |
| Ordinary requests after local scientific stop | 5/5 passed |
| Cluster samples / sampled admin GETs | 78 / 312; all admin GETs 200 |
| New failed Pods / container restarts / pressure incidents | None observed |
| Qwen phase point samples | 77 Ready, one Localizing |
| Whole-window Qwen publication | Seven contiguous query windows, 11 events, no withdrawal recorded |

The complete-window Loki query includes all CP replicas and has no failed/truncated chunk or parse error. At 20:39:26, Qwen was briefly Localizing while its original hot Pod remained Ready; the CP continued to **publish an activatable route**, then returned to Ready about ten seconds later. Another short NodePending publication transition is retained. Neither is misrepresented as a route withdrawal. All original hot-Pod identities remained stable. This is bounded evidence, not an uptime SLA or proof every possible transition was exercised.

Natural Qwen burst creation, scheduling and image pulling occurred. One fresh-node burst pulled 8,634,306,308 bytes in 154.835 seconds before it was stopped by the existing lifecycle at 20:25:08Z. It did not reach a verified successful restore/Ready state; no new Qwen snapshot qualification is claimed. The sampler cadence was unchanged at 25 seconds, with no extra stress run.

## Queueing, elasticity and cold loading

- BindCraft2 retained CPU16 plus the 100m collector and a reserved-only fitting affinity. Kueue explicitly rejected the 1x flavor on affinity and waited **196 seconds for reserved GPU quota**. It admitted/scheduled at 20:24:56Z on `computeinstance-e00p3acr87k9k4mckj`; runtime started 20:25:05Z using its cached image. The full request succeeded in 590.306 seconds without intervention.
- Existing preemptible autoscaling added two 1xH100 nodes, reaching the existing maximum of two. Total GPU capacity rose **16 → 17 → 18** and maximum sampled GPU requests reached 18. No ceiling or hot-floor changes were made.
- ESMFold2 triggered scale-up at 20:16:20Z and scheduled on `computeinstance-e00eak8d36ydt9fzne` at 20:18:28Z: **128 seconds from trigger to scheduling**. Its 3,815,828,990-byte image pulled in **67.068 seconds**; runtime started 20:20:33Z. Artifact verification took 40 seconds on this fresh node.
- Mosaic2 ran on the second new node, `computeinstance-e00y8jgm8j6131408t`, Ready at 20:27:00Z and Pod scheduled at 20:27:13Z. Its 4,204,444,871-byte image pulled in **98.527 seconds**. The complete request took 315.828 seconds. These are actual cold-node/image costs, not GPU-snapshot restore durations.
- RF low-priority shards (-100) were preempted by priority-0 Qwen burst (002, 20:31:46Z), ESMFold2-Fast (003, 20:31:48Z), and AlphaFold3 (000, 20:31:59Z). Events prove prioritization, but the controller defect prevented successful end-to-end recovery. Same-a1 replacement Pods are not relabeled as successful application attempt-2 retries. This was Kueue preemption, not a tested cloud spot interruption.

## Snapshot policy and accounting

All scientific **desired policies** are byte-equivalent after canonical projection to baseline. Runtime running-count fields differ because RF is stuck; those counts are not policy changes. Of the nine tested profiles, only Protenix sample-structure selected `cuda-criu`, bundle `protenix-v2-h100-cuda-criu-20260907-r2`; the other tested profiles had no explicit snapshot startup selection. Available options alone are not evidence of actual restoration.

Protenix operation `4a4a2555-3901-4193-8a65-78ff01737af3`, attempt `1a9f311d-54d6-5d58-9e59-b58410752873`, emitted `scientific_snapshot_request` with `mechanism=cuda-criu-restored` at **20:13:10.196746735Z**, paired with its runtime container start at 20:13:06Z. The ledger reports **4.196746 restore GPU-seconds**, 16.803254 active GPU-seconds, 35 scheduler-occupied GPU-seconds and 18.196746 occupied-idle GPU-seconds, with no gaps and zero reconciliation delta. Browser showed 4.2 seconds. This is the startup/restore interval defined by those boundaries, not an isolated CUDA-memory-transfer benchmark.

The point-sampled accepted-receipt export contains 40 lifecycle subjects; the explicitly associated rejected-submit Proteina supplement adds four. Of these 44 observed subjects, 41 are terminal/reconciled and three RF subjects remain incomplete. **Do not interpret their zero/open-interval rollups as zero resource use.** Totals only for the 41 terminal/reconciled observed subjects are 3,679.040227 scheduler GPU-seconds, 2,858.803254 active-phase GPU-seconds, and 820.236973 occupied-idle GPU-seconds. This is not a complete or final bill for the failed cohort.

Separately, the all-node DCGM point-sample integral is approximately 863.648 hardware busy-fraction GPU-seconds versus 29,498.559 scheduler-requested GPU-seconds over 1,926.546 seconds of coverage. Approximately 27,947.381 allocated GPU-seconds sampled at ≤1% utilization include the existing hot co-tenant models. These coarse whole-cluster measurements are not equivalent to the campaign lifecycle ledger or billable GPU hours. Short GPU bursts between samples can be missed.

CPU/RAM series actually include `fs2-academic-poc`, `fs2-models` and `fs2-system`; no missing query samples were observed. Node busy CPU peaked at 75.8%, node RAM at 24.3%, and sampled Pod working set at 33.20 GiB. Reserved-node minimum root headroom remained **57,015,410,688 bytes** (`...m0hs...`) and **63,894,863,872 bytes** (`...p3acr...`), despite ~83% utilization on the fuller disk. Fresh nodes fell from 309.08/309.31 GB available to minimum 242.73/282.78 GB as images/cache populated. No pressure or unexplained material reserved-node disk growth was observed.

## Final resources, processes and artifacts

After the failed-client boundary, six scheduled cluster samples and five ordinary requests were retained; final observation ended **129 seconds later**. This is a post-failure service observation window, **not clean scientific recovery**. At the final sample, RF still had three Succeeded Pods and a running API operation; known preexisting failed Pods and old pending cilium were excluded from campaign incidents.

Ordinary sampler session58985/PID4056515 stopped cleanly with77requests. Cluster sampler session7275/PID4056478 stopped by verified-PID SIGTERM with78samples. Both PIDs were confirmed absent. No observer-owned traffic or r03 process remains, and naturally autoscaled capacity was left to existing policy.

Safe reproducible exports:

- [r02-observation.json](r02-observation.json): exactly the13 genuine submitted-receipt operation IDs, including the incomplete RF operation.
- [r02-associated-proteina-operation.json](r02-associated-proteina-operation.json): the separately verified persisted operation behind the409; no synthetic successful submission.
- [r02-publication-window.json](r02-publication-window.json): full-window query coverage and per-CP publication events.
- [r02-interactive.json](../experience/r02-interactive.json): every ordinary request, failures/retries preserved; `scientific_completed_at=null` and explicit failed-client stop time.

The experience exporter now accepts a mutually exclusive `--scientific-client-stopped-at` boundary so aborted traffic is not labeled scientific completion. Its two offline regression tests pass. Private credentials and raw signed/log payloads remain outside git. Root must resume, integrate the scoped pending fixes, repair/reconcile the retained RF work, commit/push the meaningful evidence, and deploy before authorizing a new unchanged full cohort. Two consecutive clean cohorts have **not** been achieved.
