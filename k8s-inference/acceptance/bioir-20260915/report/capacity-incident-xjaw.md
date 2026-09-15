# Second Protenix capacity interruption: xjaw

The instance `computeinstance-e00xjaw5jqexvvnpat` still exists. The provider reported RUNNING in the 23:02:40 UTC check, then STOPPED in the 23:03:53 check. Its initiating failure cause is **unresolved**: neither preemption nor a CUDA/CRIU/driver/runtime cause is proven. Exact sanitized responses, operations, commands and evidence hashes are in [capacity-incident-xjaw.json](capacity-incident-xjaw.json).

## Timeline

All times are UTC on 2026-09-15.

| Time | Observation |
| --- | --- |
| 22:58:09.708986–22:58:27.018676 | Provider disk-attachment update `computeoperation-e00j1296wtrxzpyhg4`. |
| 22:58:28.980519–22:58:48.319298 | Provider disk-attachment update `computeoperation-e00wevdz3vrgyh11jf`. |
| 22:58:29 | Last Ready-condition kubelet heartbeat. |
| 22:58:51 | Recorded start of ordinary-load control `bir-protenix-xjaw-normal-3`. |
| 22:59:19 | Last runtime-monitor heartbeat reports healthy. |
| 23:00:34 | Ready becomes Unknown, reason `NodeStatusUnknown`; unreachable taints and latest NodeNotReady event. |
| 23:03:14.383016 | Provider Stop Instance operation `computeoperation-e00zxzfvgs9t1x2p7f` starts. |

The stop began 160.38 seconds after NodeStatusUnknown. Its actor/reason and completion timestamp were not returned in the observed operation record; a later instance read confirmed STOPPED. The preemptible configuration (`STOP`, priority 5) does not identify the initiating event. Disk-update overlap is correlation only. Last healthy GPU/runtime conditions are stale, and stale pod Running/Ready status does not establish successful inference.

Parent-reported qualification context: three restored trials and two normal controls completed before this interruption during normal control 3. This incident check does not re-audit those outputs. The third normal control remains incomplete; no more tests or nodes are authorized.

## Existing isolated alternative, report only

`computeinstance-e00y0jttwekyghrznp` is an existing non-preemptible single-H100 instance: the provider reports RUNNING with the `1gpu-16vcpu-200gb` preset and no `spec.preemptible` object. It was Ready at 23:03:02 with no memory/disk/PID pressure. Its 11 pods were only system/observability agents, with no GPU requests or customer/evaluation workloads.

A read-only query through the existing detector returned:

```text
NVIDIA H100 80GB HBM3, GPU-9885f9c6-110a-10b5-2c26-c256e895d575, 580.173.02, 0 MiB, 0 %
```

The compute-process query was empty. A parent-requested read-only recheck at 23:05:12 confirmed Ready=True, zero GPU-requesting pods (including init containers), 0 MiB, 0% utilization and no compute processes; it was sent directly to the snapshot worker. This is an observed idle alternative, **not authorization or a reservation**. Driver 580.173.02 differs from xjaw's 580.159.04; no reuse of the old bundle across driver/GPU identity is qualified. No shared customer host was proposed or used.

The Nebius CLI and Kubernetes skills constrained this bounded check to read-only commands with explicit profile/context. No allocation, restart, reset, deletion, provisioning, scale/quota change, new benchmark, Slack message, or further forensic investigation was performed. Original [node](../snapshot/protenix/xjaw/incident/node.json), [events](../snapshot/protenix/xjaw/incident/node-events.json), and [pod](../snapshot/protenix/xjaw/incident/pod.json) evidence remains untouched.
