# r03 observer result — functional pass, startup gap retained

All 14 scientific requests and all 67 ordinary HTTP/MCP requests passed. All 46 scientific lifecycle subjects are terminal and reconciled, including successful automatic recovery from one priority preemption. No manual workload recovery or new failed Pod, container restart or node-pressure incident was observed.

This is **not a fully clean platform qualification**. Two short-lived unscheduled Qwen burst Pods exposed a remaining startup-retention gap, even though hot serving stayed available. The browser lane also retained two test-host network-change read failures with automatic recovery. Those results are not erased by the successful scientific requests. Root owns the corrected release and subsequent cohorts.

Deployed source throughout: `bc264980f3fedc33c8fdc6d59c93096d8db9ccaf`. Cluster `mk8scluster-e00j5z9te7x5dd9g6a`, project `project-e00rene`, region `eu-north1`. Scientific traffic ended **08:24:52.683487 UTC, September 8, 2026**. Combined observation covers **07:58:53.321536–08:27:16.747247 UTC**. Original r01/r02 failures remain separate and unchanged.

## Service, recovery and route publication

| Check | Result |
|---|---|
| Scientific requests / exact replays / verified downloads | 14 / 14 / 28; all passed |
| Ordinary HTTP | 34/34; median 2.008 s, p95 2.496 s |
| Ordinary MCP | 33/33; median 3.394 s, p95 4.039 s |
| Ordinary requests after scientific completion | 5/5 passed |
| Cluster samples / sampled admin GETs | 69 / 276; all GETs 200 |
| Whole-window Qwen publication | Six complete windows, six events, zero recorded withdrawals |
| Desired scientific policies / Qwen spec | Unchanged from baseline |
| Final task resources | No scientific-labelled Jobs or Pods remained in the final all-namespace read |

Ordinary latency is through a complete non-streaming, validated response, including session/network/admission/polling; it is not TTFT or GPU decode time. Actual timestamps classify two baseline, 60 concurrent and five recovery requests; a phase-file update lag does not rewrite their raw records.

All 69 Qwen phase point samples were Ready. The full-window logs still caught a roughly ten-second NodePending publication transition that fell between those samples: all control-plane replicas continued to publish an activatable route. This demonstrates why the point samples alone are insufficient. The [publication export](r03-publication-window.json) has no failed/truncated chunk or parse error; zero recorded withdrawals is bounded evidence, not an uptime SLA.

After scientific completion, six scheduled cluster cycles and five ordinary requests were retained; final observation ended more than 144 seconds later. Ordinary session11478/PID13979 exited cleanly with67 requests. Cluster session9116/PID13984 stopped by verified-PID SIGTERM and exited cleanly with69 samples. Both PIDs were confirmed absent. Final Qwen state contained only the original hot Pod, Ready. Naturally autoscaled nodes were left to existing policy; no forced scale-down occurred.

## Queueing and cold-node behavior

- BindCraft2 retained the full 16,100m CPU, 98,560Mi memory and one-GPU request, with reserved-only frozen affinity. It admitted/scheduled at08:08:18 and reached Pod Ready at08:08:29 on `computeinstance-e00p3acr87k9k4mckj`. Fitting reserved capacity was available immediately, so no quota-wait delay is imported from r02.
- Second Proteina received its committed operation ID normally and its exact replay returned that same ID. It passed all stages and released resources; the earlier HTTP409 remains documented in r02.
- RF shard001 triggered the existing preemptible group from one to two nodes, within its unchanged maximum, at08:18:05. New node `computeinstance-e00jvej97xtd7gtrwq` became Ready at08:19:52; shard003 scheduled there at08:20:12. Its image pull took112.910 seconds, with a runtime-reported image size of4,154,027,189 bytes. This is not measured wire traffic or GPU restore time.
- Mosaic2's first image pull on the previously prepared preemptible node took109.781 seconds, with a runtime-reported image size of4,204,444,871 bytes. Its complete customer request still passed. Existing BoltzGen stage image-cache hits are recorded separately; no entire cohort is mislabeled as fresh-node startup.
- At08:19:43, Kueue preempted RF shard002 (priority−100) for AlphaFold3 (priority0), identified by Workload UID `6f3119e6-5129-4572-92ec-cf12ce78a929` and Job UID `930d173c-732d-49cc-9908-52ba6e49a99a`. The first attempt released resources, the same operation automatically ran attempt2, and the complete four-shard batch passed in441.372 seconds. No client resubmission or controller repair occurred. This was Kueue priority preemption, not a tested cloud spot interruption.

GPU capacity rose from17 to18; maximum sampled scheduler GPU requests were17. Total node count rose from7 to9, including one new GPU node and one CPU node in an existing group. No observer lane infrastructure or limit changes were made. These counts include the preexisting serving tenants and the prepared node from the separate Qwen qualification.

## Snapshot and resource accounting

Protenix operation `1001f32b-4ebf-4f1f-98bb-137a3ed30041`, attempt `d70df43c-a406-5f4c-9349-1712a9e7c154`, has actual `scientific_snapshot_request` / `cuda-criu-restored` evidence. Its runtime container started at08:01:36Z and its restore marker occurred at08:01:40.184542014Z: **4.184542 seconds**. The ledger independently reports restore4.184542, active17.815458, scheduler-occupied35 and occupied-idle17.184542 GPU-seconds, with no gaps and zero reconciliation delta. The browser duration and automatic artifact finalization matched those boundaries.

The 46 campaign subjects total **4,122.605675 scheduler-occupied GPU-seconds**, **3,228.421133 active-phase GPU-seconds** and **894.184542 occupied-idle GPU-seconds**. Phase accounting includes620 image-pull,234 artifact-load,36 resident-idle and4.184542 restore GPU-seconds. All subjects are terminal/reconciled with zero unclassified phase total. These are lifecycle accounting intervals, not isolated hardware transfer benchmarks.

Whole-cluster point sampling separately estimates827.881 hardware busy-fraction GPU-seconds versus26,295.998 scheduler-requested GPU-seconds over1,701.360 seconds. Approximately24,794.838 allocated GPU-seconds sampled at≤1% utilization include existing hot tenants. Do not equate this coarse all-cluster measure with campaign billing or infer exact causes between samples.

No metric query was missing. CPU/RAM series include `fs2-academic-poc`, `fs2-models` and `fs2-system`. Peak node CPU was76.27%, peak node RAM24.94%. Minimum reserved-node root headroom remained56,812,900,352 and63,689,121,792 bytes. The prepared preemptible node retained at least229,869,535,232 bytes and the newly added GPU node295,750,033,408 bytes as model images/cache populated. No disk pressure or unexplained material reserved-node growth was observed.

Scientific startup policy selection remained unchanged: Protenix sample-structure used its qualified CUDA/CRIU bundle; the other tested scientific profiles had no explicitly selected snapshot startup backend. Available options are not counted as actual restoration. The separate useful Qwen snapshot qualification is [documented independently](QWEN-STARTUP-20260908.md).

## Open qualification item and exports

The [unscheduled-startup repair note](QWEN-UNSCHEDULED-STARTUP-20260908.md) preserves the two premature deletions, absent Ready metrics, the off-grid activation-clock gap, failed intermediate candidates and subsequent offline/historical proof. Corrected historical values are not relabeled as a successful live r03 burst. The [browser diagnosis](../experience/BROWSER-NETWORK-20260908.md) and browser lane report retain the automatically recovered test-host read failures.

Reproducible exports: [cluster observation](r03-observation.json), [ordinary requests](../experience/r03-interactive.json), [whole-window publication](r03-publication-window.json), and [scientific results](../workload/REPORT-r03.md). Private context, lifecycle, event, log and resource receipts remain under `trial-customer-remediation-20260907/r03/observer/`; credentials and signed URLs are not exported. No further cohort started from this observer without root authorization.
