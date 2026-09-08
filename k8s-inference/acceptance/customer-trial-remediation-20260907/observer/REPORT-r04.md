# Customer trial r04 — observer acceptance

**Bounded PASS; first clean cohort after the final startup fix.** Root accepted the cross-lane gate on 2026-09-08. This does not replace the separately required second unchanged cohort or the earlier failed-cohort evidence.

Deployed source: `c85aa26e46f84ca5ae0a85b2454e86522b65cead`. Cluster `mk8scluster-e00j5z9te7x5dd9g6a`, project `project-e00rene`, `eu-north1`. No policies, limits, GPU/model recipes, images or infrastructure were changed during this cohort. Normal existing autoscaling and Kueue priority preemption remained enabled.

## Results and clocks

- Scientific client window: **09:01:13.093375–09:27:30.125076 UTC**; all 14 original cases passed, 963 scientific HTTP exchanges succeeded, 14 exact idempotency replays and 28 hash-verified downloads. See the [scientific report](../workload/REPORT-r04.md).
- Observation window: **08:59:21.725152–09:30:15.157985 UTC**. The startup preflight precedes the separately authorized scientific START.
- **73/73 ordinary Qwen requests passed**: 37 HTTP, 36 MCP; no submission retries. HTTP median/p95 1.946302/2.352961s; MCP 3.285815/3.827171s. These measure complete validated non-streaming responses, not TTFT or GPU decode time. Actual phase counts: five baseline, 62 during, six recovery; original phase-file label lag is retained in the raw records.
- **75 cluster samples / 300 admin GETs**, all HTTP 200. No missing metric queries, new failed Pods, new container restarts or node-pressure incidents. Six old failed Pods and the cilium-operator Pending since September 4 are excluded from campaign-caused incidents, not hidden.
- Full-window Loki capture: **seven complete contiguous query windows, 13 Qwen publication events, zero route withdrawals**, zero parse errors. Qwen's sampled model phase was Ready 74 times and NodePending once; the original fixed-hot Pod remained Ready and its route remained published. A model phase is not substituted for actual route/client health.
- All **46 scientific lifecycle subjects** ended terminal and reconciled with no accounting gaps: 45 succeeded, one preempted. All scientific Jobs/Pods were absent in the final all-namespace check. Desired scientific policies and Qwen spec matched baseline exactly.
- Final recovery extended more than 165 seconds after scientific END, with seven scheduled observer cycles and six ordinary requests. Both owned sampler sessions exited cleanly; their exact PIDs were verified absent. Autoscaled nodes were left to existing policy.

## Live startup-retention qualification

Two ordinary-traffic-triggered bursts exercised the final query on separate fresh preemptible nodes. No extra burst traffic or policy changes were introduced.

| Evidence | First burst | Second burst |
| --- | --- | --- |
| Pod UID | `facd6ab7-4838-42cd-bbb6-cf623470ecfe` | `11f08482-b34e-44ec-81ee-dd138214e754` |
| Created | 09:04:31 | 09:18:27 |
| Scheduled | 09:07:08 | 09:19:51 |
| Ready | 09:12:19 | 09:24:51 |
| Created → Ready | **468s** | **384s** |
| Cold regional vLLM image pull | 172.697s | 163.2s |
| Main container start | 09:11:17 | 09:23:47 |
| Actual restored-runtime marker | 09:12:16.776467670 | 09:24:47.486954836 |
| CRIU command / CUDA command | 48.558386s / 10.041128s | 49.512374s / 10.071678s |
| Natural container stop | 09:12:47 | 09:25:47 |

All times are UTC on September 8. CUDA command duration alone is **not** end-to-end restore or startup time; additional successful unlock commands and orchestration precede readiness. The image event's 8,634,306,308-byte size is runtime-reported image size, **not measured network bytes**. The same full regional vLLM image is needed by the init and main containers; snapshotting does not remove that fresh-node dependency.

The first Pod was explicitly retained while unscheduled and lacking any Ready metric. At 09:05:12 and again 09:06:39, actual generated queries showed **operation demand 0, startup retention 1, desired replicas 1, Pending 1, Ready series absent**. This lasted beyond both the former 30-second cancellation and the earlier off-grid test's 65-second gap. Its original UID survived scheduling, loading, actual restore and readiness. After normal idle cleanup the demand and startup metrics were both zero. The second independent activation also retained its original UID through loading and restored readiness.

Installed Loki evidence contains `serving_snapshot_runtime` with `mechanism=cuda-criu-restored`, successful CRIU/CUDA return codes, and matching Pod/node identities for both bursts. Both also print a nonfatal `CRIU_RESTORE_ERROR ... tun: Unable to create tun` diagnostic during SIGTERM cleanup: the serving entrypoint's `finally` block scans `restore.log`. This log line is retained, not suppressed or misreported as a failed readiness/restore command. No recipe change was made.

**Useful serving attribution remains separate:** direct metrics for the second Pod at 09:24:54 and 09:25:27 stayed at restored request-success baseline 2→2. The first Pod was already deleted when its direct diagnostic counter read ran; that NotFound diagnostic is retained, not counted as a public request failure. No historical vLLM request counter series or corresponding serving POST records were available to attribute ordinary requests to either natural burst. These runs prove startup retention/restore/Ready/cleanup, not new per-Pod useful-response attribution. The earlier [dedicated qualification](QWEN-STARTUP-20260908.md) retains its independent useful-serving proof.

## Scientific restore, placement and priority

Protenix operation `1b0d573b-0ce9-440c-9141-1f0156bbad36`, sample-structure attempt `022bcf95-fdd5-5869-83aa-d8afa4166b9e`: actual scientific-stage start 09:03:31 and `cuda-criu-restored` marker 09:03:35.315065492. The **4.315065s** interval agrees with the lifecycle restore ledger and admin display. Scheduler occupancy was 37 GPU-s: 17.684935 active and 19.315065 idle, reconciled without gaps. Other tested profiles kept their existing conventional startup selections; an available snapshot option is not evidence that they restored in this campaign.

Second BindCraft's full Pod requested **16,100m CPU, 98,560Mi memory and one GPU**. Its frozen reserved-only selection admitted/scheduled at 09:11:46; it reached Ready at 09:11:56 on `computeinstance-e00p3acr87k9k4mckj`. There was no long reserved-quota wait in this cohort. Both BindCraft clients passed and released resources.

The four-shard RF bulk operation `f7963270-82af-4878-8942-96bdd1841a7a` passed in **321.188s** without client resubmission. Kueue recorded two physical preemptions: shard 001 at 09:18:27 for the second Qwen burst, and shard 002 at 09:19:03 for ESMFold2-Fast. Preemptors were priority 0 versus RF −100. Shard 001 completed through public attempt 2 after released attempt 1. Shard 002's Pod was recreated and succeeded under the same public attempt 1. Therefore two physical preemptions correspond to **one public preempted attempt/retry**, not two. This was Kueue priority preemption, not a cloud spot-interruption test.

BoltzGen's second design-folding stage took 371.122s versus the first 173.137s, on different preemptible nodes. Existing phase ledgers distinguish the long tail: run 2 reserved 371.688690 GPU-s but occupied a scheduled GPU for 250s, with 90s image pull and 151s active compute; run 1 reserved 174.431114 GPU-s and occupied 170s, with 1s image pull and 159s compute. The extra fitting-slot wait and cold image dominate this stage difference, not slower model compute. Reservation-minus-placement comparisons are clock-derived approximations, not an invented exact client-wall decomposition.

## Accounting and resource headroom

Campaign-only terminal lifecycle totals: **3,713.300425 scheduler-occupied GPU-s**, **2,865.684935 active-phase GPU-s**, **847.615490 idle GPU-s**. Idle phases include image pull 565.300425, artifact loading 239, resident idle 39, restore 4.315065; unclassified time is zero.

Separately, the whole-cluster 25-second DCGM sampling integrated approximately 813.930432 busy-fraction GPU-s across 1,851.540888s of observed intervals. This includes other hot tenants and misses short activity between samples; it is **not** a substitute for campaign billing or a claim that active-phase time equals hardware utilization.

Existing autoscaling expanded GPU capacity from 16 to 18 across six to eight nodes; sampled GPU requests peaked at 18. No ceilings were raised. Node CPU/memory peaks were 77.91%/38.25%. Reserved-node minimum root-disk headroom was 56,645,894,144 and 63,514,677,248 bytes. The two newly prepared nodes retained at least 255,015,493,632 and 256,037,896,192 bytes after cache growth. No disk-pressure event occurred; the approximately 83% reserved-disk usage is reported with actual free bytes.

## Evidence

- [Allowlisted cluster/lifecycle export](r04-observation.json)
- [Whole-window route evidence](r04-publication-window.json)
- [Ordinary HTTP/MCP export](../experience/r04-interactive.json)
- Private raw root: `h100/releases/trial-customer-remediation-20260907/r04/` under the existing local acceptance state directory. `observer/run-context.json` records source, context and process identities. Original receipts, failed diagnostic reads and narrowly queried Loki logs are preserved.

This is a bounded functional/customer-workflow acceptance, not a fleet-size SLO, exhaustive snapshot qualification for every model, proof of all-GPU compatibility, or a production load-capacity claim.
