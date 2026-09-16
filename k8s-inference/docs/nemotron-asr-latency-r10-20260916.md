# English ASR latency during natural burst-node loss

Read-only diagnosis of workshop run `0783b3d4-45d7-4e10-9260-9d0a83dea83e`, operation `23e7345c-f777-4e02-beb5-6e3a590ff8f4`. No new inference, configuration change, rollout, node action or injected failure was performed. Exact resource identities, events and payload-free lifecycle signals are in the [sanitized receipt](../acceptance/voice-agent-20260916/nemotron-en-natural-node-loss-r10.json).

## Finding

This was **not a zero hot floor or a KEDA scale-down**. English Nemotron was configured min 1 / max 2: one fixed regular H100 hot replica plus a min-0/max-1 preemptible H100 burst deployment. The hot pod remained Ready from 12:01:07 UTC with zero restarts. The canonical Service selects both roles and does not prefer hot replicas.

| UTC | Observation |
| --- | --- |
| 18:34:04 | KEDA activated the burst deployment, 0 → 1. |
| 18:34:50 | Burst container became ready. |
| 18:36:05.238 | Delayed ASR attempt 1 admitted. |
| 18:36:30 | Burst node became `NodeStatusUnknown`, tainted unreachable; pod Ready became false. |
| 18:38:22.220 | Attempt 1 emitted `retry` with `runtime_transport_error`. |
| 18:38:23.700 | Attempt 2 ready. |
| 18:38:26.501 | Attempt 2 succeeded on the existing regular hot pod. |
| 18:41:30 | Taint manager evicted the burst pod; its replacement was unschedulable. |

The burst node is labeled preemptible and has a cloud-provider shutdown taint. Kubernetes proves loss of readiness/unreachability; the provider's underlying cause is not established. The first failed attempt has no retained pod correlation. Attribution to that burst endpoint is a strong temporal inference, not a proven socket identity. Successful attempt 2 is exactly joined to the hot pod, node and GPU.

The previous KEDA deactivation was 18:15:19, outside the stall. The 18:41:30 eviction follows the pod's explicit 300-second unreachable toleration, consistent with [Kubernetes taint eviction semantics](https://kubernetes.io/docs/concepts/scheduling-eviction/taint-and-toleration/).

## Actual latency and failure bounds

The operation's `cold_start_seconds=138.487557` is legacy **accepted-to-ready time**, including retries, not model initialization time. Attempt 1 admission to transport retry took 136.983 seconds; successful attempt 2 ran for 2.797 seconds, with 2.353 seconds of model processing for 44.536 seconds of audio. Accepted-to-completion was 141.289 seconds.

Live settings were runtime timeout 3,600 seconds, activation timeout 7,200 seconds, retry base 1 second, worker lease 30 seconds and operation max attempts 3. The operation deadline was 20:36:05 UTC, about two hours after acceptance. Workshop ASR polling has a separate 600-second timeout after the initial request. A lease heartbeat is not a per-call failover deadline. The scalar runtime timeout is used for individual HTTP I/O phases; [HTTPX distinguishes connect, read, write and pool timeouts](https://www.python-httpx.org/advanced/timeouts/).

**138 seconds is an observation, not an enforced recovery bound.** Lifecycle stores a generic transport reason, not whether the underlying failure was connect, read, reset or another transport condition. It also reuses the first activation timestamp in the attempt-2 admit signal; do not treat that timestamp as proof of overlapping attempts.

## Recommendation only

1. Keep the existing hot floor. Increasing it does not directly fix waiting on a failed endpoint.
2. Add a narrowly scoped native-ASR connect timeout, separate from the inference/read budget, and retain the existing operation deadline and fenced retry accounting. Capture a safe transport failure class so future receipts distinguish connect from read failures. Do not shorten generic model read timeouts.
3. If interactive hot-first behavior is required, implement controller-owned hot-role selection with explicit busy/unavailable fallback to the canonical or burst route. Keep the same model authorization, operation admission, usage accounting, canonical Service, scaling policy and GPU limits. No such preference exists today. Merely narrowing the canonical Service would remove burst capacity; ordinary Kubernetes [traffic distribution preferences](https://kubernetes.io/docs/concepts/services-networking/service/) are topological, not hot-versus-burst role priorities.
4. Validate bounded transport failure, explicit pre-admission busy fallback and unchanged burst scale-out before rollout; perform a scoped controlled-fault test before claiming a short recovery SLO.

This is evidence of **observed natural node-loss coinciding with successful transport-retry recovery**, not a controlled preemption qualification. The earlier voice-runtime graceful-drain tests remain a separate category of evidence.

The Kubernetes diagnostics/autoscaling workflow kept this investigation read-only and separated configured floors, actual readiness, transport retries and causal uncertainty. A temporary standard admin session was used only to read payload-free lifecycle fields and was logged out successfully (HTTP 204).
