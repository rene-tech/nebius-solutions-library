# Customer evaluation handoff — September 8, 2026

**Ready for the bounded customer evaluation.** Two consecutive complete,
unchanged customer cohorts passed after the final deployed fix. No customer
request failed, no manual recovery or hidden submission retry was used, and
ordinary service remained available during scientific batching and priority
preemption. Customer-flow score: **10/10 against this functional acceptance
scenario**, not a universal availability, performance or scientific-quality claim.

The retained H100 cluster is `mk8scluster-e00j5z9te7x5dd9g6a` in
`project-e00rene`, `eu-north1`. Deployed source is
`c85aa26e46f84ca5ae0a85b2454e86522b65cead` on
`rene-tech/nebius-solutions-library` main. Later commits contain documentation
and evidence, not production changes. [Exact images, tests and Terraform
convergence](FINAL-STARTUP-RELEASE-20260908.md) are recorded separately.

## Start using the cluster

- [Admin console](https://89.169.99.188/admin/)
- [MCP endpoint](https://89.169.99.188/mcp)
- [Inference API base](https://89.169.99.188/v1)
- [Scientific model discovery](https://89.169.99.188/v1/scientific-models)
- [Grafana operator entry](https://89.169.99.188/admin/observability/grafana)

Use the credentials from the protected Terraform access bundle. The admin
bootstrap credential and inference/scientific PATs are distinct; the admin
credential is not an inference token. Issue customer-scoped keys through
**Access / API keys**, and use **Scientific runs** to observe and configure
dispatch. Do not give customers the bootstrap operator credential. No token,
cookie or signed download URL is included in this handoff.

The [scientific API quick start](../../docs/SCIENTIFIC_BATCH_API.md) explains
discovery, input upload, submission, polling and result download over HTTPS/MCP.

The final public inventory check again retained all **24 configured models**:
[inventory receipt](final-inventory.json). This is a discovery/configuration
check, not a claim that this loop newly benchmarked all 24 models. GLM remains
excluded from this H100 customer scenario.

## What passed

The nine trial variants were Proteina-Complexa, BoltzGen, mosaic, BindCraft,
RFdiffusion, ESMFold2, ESMFold2-Fast, Protenix v2 and AlphaFold3. Both runs used
the original fixtures, parameters, runtime identities, priorities and maximum
four concurrent clients. Each run included sequential model switching and a
real four-shard server-side RF batch. Waiting for a local client slot is
reported separately from server queueing.

| Acceptance measure | r04 | r05 | Combined |
|---|---:|---:|---:|
| Successful scientific operations | 14/14 | 14/14 | 28/28 |
| Successful scientific HTTP exchanges | 963 | 894 | 1,857 |
| Same-operation idempotency replays | 14 | 14 | 28 |
| Hash-verified scientific downloads | 28 | 28 | 56 |
| Successful ordinary HTTP/MCP requests | 73/73 | 64/64 | 137/137 |
| Successful sampled admin API reads | 300/300 | 260/260 | 560/560 |
| Real browser queries | 299 | 272 | 571 |
| Automatic browser publication/download gates | 7 | 7 | 14 |
| Unexpected browser failures | 0 | 0 | 0 |
| Terminal, reconciled scientific lifecycle subjects | 46/46 | 46/46 | 92/92 |
| Complete contiguous route-log windows | 7 | 6 | 13 |
| Recorded serving-route withdrawals | 0 | 0 | 0 |

Scientific windows were 09:01:13.093375–09:27:30.125076 UTC for r04 and
09:34:48.229446–09:56:40.942742 UTC for r05. Ordinary traffic includes explicitly
separated baseline, active-workload and recovery phases. Both cohorts continued
past completion for more than 90 seconds and at least four observation cycles;
r05's complete route window ends at 09:59:03.993337 UTC.

Both RF batches recovered automatically on their original operation IDs after
higher-priority work displaced shards. The reports distinguish public attempt
retries from physical Pod recreation, and do not call Kueue priority preemption
a cloud spot-interruption test. All required artifacts validated, all scientific
Jobs/Pods disappeared, and all terminal accounting reconciled without gaps.

R05 reproduced the repaired admin publication race: an automatically polled
completed run initially had zero artifacts, then published eight artifacts and
passed validation on the next automatic response, 5.247 seconds later. No
navigation or reload was used to recover it; polling stopped after publication.
Stage, queue, loading and retry progress remained visible.

## Fixes and verification

The integrated changes correct committed-admission HTTP 409 responses, RF
preemption finalization/retry ownership, impossible full-Pod placement,
premature admin result-polling termination, misleading restore-phase clocks,
hot-route withdrawal during burst scale-down, and premature cancellation of
starting/unscheduled Qwen bursts. Existing model recipes and resource ceilings
were preserved.

The final release passed 1,633 non-external backend tests, with four optional
skips and 77 external-service deselections. Focused startup tests, actual
Prometheus expression execution, independent off-grid timing checks and live
GPU restore evidence supplement that suite. Existing UI coverage passed 164
tests; final harness/export suites passed 9 workload, 13 browser-helper and
19 observer tests. Counts overlap and are not added into a fabricated total.

Terraform deployed the exact source images, and all three post-apply stages
reported zero managed changes. Final application readback showed API 3/3,
admin 2/2 and model controller 2/2 updated/ready/available, with observed
generations, zero terminating replicas and the expected immutable image pins.
The API replica change is normal existing autoscaling, not a changed setting.
No GPU quota, node-group ceiling, startup budget, driver or model policy was
raised or changed during acceptance.

The separate [September 6 live access/policy acceptance](../scientific-fleet/evidence/customer-access-policy-h100-8bb53aab-20260906.md)
retains real browser key creation, scoped discovery, denied out-of-scope use,
revocation, pause/resume and one-at-a-time dispatch tests, with effective policy
restored afterward. The [September 8 configuration check](experience/POST-RELEASE-ADMIN-CHECK-20260908.md)
retains the reversible cold-only setting test. These are explicitly dated
evidence, not new key/config mutations smuggled into the unchanged cohorts.

## Performance and scope boundaries

- Protenix actually used CUDA-CRIU snapshots: observed restore intervals were
  **4.315065s** and **4.594503s**. End-to-end client times were 70.582s and
  64.934s; the two clocks are not interchangeable.
- Two fresh-node Qwen bursts reached Ready in **468s and 384s**, including node
  provisioning and **163–173s runtime-image pulls**. A naturally cached-node
  activation in r05 reached Ready in **60s**. Actual CUDA command times were
  about 10s, with separate CPU/CRIU and orchestration costs. Snapshotting does
  not eliminate the full runtime image dependency on a new node.
- All three natural bursts retained their original Pod identity through actual
  restore and readiness and cleaned up normally. The first explicitly survived
  zero operation demand, absent Ready metrics and more than 128s of unscheduled
  waiting. New per-Pod useful-response attribution was not established for
  these natural bursts; the [earlier dedicated useful-serving proof](observer/QWEN-STARTUP-20260908.md)
  remains separate. Nonfatal CRIU teardown diagnostics are preserved.
- Other scientific profiles used their selected conventional startup paths.
  An available snapshot option is not evidence that it restored in these runs.
  Exact per-model end-to-end timings are in the workload reports below.
- R05 inherited naturally prepared caches/capacity; it was not an identical
  fresh-node cold-start experiment. One previously idle preemptible node became
  `NodeStatusUnknown`, disappeared and was replaced without a lost campaign
  attempt or client error. Its cloud-side cause is **unproven**; it must not be
  described as a confirmed planned scale-down or spot interruption.
- Six old failed Pods and a preexisting Pending Cilium operator remain recorded
  as baseline. This pass does not assert that every historical cluster object
  is healthy. No new failed test Pods, restarts or customer-facing failures were
  observed. Large-load capacity, real provider-interruption recovery, other GPU
  families, every possible scientific input and unaided human onboarding were
  not newly qualified by these two small cohorts.

## Evidence and cleanup

- r04: [root gate](R04-ACCEPTANCE.md), [workload](workload/REPORT-r04.md),
  [observer](observer/REPORT-r04.md), [browser](experience/R04-EXPERIENCE.md).
- r05: [workload](workload/REPORT-r05.md), [observer](observer/REPORT-r05.md),
  [browser](experience/R05-EXPERIENCE.md).

Every failed prior cohort and failed intermediate check is preserved. Public
receipts retain exact source/runtime/operation identities and raw evidence
hashes; credentials, original HTTP traces and screenshots remain private.

Both workload runners, both pairs of samplers and both browsers exited and
their owned processes were verified absent. After both browsers closed, root
verified ownership and no mounts on the inert network holder
`6a8f517bfda0c33cfd3f29c9c5e71846d10f9c894212d281e80995d0bc6a0950`,
stopped it and removed only that container. No test artifacts or customer data
were removed. Host DNS remains the original systemd stub; no global networking
or browser error detection was disabled. Cluster capacity is left to the
existing policies, and the customer endpoints remain running.
