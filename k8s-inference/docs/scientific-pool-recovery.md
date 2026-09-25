# Admitted scientific workload recovery

For this operational-only release, `scientificBatch.toolsImage` is explicitly
pinned to the preceding qualified control-plane/tools digest. The gateway and
model-controller image can change without changing scientific CPU-stage or
companion images. Empty preserves the chart's historical same-image default;
updating this pin separately requires the affected recipe/profile qualification.

Scientific Jobs and JobSets can hold a Kueue reservation without a usable node.
The controller distinguishes that state from waiting for admission and from
loading an image, artifacts or a checkpoint. An ordinary customer keeps one
operation, idempotency key and immutable scientific input across recovery.

## Incident and effective configuration

On September 24, 2026, two GROMACS umbrella windows in operation
`8bcd18d3-bfc0-4f3f-a4e4-86410e793072` were admitted to `h100-1x`, whose four
registered Nodes were `Ready=Unknown`. Their Pods had no node assignment or
container status. An operator requeued them after about eleven minutes.

The September 25 read-only audit confirmed Kueue v0.17.8 at image
`sha256:cecba825d0b0feab9bed2835efe2eb8d825512f1616c8762ab80c53f2ea6afe6`.
Its Deployment mounts the active Configuration with `waitForPodsReady.timeout:
2h`, `recoveryTimeout: 15m`, creation-based requeue ordering, and a five-requeue
limit with 15–300 second backoff. In this exact v1beta2 version, presence of the
configuration activates the feature; there is no separate `enable` field.
The 15-minute recovery timeout applies after a running workload loses readiness.
The two-hour initial deadline therefore did not expire during the incident.
[Pinned Kueue configuration definition](https://github.com/kubernetes-sigs/kueue/blob/v0.17.8/apis/config/v1beta2/configuration_types.go).

The unavailable flavor remained eligible because its configured queue quota is
a capacity envelope, not a node-health measurement. The live autoscaler also
reported the corresponding group unhealthy with four registered/unready Nodes,
zero ready/unregistered/not-started Nodes and no scale-up progress. Existing
`max_queue_seconds` deliberately ends at admission, so it did not cover the stall.

## Recovery policy and bounds

The operational controller and Kubernetes observer extend the existing immutable
attempt boundary. They do not change Kueue's quota/admission ownership or the
qualified model/runtime/recipe/snapshot identities.

| State | Controller action |
| --- | --- |
| Awaiting reservation/admission | Existing frozen queue deadline and priority/lane policy |
| Every observed Pod is Pending, unscheduled, has no node and no container/init status; every selected-pool Node has stopped reporting; fresh autoscaler evidence confirms no replacement progress | Retire the unstarted attempt after the confirmation deadline |
| Empty pool, newly NotReady Nodes, pending node registration, increased autoscaler target or active scale-up | Preserve the full admitted startup window |
| Any Pod is assigned, initializing, loading or running, or durable history has passed node-pending | Never apply pre-start recovery |
| Missing/stale/incomplete health observations | Report unknown; do not infer a pool failure |
| Still admitted and positively unstarted at the fallback deadline | Bounded `admitted_scheduling_timeout` outcome |

Current fast confirmation requires all selected-pool Nodes to report
`Ready=Unknown` / `NodeStatusUnknown`, with complete timestamped observations.
It corroborates their `nebius.com/node-group-id` against the standard
`kube-system/cluster-autoscaler-status` ConfigMap, whose probe must be at most
120 seconds old. This is deliberately narrower than treating arbitrary
`NotReady` or scheduler messages as proof of provider failure.

Chart settings under `scientificBatch.poolRecovery`:

| Setting | Default | Meaning |
| --- | --- | --- |
| `failureConfirmationSeconds` | 120 | Minimum since both admission and the latest lost-node transition |
| `admittedUnscheduledTimeoutSeconds` | 7200 | Separate upper bound for positively unstarted admitted attempts |
| `backoffBaseSeconds` | 15 | Retry delay measured from the persisted failed-attempt completion |
| `backoffMaxSeconds` | 300 | Exponential delay cap; the existing frozen attempt budget still applies |

The qualification target is re-admission within five minutes of confirmed pool
unavailability when another qualified pool has available admission capacity.
This excludes that destination's model-loading time. It is an acceptance
target, not a measured guarantee until the exact candidate receipts pass.

Failure is stored as `infrastructure` / `admitted_pool_unavailable`, never
provider preemption. Generic reservation deletion/recreation similarly reports
an infrastructure boundary with its original cause; it does not invent the
actor. Explicit Kueue preemption events retain their existing classification.

The original attempt's Kueue UID, pool, admission time and Pods remain in history.
Foreground deletion is requested once in durable state, and Kubernetes must
confirm the exact workload UID absent before a replacement is created. Successful
or running peer shards are not duplicated; released failed shards can retry
while peers continue. Cancellation always wins on the following fenced reconcile.

Retry pool selection is a deterministic subset of the original qualified set:
exclude pools on earlier capacity-failed attempts of the same stage/shard while
an alternative remains. Full Pod, GPU, topology, snapshot and artifact constraints
remain in the normal manifest renderer. If every qualified pool has failed,
the remaining original attempt budget can retest the original set, allowing
capacity to return. No retry budget is added. With no capacity, the run ends
with an actionable error rather than an indefinite admitted state.

Timers, attempts and pool exclusions derive from PostgreSQL attempt history and
Kubernetes condition times; a controller restart does not replenish them. The
existing tenant lane, workload priority and fair-share usage remain unchanged.
A replacement is a new Kueue Workload, so its Kubernetes creation timestamp is
new; the change does not promise to preserve original position among equal-priority
Workloads. Existing schema, operation and billing ledgers retain ownership.

## Customer and operator evidence

The public attempt status includes a `recovery` object with the original cause,
failed pool, current qualified/avoided pools, attempt budget, retry time and
measured admitted wait. Admin attempt details carry the same recovery evidence
and bounded scheduler explanations. Queuing/reservation time is not presented
as GPU execution; the existing Pod/node/device ledger accounts for any real work.

Local regression coverage includes dead pools, incomplete health, scale-up,
slow cold starts, partial gang initialization, independent peer progress,
controller crashes/CAS failures, retry exhaustion, cancellation and real
PostgreSQL restart durability. Live customer qualification is recorded under
`acceptance/admitted-pool-recovery-20260925/`; component tests alone do not establish
readiness. Use task-owned injection, ordinary credentials and native GPU output
validation, retain all failed/inconclusive runs, and require two consecutive
unchanged-release cohorts.

Deployment must preserve sibling image/config identities, add only node-list
and named autoscaler-status read access, and record the prior Helm revision and
image digest. Rollback restores the prior image/chart; no state migration or
quota change is involved. Existing stored attempts remain readable by the prior
release, although the prior controller lacks this automatic recovery behavior.
