# Protenix snapshot capacity interruption

The provider confirms that `computeinstance-e00fkt1bsa4ec657sn` still exists and is **STOPPED** (`spec.stopped=true`). The initiating cause is unresolved: the instance is preemptible with `on_preemption=STOP`, but the inspected records do not identify preemption or an initiating actor. They also do not establish a CUDA, CRIU, driver, or container-runtime fault.

## Timeline

All times are UTC on 2026-09-15. Exact commands, operation records, node conditions, and evidence hashes are in [capacity-incident-fkt.json](capacity-incident-fkt.json).

| Time | Observation |
| --- | --- |
| 22:32:47.285617–22:33:07.066810 | Provider operation `computeoperation-e00csk3rhcpw6dgcpf`: updating attached disks. |
| 22:33:09.071145–22:33:37.230116 | Provider operation `computeoperation-e00n1xsr7461f868m1`: updating attached disks. |
| 22:33:17 | Last recorded kubelet Ready-condition heartbeat. |
| 22:33:38 | Recorded container start for `bir-protenix-snapshot-restore-3`. Its subsequently reported Running/Ready state is stale, not evidence that restored inference completed. |
| 22:34:28 | Last runtime-monitor heartbeat reports `ContainerRuntimeHealthy`; this is not a continuing health guarantee. |
| 22:35:29 | Ready becomes Unknown with `NodeStatusUnknown`; node-controller records NodeNotReady and unreachable taints. |
| 22:36:41.490800–22:36:54.551563 | Provider operation `computeoperation-e00s95b6p81cvzaxvj`: Stop Instance. |

The recorded stop begins 72.49 seconds after NodeNotReady, so it does not by itself explain the earlier heartbeat loss. Disk-operation timing is correlation only. Last GPU error conditions reported no error at 22:32:36 but are stale; a later host fault is not excluded. The aggregated NodeNotReady event has an older first occurrence, so this incident uses its latest timestamp and the node-condition transition.

## Replacement verification

Before the replacement launch, `computeinstance-e00xjaw5jqexvvnpat` was Ready, with one allocatable GPU and zero GPU-requesting pods. The 11 observed pods were system/observability workloads. A read-only query through its existing GPU detector returned:

```text
NVIDIA H100 80GB HBM3, GPU-ca98bcbc-cf06-a4c2-e3bc-c27a0cba95bc, 580.159.04, 0 MiB, 0 %
```

The compute-process query returned no entries. Ready, GPU-error, and runtime-condition heartbeat times were 22:37:13, 22:37:29, and 22:37:49 respectively. No separate exact collection timestamp was captured for these four checks; they preceded the 22:38:58 provider-operations query and the peer replacement launch.

This verified usable capacity for the authorized fresh independent snapshot cohort, with a new PVC/name prefix and no cross-GPU-UUID bundle reuse. The parent subsequently reported that replacement evaluation is active; the observation is not a claim that this node remains available for other work. No user decision or additional provisioning was required.

## Scope and evidence

The check used the Nebius CLI and Kubernetes skill safety procedures: existing explicit profile/context, read-only provider/Kubernetes commands, and sanitized provider fields. No restart, reset, force deletion, provisioning, scaling, controller, finalizer, or quota changes were made. Investigation ended after the bounded timeline was accepted; no causal conclusion is inferred from missing evidence.

The original peer incident evidence remains unchanged: [node](../snapshot/protenix/incident/node.json), [node events](../snapshot/protenix/incident/node-events.json), and [restore-3 pod](../snapshot/protenix/incident/pod.json). Their SHA-256 hashes are preserved in the companion JSON. The failed-node pod/PVC and the replacement benchmark are owned by the snapshot peer, not this incident check.
