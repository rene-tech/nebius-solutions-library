# First trial-customer simulation — 7 September 2026

Status: **complete; not ready for unattended customer use**. Scientific campaign:
13 passed / 1 scheduler-blocked and cancelled. Recovery checks passed. No
production fix was deployed; this handoff reports the failures rather than
claiming the platform is finished.

This tests platform usability for the proposed cancer-immunotherapy customer's
model mix, not scientific usefulness, biological efficacy or clinical validity.
All scientific inputs are existing model-owned synthetic acceptance fixtures.

## Agreed scope

- Fourteen scientific operations across nine requested model/profile IDs: two
  each of Proteina-Complexa, BoltzGen, Mosaic, BindCraft and RFdiffusion, and one
  each of ESMFold2, ESMFold2-Fast, Protenix v2 and AlphaFold3.
- A sequential RFdiffusion → Protenix → Mosaic switch, then eleven mixed-batch
  operations with at most four concurrent clients. One existing RF request
  fans out to four independent designs. Client-slot waiting is separate from
  accepted server/Kueue queueing; fourteen operations are not submitted at once.
- Continuous low-rate interactive Qwen requests over HTTP/MCP, public discovery
  and documented readiness checks; real browser observation of operations and
  results; independent Kubernetes/Prometheus/Loki observation.
- Exact submission replay, public result validation, hash-verified artifact
  retrieval, lifecycle/resource release and final return to the original
  configuration. No hidden retry, policy change or operator repair.

The retained H100 cluster is `mk8scluster-e00j5z9te7x5dd9g6a`,
`project-e00rene`, `eu-north1`. Source baseline is main
`b707b5e0c3fd9252892893eed0008306c21ee873`, with control-plane runtime built from
`0c1c6f9e2` (`fd7e0c0a…`) and unchanged admin image `75b9f702…`.
The two reserved nodes provide 16 H100 GPUs. The existing preemptible pool
starts at zero, may grow automatically within its unchanged maximum of two,
and is not manually enlarged. Other models keep their original hot floors.

## Evidence and interpretation

- [Scientific workload and reproduction](workload/README.md).
- [Independent cluster observation](observer/README.md).
- [Interactive and browser experience](experience/README.md).
- [Concrete usability findings](findings.md), kept separate from inference
  failures and from limitations of the test harness.

Private raw receipts, timestamps and browser screenshots are retained under
`h100/releases/trial-customer-20260907/` outside the repository. Credentials,
cookies and signed download URLs must not be copied into this report.

Public scientific requests use the existing academic tenant token, while
normal serving requests use the ordinary serving token. The browser uses an
administrator session: it tests operator-assisted management, not a newly
onboarded customer-scoped login. This is a modest-load rehearsal using known
valid examples, not an unaided onboarding study, saturation test or SLA proof.
Per-model samples are one or two; end-to-end times include queueing and polling
and must not be presented as cold-start or GPU-kernel durations.

## Scientific results

The campaign ran from **14:54:07.467563 to 15:20:44.960235 UTC** (26m37.493s).
All nine requested model/profile IDs produced validated results at least once.
Every successful operation passed the existing semantic oracle and hash-verified
downloads of its output manifest and one bounded result artifact. All fourteen
submissions passed exact same-operation idempotent replay. The second BindCraft
operation did not complete: it was unschedulable and needed explicit cancellation.
The runner deliberately exited nonzero rather than hide that failure.

These are **client end-to-end seconds**, including input upload, submit/replay,
server queueing, execution, five-second polling granularity and output retrieval.
They exclude time waiting for a slot in the four-client test executor and are
not model cold-start durations or GPU-kernel benchmarks.

| Model/profile | Successful client times (seconds) | Outcome |
| --- | ---: | --- |
| Proteina-Complexa | 257.007 / 255.868 | 2 passed |
| BoltzGen | 819.344 / 1052.889 | 2 passed |
| Mosaic | 114.191 / 114.096 | 2 passed |
| BindCraft | 497.107 | 1 passed; repeat blocked, cancelled after 468.237s client time |
| RFdiffusion | 97.679 / 244.596 | 1 single-design + 1 four-shard batch passed |
| ESMFold2 | 365.903 | Passed, including a cold preemptible-node start |
| ESMFold2-Fast | 92.583 | Passed |
| Protenix v2 | 75.559 | Passed using CUDA/CRIU restore |
| AlphaFold3 | 76.308 | Passed |

The [measurements](workload/results-r01/measurements.json) also retain exact
accepted-to-result, local client-slot wait, stage attempts, execution identities
and individual request clocks. The 969 instrumented scientific HTTP calls all
returned 200/201/202, **but one operation still failed to make progress**. The
separately recorded public cancellation is not hidden in those success-status
counts. HTTP success alone is not workload success.

## Batching, priorities, elasticity and startup

- Sequential RFdiffusion → Protenix → Mosaic switching required no operator
  action. Mixed scientific operations ran alongside ordinary Qwen traffic.
- RF's lower-priority bulk request had four final successful inference shards.
  ESMFold2-Fast and AlphaFold3 (priority 0) each preempted one bulk attempt
  (priority −100). Both recovered as attempt 2: six GPU attempts, four final
  shard results, one public operation and no client resubmission. This tests
  Kueue priority recovery, **not** actual cloud spot-node reclamation.
- Existing autoscaling grew the cluster from 16 to 18 H100 GPUs using its two
  permitted one-GPU preemptible nodes. No node ceiling, quota or policy was
  raised. Peak sampled GPU requests were 17; sampling is not an exact peak bound.
- ESMFold2 waited 159s for scheduling/new capacity; its new-node runtime image
  pull took 68.859s for approximately 3.816GB. Expected cold capacity delay is
  distinct from BindCraft's impossible node fit.
- BoltzGen's second run was 233.545s slower. Its design-folding stage accounted
  for almost all of that difference: roughly 155s extra before scheduling and
  74s extra image preparation; application active-compute phase was 150s versus
  147s. This does not establish slower GPU kernels on the preemptible node.
- Only Protenix selected a snapshot policy in the unchanged nine-profile setup.
  Its exact run logs establish `scientific_snapshot_request` with
  `mechanism=cuda-criu-restored`. The other eight profiles used normal policies;
  their available snapshot options were not exercised by this campaign.

## Customer experience: 6/10 overall

The model/API workflow is useful for an **operator-assisted pilot**, but it is
not yet a smooth unattended service. All requested profiles worked; switching,
results, asynchronous progress, priority recovery and bounded elasticity worked.
However, I was blocked once by an impossible BindCraft placement and had to
cancel it. Ordinary MCP inference also failed once. The browser could not
explain the scheduling blocker or download completed results directly.

- **71 ordinary Qwen requests:** HTTP 36/36 passed; MCP 34/35 passed. No inference
  retry hid the MCP failure. Seventy successful response times ranged from
  1.282s to 4.142s; their descriptive p95 was 3.682s. These are complete-response
  clocks, including client/protocol setup and polling, not TTFT or token speed.
- **Five scientific MCP readbacks passed:** catalog plus status/result for two
  already-completed scientific operations. Scientific submissions themselves
  used HTTP; this does not claim all nine profiles were submitted through MCP.
- **296 observer admin reads returned HTTP 200** across 74 cycles. The MCP
  semantic failure is retained separately; successful transport does not imply
  successful work.
- The real browser exercised an existing **admin** login and known valid
  examples, not a new customer's API-key onboarding or unaided input preparation.
  Operator-console usability is **4/10** until the documented status, live
  refresh, download and accounting defects are corrected.

The test-harness mistakes are retained separately: an unsupported `/healthz`
probe, an initially inappropriate discovery credential scope, and a mistyped
browser operation URL. They are not counted as platform outages. The failed
Qwen MCP call, in contrast, was correlated to an actual server-side exception.

## Before allowing unattended customer trials

1. **Fix complete-Pod pool eligibility.** BindCraft's 16,100m CPU request was
   admitted to a node shape with only 15,900m allocatable CPU. The scheduler and
   autoscaler could not resolve it, even when reserved capacity became free.
   Select a fitting pool and provide an explicit recovery/error path; increasing
   the number of identical small nodes cannot fix this case.
2. **Fix transient MCP route loss/error handling.** One ordinary Qwen call hit a
   confirmed server-side `model is not routable` exception while returning MCP
   transport HTTP 200. Later requests recovered. The precise route-withdrawal
   trigger is not proven and must not be attributed to autoscaling by assumption.
3. **Finish the operator experience.** Stuck work looked like `running / No
   error`; live progress needed a refresh; completed artifacts had no download
   action and were labeled stale; snapshot and GPU-accounting displays were
   inconsistent. Correct these joins/actions before presenting self-service as
   complete. [Evidence and source diagnosis](findings.md).

No architecture replacement is indicated by this modest test. Keep the existing
asynchronous operations, Kueue and elastic pools; fix per-node placement and
route continuity first. Image prewarming on new nodes is a targeted performance
follow-up, with storage headroom considered. Do not confuse regional registry
mirroring with an image already present on a newly provisioned node.

The [final public inventory](final-inventory.json), observed at 15:21:16 UTC,
passed discovery/configuration checks for all 24 required models (GLM excluded).
It still labels BindCraft `batch-ready`; this is a capability/qualification
projection, **not** proof that every permitted pool can run its current Pod.
Other models were not all re-benchmarked in this nine-profile customer simulation.

## Recovery, accounting and handoff

Observation continued through **15:22:56.950623 UTC**, more than two minutes
after the scientific campaign finished. All fourteen operations were terminal,
no campaign Pods remained, and every successful/preempted/cancelled attempt
reported released resources. Scientific policies matched their baseline.
The final three labeled interactive recovery probes passed. All three lanes'
test samplers/browser/client processes were stopped; no test work was left running.
Autoscaled nodes were left to the existing scale-down policy, not manually
deleted. The original serving fleet remained in place.

All 46 scientific lifecycle subjects reconciled: 44 application-observed and
two estimated preempted attempts. This is useful accounting, but not complete
hardware-busy attribution: `active_compute` is an application phase, not kernel
execution. Restore phase totals were zero **despite the proven Protenix restore**;
that phase classification still needs correction. Overview integrated GPU time
and some per-run summary fields were unavailable. Do not use these UI displays
as a complete customer-billing or snapshot-performance ledger yet.

Validation: **20 offline tests passed** (16 trial-harness tests plus four reused
scenario-client tests), Ruff passed using the control-plane environment, Node
syntax passed, and exported JSON was checked for credential fields. Meaningful
test code, redacted receipts, exact failure evidence and reports are retained on
the existing solutions-library `main` branch; no production code, Terraform,
limits, security policy or deployment was changed by this simulation.
