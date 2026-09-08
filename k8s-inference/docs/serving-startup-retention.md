# Bounded serving startup retention

Serving `ModelDeployment` availability separates the startup budget from the
idle cooldown. `availability.startupTimeoutSeconds` is optional, accepts
60–7200 seconds, and uses 900 seconds when omitted. Existing revisions retain
their original spec digests when this setting is omitted or null.

The generated KEDA `ScaledObject` has two Prometheus triggers:

1. Existing operation demand, segmented across hot/burst pools.
2. Startup retention, which holds only the Deployment's already-requested
   replicas while its current, non-deleting Pods are Pending or Running but
   not Ready. The hold starts at a positive desired-replica change; first-ever
   Deployment creation is also covered when there is no historical zero scrape.

The second trigger reports `desired replicas × target queue depth`. Its
`AverageValue` target uses the same queue depth, so HPA retains that exact
   count rather than deleting initializing Pods during an N-to-lower change.
KEDA remains the only autoscaled replica owner; minimum/maximum replicas,
pool/node limits, fixed-hot replicas, runtime images, snapshot recipes and
driver compatibility do not change.

Readiness or budget expiry removes startup retention. Existing operation
demand still applies, followed by the original idle/cooldown. The timeout
does not cancel active customer operations. No startup signal can activate a
Deployment whose desired replicas are zero. Pod/container restarts, replacement
UIDs and replica decreases cannot reset the clock; a real later scale-out can.
Retention lasts through telemetry visibility at normal scrape/poll granularity,
not a new wall-clock SLA. Query expiry uses the actual scale-out scrape
timestamp, so a repeated evaluation of an old sample does not renew the budget.

The implementation uses existing kube-state-metrics series, not a new scaler:
`kube_deployment_spec_replicas`, `kube_deployment_created`,
`kube_replicaset_owner`, `kube_replicaset_spec_replicas`, `kube_pod_owner`,
`kube_pod_status_ready`, `kube_pod_status_phase`, and
`kube_pod_deletion_timestamp`. Namespace, Deployment→ReplicaSet ownership,
active ReplicaSet count and Pod UID are matched explicitly. Terminal, deleting,
foreign and retired-ReplicaSet Pods do not retain capacity. Duplicate scrape
replicas are deduplicated. Missing series do not invent replica demand; the
monitoring stack must supply these standard metrics for retention to work.

An unscheduled Pod can legitimately have no Ready condition yet. Retention
therefore begins with the observed, owned Pending/Running set and excludes
only explicitly Ready Pods; it does not require a `ready=0` metric to exist.
The actual positive-replica-change scrape timestamp is retained on a one-second
history grid, including a fresh edge before the next grid step. This supports
ordinary scrape intervals of one second or slower without losing a short edge.
It does not increase the monitoring scrape frequency or renew the timestamp on
equal replica samples. Before the first positive desired/Pod telemetry appears,
the query cannot claim a hold or invent demand. The existing bounded startup
budget remains necessary; telemetry visibility is not instantaneous.

KEDA's `initialCooldownPeriod` alone would not solve repeated activation:
it runs from ScaledObject creation, whereas these ScaledObjects persist across
many cold starts. KEDA's ordinary cooldown controls the last replica to zero;
HPA controls positive replica counts. See the official
[ScaledObject reference](https://keda.sh/docs/2.20/reference/scaledobject-spec/)
and [scaling lifecycle documentation](https://keda.sh/docs/2.20/concepts/scaling-deployments/).

## Evidence retained on 2026-09-08

The failed r02 Qwen burst Pod
`qwen3-8b-b300-burst-h100-1x-6d6575886f-hlzmc`, UID
`b278f059-ccf4-4846-ba87-37500ba1d671`, pulled the full vLLM image in its first
`snapshot-local-address` init container for **154.835 seconds**. Desired burst
replicas dropped from one to zero before that pull completed; the existing
ScaledObject's cooldown was 30 seconds. The first init was then killed, and
the model container never started. This was **not a 155-second GPU restore**.
The original hot Pod remained Ready. The reported 8,634,306,308-byte image size
is a runtime-reported image size, not measured wire traffic.

Private retained receipts remain under
`/home/tux/.local/state/k8s-inference-dual-acceptance/h100/releases/trial-customer-remediation-20260907/r02/observer/`:
`natural-qwen-burst-r02-events.json`, `natural-qwen-burst-r02.json`, and
`samples.jsonl`. No old failure has been relabeled as a pass.

An authorized read-only Prometheus check on the retained H100 stack confirmed
all listed metric families with current series except deletion timestamps,
which were absent while no observed model Pod was deleting. The **new query**
was evaluated against retained r02 samples without applying it live:

| Evaluation time (UTC, September 7) | Startup-retention demand | Interpretation |
| --- | ---: | --- |
| 20:22:45 | 0 | Initial telemetry visibility lag; no invented immediate coverage. |
| 20:23:10 | 1 | Actual historical initializing burst would retain its one requested replica. |
| 20:24:25 | 0 | Historical Pod already deleting; query cannot undo that old deletion. |

Current idle query evaluation returned zero. These are read-only historical
calculations, **not proof of a successful post-fix live startup**.

Offline verification: 111 focused rendering/controller/route/startup tests
passed; the new startup test evaluates 19 real PromQL lifecycle scenarios with
`promtool`, plus invalid identity/budget controls. Ruff passed. A separate
legacy module comparison against source `984cf72f36b3c676fe2c6faa1fc5c5722d4f983d`
confirmed identical spec digests and entire Deployment manifests for both
cold-only and fixed-hot-plus-burst fixtures when the setting is omitted.
The existing hard-coded released digest remains
`sha256:092bab27467b2a92ccfba642ba13cbd2896bdbde3e85080ebf687d105987f000`.
The fast-start test's independent legacy-payload builder was updated to omit
this new optional field; its fixed historical digest assertion was retained.

Root owns the integrated Terraform deployment and subsequent real burst
qualification. Acceptance still requires a new burst Pod to become Ready,
actual restore evidence for that same Pod, and useful public requests served
by it. Regional image presence is not a node-local image-cache hit, and all
fresh-node image, localization and restore costs remain inside measured T0.

Subsequent live qualification and the r03 unscheduled-Pod/clock correction are
documented separately in [the startup qualification](../acceptance/customer-trial-remediation-20260907/observer/QWEN-STARTUP-20260908.md)
and [the retained repair evidence](../acceptance/customer-trial-remediation-20260907/observer/QWEN-UNSCHEDULED-STARTUP-20260908.md).
The earlier test counts and counterfactual results above describe the initial
implementation, not a claim that its later-discovered gap was already fixed.
