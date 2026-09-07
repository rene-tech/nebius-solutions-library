# Independent trial observation — 7 September 2026

The bounded campaign completed, but it exposed two customer-facing defects that
should be addressed before an unattended trial: an impossible BindCraft overflow
placement and an unhandled temporary MCP route-unavailable error. This was a test
and diagnosis run; no deployment, quota, capacity ceiling, policy, or permission
was changed by this observer.

## Scope and final recovery

- H100 cluster `mk8scluster-e00j5z9te7x5dd9g6a`, project `project-e00rene`,
  region `eu-north1`, explicit context `k8s-inference-h100`.
- Source baseline `b707b5e0c3fd9252892893eed0008306c21ee873`.
- Observed **14:52:28.715581–15:22:56.950623 UTC**, 74 samples at 25-second
  intervals. Final scientific completion was 15:20:44.960235; monitoring continued
  through five subsequent samples. The sampler exited cleanly and signed out.
- All 14 campaign operations terminal: **13 succeeded, one deliberately cancelled
  after a confirmed scheduling blocker**. No remaining campaign Pod or nonterminal
  operation appeared at final recovery. Public result/resource-release receipts
  are owned by the workload lane; [observation.json](observation.json) joins their
  submitted operation IDs to observed attempts, Pods, clocks, and phases.
- Background allocation returned to **13 GPUs**, the baseline. The original
  serving fleet had 13 hot models and one cold model, with zero unhealthy/unknown
  states. Scientific profiles are separate, on-demand Jobs.
- Capacity grew automatically **16 → 17 → 18 GPUs** within the existing two-slot
  preemptible pool. Peak sampled scheduler requests were 17 GPUs. The two naturally
  added nodes were left to the existing autoscaling policy, not force-removed.
- All scientific desired/inherited policy records, including the nine tested
  profiles, were byte-equivalent after canonical JSON comparison before/after.

## Availability and incidents

The four observer admin endpoints each returned 200 for all 74 reads: **296/296**.
This does **not** imply zero customer errors: MCP can return HTTP 200 containing a
tool error, and the independent customer client caught one such failure.

| Observer endpoint | p95 seconds | Maximum seconds |
| --- | ---: | ---: |
| Capacity | 0.846 | 0.991 |
| Operations | 0.928 | 1.230 |
| Workload telemetry | 1.160 | 1.251 |
| Overview | 0.859 | 0.914 |

No new container restart, failed Pod, previously-ready node failure, or baseline
serving-Pod readiness loss was **sampled**. Six historical failed Pods were present
before the trial and are excluded. New nodes' initial CNI/CSI NotReady states are
normal startup, recorded separately. Two Kueue preemptions were confirmed by
events even though their short-lived terminated Pods were not sampled as failed.

### 1. BindCraft overflow cannot fit its chosen node flavor

Operation `2ec36323-564e-4a58-9091-9c55e6e7d064`, Job
`fs2-academic-poc/fs2-design-design-000-a1-dc7900545d35`, Pod UID
`494cff16-4faf-4a6c-9972-9b13c7103f77`:

- Kueue admitted `inference-h100-1x` at **15:05:04**, fixing the Pod's pool selector.
- The admitted request was **16.1 CPUs**: 16 scientific + 0.1 artifact collector.
  The node has **16 physical CPUs and 15.9 allocatable**, before daemon overhead.
- `FailedScheduling: Insufficient cpu`; `NotTriggerScaleUp` at **15:05:35** also
  explicitly reports insufficient CPU. Another node of the same preset cannot fit
  this request. Reserved capacity later freed, but the flavor remained fixed.
- The workload owner used the public cancellation API at **15:12:46.188191** only
  after root authorization and evidence retention. There was no retry/resubmission
  or hidden success substitution. The first BindCraft run on reserved capacity
  succeeded.
- The cancelled attempt reserved **462.870473 quota GPU-seconds**, but occupied
  **zero scheduler/device GPU-seconds**. These clocks must not be conflated.

Before customer use, scientific placement must filter pool candidates using the
complete effective Pod request and allocatable/daemon overhead, and avoid trapping
admitted work on an impossible flavor. A clear public unschedulable reason and
bounded requeue/fallback path are also needed; increasing quota would not fix this.

### 2. MCP semantic error while the hot Qwen Pod remained healthy

The independent client recorded Qwen MCP request ordinal 46 beginning
**15:12:02.515582**, failing after **0.96245 seconds** without an operation ID or
automatic client retry. Subsequent independent requests passed.

The matching server window contains exactly the relevant `invoke_model` failure:
CP Pod `fs2-serve-control-plane-6c7fdcc757-pwmqv` logged at **15:12:03.426** that
`registry.get()` raised `RuntimeError: model is not routable`. `invoke_model`
catches other lookup/policy exception types, but not this domain error. The MCP
response was nevertheless **HTTP 200** at **15:12:03.429**, duration 671.507 ms.

Successful model-controller status updates and Kubernetes reads occur around this
time; there is no retained bridge-refresh exception proving the exact withdrawal
predicate. The reporting database role denied a bounded read of historical status
events. No permissions were bypassed or expanded. Therefore the precise transient
projection cause remains **unproven**; concurrent autoscaling is not claimed as its
cause. Correlation uses the client/server timestamps, not an invented request ID.

Before customer use, handle transient route-unavailable errors as a typed,
recoverable MCP response and investigate the transient route withdrawal. Logical
MCP failures need to be visible in request/error telemetry even when HTTP is 200
and admission has not created an operation record.

## Queuing, priority, and cold-node behavior that worked

**ESMFold2 overflow:** Pod unschedulable at 14:59:14; triggered existing pool
scale-up at 14:59:44; scheduled onto new node
`computeinstance-e00m9v4wt80f4zvenh` at 15:01:53. Its 3,815,828,990-byte runtime
image then pulled in **68.859 seconds**, 15:02:50–15:03:59. This was a normal
159-second scheduler/node wait followed by initialization/image preparation, not
an application failure.

**RFdiffusion four-shard bulk:** existing priority rules preempted two low-priority
attempts, then the platform retried them automatically and returned all four
successful results in **244.596 seconds**, without client resubmission:

| RF shard attempt | Event time | Higher-priority workload | Priority |
| --- | --- | --- | --- |
| `design-001`, attempt 1 | 15:13:08 | ESMFold2-Fast, Job UID `3d58ddde-f997-44c5-aff8-ba92bafac78b` | 0 versus RF −100 |
| `design-002`, attempt 1 | 15:13:42 | AlphaFold3, Job UID `f2e43eea-db65-43bc-a392-4ccbc052d3f0` | 0 versus RF −100 |

Both second attempts were admitted at **15:15:02**. Events explicitly identify
Kueue prioritization in the same ClusterQueue/cohort. This proves priority-driven
retry, **not** recovery from an actual cloud spot interruption.

**BoltzGen repeat:** the second request took 1052.889 seconds versus 819.344.
Almost all extra time came from design-folding on a preemptible node, not slower
model compute:

| Design-folding clock, seconds | Reserved first run | Preemptible second run |
| --- | ---: | ---: |
| Public stage duration | 161.996 | 395.929 |
| Quota GPU-seconds | 163.760 | 396.952 |
| Scheduler-occupied GPU-seconds | 158 | 236 |
| Application compute phase | 147 | 150 |
| Image preparation phase | 5 | 79 |
| Artifact loading phase | 5 | 6 |
| Resident-idle phase | 1 | 1 |

Second Pod created at 15:12:47, scheduled at 15:15:23 (**156 seconds waiting**).
Its 3,640,249,795-byte runtime image pulled in **74.984 seconds**,
15:15:31–15:16:46. The difference between quota and scheduler clocks includes
prescheduling and release boundaries, not an exact alternative queue stopwatch.

## GPU accounting and snapshot interpretation

All **46 campaign lifecycle subjects** ended reconciled: 44 application-observed,
two estimated preempted attempts. No final data-gap entries remained. Across those
subjects:

| Clock or phase | GPU-seconds |
| --- | ---: |
| Quota reserved | 4093.894456 |
| Scheduler occupied | 3182.644635 |
| Device allocated | 3104.410117 |
| Application active-compute phase | 2661.732065 |
| Image preparation | 258.912570 |
| Artifact loading | 229 |
| Resident idle | 33 |

Occupied non-compute phases total **520.912570 GPU-seconds**. GPU-free CPU stages
correctly contribute zero to GPU clocks. There was no observed scientific
cooldown-grace phase; completed Job resources were released. This does not measure
the cost of keeping unrelated hot serving models resident between requests.

The all-GPU 25-second DCGM sample integral was about **715 hardware-busy-equivalent
GPU-seconds**, versus about **27,197 sampled scheduler-requested GPU-seconds** for
the whole cluster, including its standing hot fleet. This approximation can miss
short kernels/requests and must not be billed, equated with lifecycle compute
phases, or attributed entirely to this customer.

| Tested profile | Unchanged startup selection during trial |
| --- | --- |
| Protenix v2 | CUDA-CRIU, `sample-structure`, bundle `protenix-v2-h100-cuda-criu-20260907-r2` |
| Proteina-Complexa, BoltzGen, Mosaic, BindCraft, RFdiffusion | Native loading |
| ESMFold2, ESMFold2-Fast, AlphaFold3 | Native loading |

Protenix's **actual** restored worker is evidenced by Pod UID
`7eb0bc7c-521d-429f-af8c-108cc73d21b7` and Loki
`scientific_snapshot_request` with mechanism `cuda-criu-restored` at
**14:56:30.040255050**. Available snapshot options for other models were not silently
enabled or represented as used. One accounting limitation remains: the durable
`restore` phase is zero even for this proven snapshot request; startup timing is
not correctly isolated into a distinct restore phase in this reporting path.

## Resource headroom and remaining measurement limits

Maximum sampled node CPU busy was **71.98%**, node memory used **24.50%**, and root
disk used **82.71%**. No pressure condition was observed. The two reserved nodes'
308.93-GiB roots retained essentially unchanged free capacity:

| Node | Baseline available GiB | Final available GiB |
| --- | ---: | ---: |
| `computeinstance-e00m0hsph76ajt9sdb` | 53.442 | 53.439 |
| `computeinstance-e00p3acr87k9k4mckj` | 59.885 | 59.881 |

The new preemptible roots retained 239.43 and 287.85 GiB available. There was no
immediate disk-pressure blocker. All metric groups returned series throughout.

Important coverage limits: all-namespace Pod/node and all-GPU DCGM sampling include
academic Jobs; periodic Kueue and per-Pod CPU/RAM queries were namespace-limited to
`fs2-models`/`fs2-system`. Academic scheduling events were captured separately.
Global node CPU/RAM includes academic activity. Historical overview reconciliation
already compared different apparent windows (durable count 4 versus Prometheus
1261); integrated DCGM GPU-seconds and TTFT were already marked unavailable. These
are pre-existing limitations, not invented zeroes or campaign-induced incidents.

## Evidence integrity

Raw evidence remains private under the H100 release's
`trial-customer-20260907/observer/`; raw credentials, API-key identities, signed
artifact URLs, and model payloads are not copied into this report.

- `samples.jsonl`: SHA256 `0161871024529d9414877342bb08d773de5e29d85c782b68c9c02d112c0bea45`
- `bindcraft2-unschedulable-r02.json`: `1c18111ed2ad9253c9d3c12bdf32bdbace29bf4c3bf1821c9865838a64e98387`
- `qwen-mcp-exception-loki-r01.json`: `956e2d94a395791dc71e45682b27a69bbe85be494c6936d65dbdfd42fd0e733d`
- `protenix-switch-loki-r01.json`: `b7f5bcd89a2ea16903ea6046b9f586aaf2432b2465cc04369b5348bfcf66d5ce`
- RF priority-event receipts: `8bc94c67d1f0d34cdb4fbd5af9e7a62c5d99ef4dd98b9d66e4f0f0206be221a3`,
  `13b40ea2c5e7f4abde59fa668391c806fbc2fc9dd6a16b6de9f462ff3bc32465`.
