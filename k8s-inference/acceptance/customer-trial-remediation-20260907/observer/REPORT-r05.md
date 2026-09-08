# Customer trial r05 — observer acceptance

**Bounded PASS; root accepted the combined scientific/browser/observer gate.** This is the second consecutive unchanged clean cohort after [r04](REPORT-r04.md), on deployed source `c85aa26e46f84ca5ae0a85b2454e86522b65cead`. Earlier failed cohorts remain documented and are not relabelled as passes.

Cluster `mk8scluster-e00j5z9te7x5dd9g6a`, project `project-e00rene`, `eu-north1`. No model policies, hot floors, resource ceilings, images, runtime recipes or infrastructure were changed during this cohort. The original 14 scientific fixtures, maximum four scientific clients, and alternating 25-second HTTP/MCP sampler were unchanged.

## Customer availability and recovery

- Scientific window: **09:34:48.229446–09:56:40.942742 UTC, September 8**. All 14 clients passed and all resources released; see the [scientific report](../workload/REPORT-r05.md).
- Observation window: **09:32:20.748152–09:59:03.993337 UTC**. The preceding startup preflight is separate from scientific START.
- **64/64 ordinary Qwen requests passed**, evenly split between HTTP and MCP, without submission retries. HTTP median/p95: **1.962435/2.489732s**; MCP: **3.349348/4.036406s**. These are complete validated non-streaming response times, not TTFT or GPU decode measurements. Exact timestamp phases contain six baseline, 52 during, six recovery requests; original phase-file label lag is retained.
- **65 cluster samples / 260 admin GETs**, all HTTP 200; no missing metric queries, new failed Pods or new container restarts. Qwen's sampled model phase was Ready in all 65 samples; the original fixed-hot Pod UID `2c04bb56-77ea-4838-8198-256330c55b07` stayed Ready with zero restarts.
- Whole-window Loki evidence has **six complete contiguous windows, six publication events, zero withdrawals and zero parse errors**. The point sampler alone is not used to claim continuous route availability.
- All **46 scientific lifecycle subjects** are terminal and reconciled without accounting gaps: 45 successful and one preempted. Final all-namespace scientific Jobs/Pods were empty. All ten desired scientific policies and the Qwen spec match baseline exactly.
- Recovery exceeded **142 seconds**, with six scheduled cluster cycles and six successful ordinary requests. Both owned samplers exited zero; PIDs `831056` and `831558` were verified absent. No further traffic was started, and existing autoscaling was left untouched.

Six historical failed Pods and the cilium-operator Pending since September 4 remain baseline findings, not campaign failures. One formerly idle preemptible node, `computeinstance-e00nxqrd9fzhb06jgs`, transitioned to `NodeStatusUnknown` at 09:37:20 and subsequently disappeared. Replacement node `computeinstance-e00ferc34ygwj58q2m` joined later. The observed change did not cause a failed request or lost campaign attempt. Sampled Kubernetes status does **not** establish its cloud-side removal cause; this is not described as either a proven cloud preemption or the separate Kueue priority event below.

## Actual restore and placement evidence

Protenix operation `2750e2dc-c91f-42cf-a64f-fe8ba4f10c34`, attempt `2ea7c586-4688-5175-9bf4-e0a40354a681`, started its scientific container at **09:36:52**. The actual `cuda-criu-restored` marker occurred at **09:36:56.594503293**, giving **4.594503s**, independently matching the lifecycle ledger and admin display. Occupancy was **33 GPU-s**, split into 15.405497 active and 17.594503 idle, including that restore interval. The complete public client request took 64.934s: the restore interval is not substituted for the whole workflow. Other scientific profiles kept their previous conventional startup selections; available snapshot options are not proof of use.

Second BindCraft requested **16,100m CPU, 98,560Mi RAM and one GPU**, including its collector. Reserved-only admission at 09:43:33, scheduling at 09:43:34 and readiness at 09:43:44 placed the complete Pod on `computeinstance-e00p3acr87k9k4mckj`. There was no long reserved-quota wait in this cohort; the client passed in 496.081s and released resources.

The four-shard RF batch `d0bf3a01-d206-4fc4-bd65-0caefc784409` passed in **294.601s**. Kueue preempted shard 003 at **09:53:16**, stopping its Job at 09:53:17, to admit AlphaFold3 `cd8e0e8c-9b10-4a9f-9f62-cb1c5a063a07`: priority 0 versus RF −100. The exact preemptor Workload UID `873e3dab-c98f-444f-966b-016c70f58d90` and Job UID `00558b0a-6278-409f-a62f-26674f2795b7` match sampled Pod labels. Released attempt 1 recovered automatically through attempt 2, with no client resubmission or manual intervention. This is Kueue priority recovery, not a cloud spot-interruption test.

## Natural Qwen cached-node startup

One naturally triggered burst used already-prepared node `computeinstance-e00dqrjnkqna1k6dtm`. Pod UID `b115b9a0-4df3-4d61-8f4a-641e11fc3632` was created **09:38:22**, started its main container **09:38:28**, emitted the actual restored-runtime marker **09:39:16.896898202**, and became Ready **09:39:22**: **60 seconds from Pod creation**, on a cached node, not a fresh-node cold-start claim.

Regional vLLM/tools image events were cache hits. The successful CRIU and CUDA command durations were **37.867479s and 9.958064s**, plus short successful unlock commands and orchestration. CUDA-only time is not the overall restore. The same original Pod reached readiness and was naturally stopped at **09:40:17**. Existing startup retention remained effective while demand was zero; the corresponding metric returned zero after cleanup. Final missing-Ready/fresh-node coverage belongs to r04's two independent live activations, not an invented repeat here.

The retained runtime Loki query covers 09:38–09:40 and proves actual restore; it does not cover the later cleanup log tail. A direct request-counter diagnostic arrived after deletion and returned NotFound, retained privately rather than counted as a public request failure. No new per-Pod useful-response attribution is claimed for this natural burst. The separate [dedicated qualification](QWEN-STARTUP-20260908.md) preserves its own useful-serving evidence.

## Accounting and headroom

Campaign-only lifecycle totals: **3,652.079547 scheduler-occupied GPU-s**, **3,057.485044 active-phase GPU-s**, **594.594503 idle GPU-s**. Idle components are image pull 318, artifact loading 235, resident idle 37 and restore 4.594503 GPU-s; unclassified time is zero.

Separately, whole-cluster 25-second DCGM sampling integrated approximately **797.636911 busy-fraction GPU-s** over 1,601.331574s of observed intervals. It includes other hot tenants and misses activity between samples; it is not campaign billing and does not equate active-phase time with actual GPU compute utilization.

Capacity ranged from **17 to 18 GPUs / seven to eight nodes**, with requested GPUs peaking at 18. CPU and RAM peaks were **77.14% / 38.26%**; no node-pressure event occurred. Reserved-node minimum root-disk headroom was **56,643,411,968 / 63,509,426,176 bytes**. The prepared dqrjn node retained at least 242,849,124,352 bytes after roughly 13.3GB additional cache growth; the new ferc node retained at least 295,612,911,616 bytes. Disk percentages are not presented without absolute headroom.

## Evidence and limits

- [Allowlisted cluster/lifecycle export](r05-observation.json)
- [Complete publication-window export](r05-publication-window.json)
- [Ordinary HTTP/MCP export](../experience/r05-interactive.json)
- Private raw root: `h100/releases/trial-customer-remediation-20260907/r05/` under the existing local acceptance-state directory. Exact context/process identities, final cleanup, failed diagnostic read, Job events and runtime receipts remain there.

This is bounded customer-workflow acceptance, not an exhaustive model snapshot matrix, a fleet-scale SLO, all-GPU compatibility proof or a maximum-throughput benchmark. No credentials or signed result URLs are included in these exports.

The three focused observation/publication/ordinary exporter suites passed **19 tests**; whitespace validation and the credential-value/signed-URL scan passed. No new permissions were needed. Pytest reported unrelated historical temporary PostgreSQL-directory cleanup warnings after its passing result; those paths were not modified by this task.
