# r04 cross-lane acceptance

Root verdict on September 8: **PASS for the bounded customer scenario; clean
streak 1 of 2**. R05 must independently finish on the unchanged final release
before final handoff. Prior failed cohorts remain unchanged.

Deployed source: `c85aa26e46f84ca5ae0a85b2454e86522b65cead` on the existing
H100 cluster `mk8scluster-e00j5z9te7x5dd9g6a`, project `project-e00rene`,
`eu-north1`. Exact image pins and zero-change post-apply Terraform plans are in
the [release record](FINAL-STARTUP-RELEASE-20260908.md).

| Gate | Evidence |
|---|---|
| Original scientific workload | 14/14 passed across nine model/profile variants; 963 successful HTTP calls, 14 exact replays, 28 verified downloads |
| Normal service | 73/73 complete HTTP/MCP responses passed: 5 baseline, 62 during science, 6 recovery |
| Admin APIs | 300/300 sampled GETs returned 200; no metric-query gaps |
| Real admin browser | 299 queries, 41 successful actions, 7 automatic publication/download workflows, no unexpected HTTP errors, failed reads or application exceptions |
| Priority recovery | Four-shard RF bulk completed on its original operation after two physical priority preemptions; one public attempt retry and one same-attempt Pod recreation distinguished |
| Lifecycle and cleanup | All 46 subjects terminal/reconciled/no gaps; all scientific Jobs/Pods gone, no test workers left, policies unchanged |
| Route continuity | Seven complete contiguous log windows, 13 publications, zero withdrawals; fixed-hot Qwen Pod unchanged |
| Final startup fix | Two natural Qwen bursts retained their original UIDs through waiting/loading, actual snapshot restore, Ready and idle cleanup; explicit zero demand/positive startup retention with absent Ready series beyond the former 30-second cutoff |

The scientific client window was 09:01:13.093375–09:27:30.125076 UTC.
Observation continued through 09:30:15.157985; browser closed normally at
09:30:17.667. Recovery exceeded 165 seconds with seven observer cycles and six
ordinary requests. No client retry, manual recovery, capacity-limit increase,
driver/recipe change or hidden fixture change was used.

Root checked the independent [scientific measurements](workload/REPORT-r04.md),
[observer report](observer/REPORT-r04.md), [complete-window logs](observer/r04-publication-window.json),
[ordinary traffic](experience/r04-interactive.json) and
[browser evidence](experience/R04-EXPERIENCE.md). Exported invariants independently
confirm 14 successful operations and 46 terminal/reconciled lifecycle subjects
with no gaps. Production component/stage/runtime source is unchanged since the
deployed commit; subsequent commits contain evidence and documentation only.

## Scope and remaining performance costs

This is functional evaluation readiness, not scientific efficacy, an all-GPU
qualification, a production SLO or a large-load capacity benchmark. Only actual
Protenix and Qwen restores are claimed here; other models retain their selected
conventional startup paths. Their available snapshot options require separate
qualification receipts.

Protenix's observed restore interval was 4.315065 seconds. Qwen's fresh-node
created-to-Ready times were 468 and 384 seconds, including provisioning and
roughly 163–173-second full runtime-image pulls; CUDA commands alone took
approximately 10 seconds. Those costs are not hidden behind snapshot timings.
These two natural bursts did not establish new per-Pod useful-response
attribution; earlier dedicated useful-serving evidence remains separately scoped.
A nonfatal CRIU diagnostic printed during normal teardown is preserved.

The longer second BoltzGen run completed in 1,030.785 client seconds. Its ledger
separates a fitting-slot wait and cold image pull from compute, rather than
misreporting all of it as GPU work. Campaign totals were 3,713.300425 occupied
GPU-seconds, partitioned into 2,865.684935 active-phase and 847.615490 idle seconds;
restore is included in idle, not added again.

One sampled Qwen NodePending phase occurred while the hot Pod and published
route remained available. Six old failed Pods and a preexisting Cilium operator
Pending state are retained as baseline, not erased or blamed on this campaign.
No unexpected customer-facing failure was observed.
