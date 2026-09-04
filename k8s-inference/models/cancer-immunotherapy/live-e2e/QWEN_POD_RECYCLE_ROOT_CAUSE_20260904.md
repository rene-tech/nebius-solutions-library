# Qwen pod-recycle root cause — 2026-09-04

## Verdict

The Cosmos `ModelDeployment` edit did **not** restart Qwen. A concurrent
fast-start qualification session explicitly deleted the Qwen pod and then
started overlapping shell loops that repeatedly selected and deleted the
current Qwen pod. Kubernetes replaced each deleted pod from the same unchanged
ReplicaSet. The qualification session itself recorded that a second campaign
process overlapped the first, stopped both loops, and rejected the resulting
cohort.

This corrects the causal uncertainty in
`LIVE_ADMIN_MODEL_CONFIGURATION_ACCEPTANCE_20260903.md`. The observed
zero-ready interval remains a real service-continuity failure, but its cause was
unserialized destructive testing of the protected single-replica service, not
cross-model state in the admin or model controller.

No cluster object was changed while performing this diagnosis.

## Exact correlation

The retained Task Deck terminal transcript for
`fs2-cancer-immunotherapy-fast-start-snapshot-qualification-r20260902`, tmux
session
`agent-fs2-cancer-immunotherapy-fast-start-snapshot--5c5e684d87f2`, contains:

- an explicit `kubectl delete pod` of
  `qwen3-8b-b300-hot-h100-reserved-8x-5f79788d4-gn4lz`;
- a second explicit deletion of replacement pod `...-b648h`;
- two overlapping `for i in $(seq 1 20)` loops whose body discovers the current
  pod by `app.kubernetes.io/name=qwen3-8b` and runs
  `kubectl delete pod -n fs2-models "$old"`;
- `pkill -f 'qwen20.tsv'`, followed by the session's finding that a second
  concurrent campaign process had been detected, both loops were stopped, and
  the partial cohort was invalid.

The fast-start task log records resumptions at `13:23Z` and `13:39Z`. The live
Kubernetes event stream gives the corresponding deletion/replacement chain:

| Deleted at (UTC) | Deleted pod suffix | ReplicaSet replacement suffix |
| --- | --- | --- |
| `2026-09-03T13:23:58Z` | `gn4lz` | `b648h` |
| `2026-09-03T13:26:06Z` | `b648h` | `79rhb` |
| `2026-09-03T13:40:11Z` | `79rhb` | `29plz` |
| `2026-09-03T13:40:58Z` | `29plz` | `6mxmr` |
| `2026-09-03T13:42:52Z` | `6mxmr` | `s5wbc` |
| `2026-09-03T13:44:45Z` | `s5wbc` | `6d76w` |
| `2026-09-03T13:45:17Z` | `6d76w` | `86ml9` |

For every deletion, kubelet emitted `Killing: Stopping container vllm` and the
ReplicaSet emitted `SuccessfulCreate` for the next pod. It did not emit
`SuccessfulDelete` for Qwen. That event pattern is consistent with an external
client deleting a pod and the ReplicaSet repairing its replica count. By
contrast, the ReplicaSet did emit `SuccessfulDelete` when it intentionally
scaled down the temporary Cosmos burst pod.

The overlap explains the especially destructive tail. One loop was waiting for
pod `29plz` to become Ready while the second loop deleted it; the transcript
retains both `timed out waiting for ...29plz` and `NotFound` for that pod. The
subsequent loops continued deleting not-yet-ready replacements until both were
stopped.

## Candidate-by-candidate elimination

### Shared ConfigMap or Secret rollout: eliminated

Qwen retained:

- `ModelDeployment` UID `a7ca4c91-973b-467f-aaba-3fa566ff9546`, generation
  `4/4`, and spec digest
  `sha256:d1da450072bb7002d46b234ba5842671e001e10f36eefc382c1c0195c6876add`;
- Deployment UID `95137ee9-d290-4f36-8076-9de85c65d4e2` and generation `4`;
- ReplicaSet UID `10480968-e5c5-42ee-8e69-a04bfc565dad`, revision `4`, and pod
  template hash `5f79788d4` throughout all seven replacements.

The Qwen Deployment does not mount `fs2-model-bundles`,
`fs2-model-envelope`, or a shared Secret. Its material volumes are the
model-specific `qwen3-8b-b300-contract` ConfigMap, model-specific
`qwen3-8b-cache-rwx-7af24455` PVC, and `emptyDir` volumes. A shared-content hash
therefore did not alter its pod template, and the unchanged ReplicaSet proves no
rollout occurred.

### Model controller cross-model write: eliminated

Retained Loki logs show the controller does list/reconcile both ModelDeployments
on its five-second cycle and patches their **status**. During the incident it
made zero non-status Qwen writes. Its only rendered-resource writes were scoped
to Cosmos:

- r6 at `13:33:46.837Z`–`13:33:47.268Z`: delete the Cosmos ScaledObject and
  burst Deployment, then create/update only the Cosmos hot Deployment,
  adapter/publication ConfigMaps, Service and ServiceAccount;
- r7 at `13:40:38.712Z`–`13:40:39.215Z`: delete the Cosmos hot Deployment and
  recreate/update only its burst Deployment, ScaledObject, ConfigMaps, Service
  and ServiceAccount;
- one Cosmos burst Deployment convergence patch at `13:40:54.613Z`.

The first two Qwen deletions predate r6. The `79rhb` deletion at `13:40:11Z`
also predates the controller's r7 resource writes. Source inspection further
confirms `Pod` is not in the model controller writer allowlist:
`POD_ENDPOINT` is used only for read-only discovery, while apply/delete accepts
only `RESOURCE_ENDPOINTS`, which excludes Pods.

### Shared Kueue/admission rewrite or preemption: eliminated

Kueue v0.17.8 did not log or emit `Evicted`, `Preempted`,
`ExcessPodDeleted`, a non-admission stop, or a queue-policy rewrite for these
workloads. After each external deletion it observed the new pod, created a new
pod-owned Workload, reserved the same
`inference-h100-reserved-8x` flavor, and admitted it immediately.

The exact upstream v0.17.8 source at commit
`818686e072da52af3564ba21f37f826fe05190af` also shows that the Deployment
integration uses a no-op reconciler with get/list/watch-only RBAC; its webhook
propagates the queue labels and `pod-suspending-parent: deployment` annotation.
The Pod integration can delete a pod when stopping an unadmitted, evicted, or
deleted Workload, but none of those trigger conditions or log paths occurred
here. Kueue reacted to each replacement; it did not initiate the deletion.

### Startup probes: eliminated as the delete trigger

Replacement pods recorded startup-probe connection refusals while vLLM loaded,
but the unchanged template allows 180 failures at a ten-second period. Several
pods were externally deleted in under two minutes, and `29plz` in 47 seconds.
The probe could not reach its failure threshold before those deletions.

## Narrow fix

Do not change the model controller for this incident. A controller change would
not address the actor that deleted Qwen and would add risk to a path whose
resource isolation behaved correctly.

The smallest enforceable fix is to close the direct-delete path used by live
benchmark agents:

1. Remove `pods/delete` in `fs2-models` from the shared task/operator
   credential. The model controller does not need it.
2. Give that verb only to a narrow model-maintenance runner. The runner must
   acquire a Kubernetes Lease named from the exact model deployment, record the
   task/run holder, and reject a held or stale-uncertain lease.
3. Require the runner to fence every operation on exact ModelDeployment UID,
   spec digest, Deployment UID, ReplicaSet UID and pod UID. Selector-only pod
   deletion is forbidden.
4. Refuse disruption of a protected deployment with fewer than two Ready
   replicas unless an explicit maintenance-window receipt is present. Qwen has
   one 8x-H100 replica, so deleting it necessarily creates a zero-ready interval.
5. Make both live acceptance and fast-start tooling acquire the same per-model
   Lease. This serializes a customer-continuity observation against a destructive
   benchmark even when the tasks run in different worktrees or processes.

The Lease alone is advisory, so the RBAC restriction is the part that makes the
fix enforceable: ordinary agents cannot bypass the runner with ad-hoc
`kubectl delete pod`. This is a scoped RBAC/tooling change, not a scheduler,
controller, or Terraform refactor.

## Proof required after the fix

Use a quiescent window or task-owned model and retain one continuous observation
covering baseline, Cosmos edit, reconciliation, rollback and two additional
controller cycles. It passes only if all of the following remain invariant for
Qwen:

- ModelDeployment UID/generation/spec digest;
- Deployment UID/generation;
- ReplicaSet UID/revision/pod-template hash;
- pod UID and deletion timestamp;
- EndpointSlice ready/serving state and authenticated semantic requests.

At the same time, retained controller logs must show no non-status Qwen
`PATCH`/`DELETE`, and the maintenance runner must demonstrate that a concurrent
Qwen destructive request is rejected by the held Lease/RBAC boundary. That
would prove the admin edit is isolated and the previously uncontrolled actor can
no longer invalidate the observation.

## Current safety state

At the final read-only check, Qwen replacement `86ml9` was Ready and serving
from the original ReplicaSet, with zero container restarts. This diagnosis did
not rerun the Cosmos mutation, delete a pod, submit a GPU job, change Kueue,
apply Terraform, touch B300, or access Forge. The full customer-readiness task
therefore remains running.
