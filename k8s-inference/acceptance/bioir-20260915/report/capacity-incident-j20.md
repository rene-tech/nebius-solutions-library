# Post-study j20 capacity loss

`computeinstance-e00j20a9hkb508cn4a` still exists and the provider reports **STOPPED**, with `spec.stopped=true`. The initiating cause remains unknown. Its preemptible setting (`STOP`, priority 5) is not proof of preemption; the inspected records expose no initiating actor or reason.

## Timeline

All times are UTC on 2026-09-15. Exact sanitized provider responses, operation IDs, node conditions, commands and evidence hashes are in [capacity-incident-j20.json](capacity-incident-j20.json).

| Time | Observation |
| --- | --- |
| 23:17:58 | Last Ready-condition kubelet heartbeat. |
| 23:17:59.550353–23:18:18.425545 | Provider disk-attachment update `computeoperation-e00z71k05kb35941am`. |
| 23:18:34.482 | Saved OF3 final preflight observes Ready=True and no GPU allocations. |
| 23:18:35.769 | OF3 cleanup receipt confirms owned ConfigMaps/PVC/PV removed; no GPU allocations/processes. |
| 23:18:54.492 | Successful read-only GPU audit: 0 MiB used, 0% utilization. |
| 23:20:02 | Last GPU-monitor heartbeat reports no GPU error. |
| 23:21:13 | Last runtime-monitor heartbeat reports healthy. |
| 23:22:30 | Ready becomes Unknown, `NodeStatusUnknown`; unreachable taints and latest NodeNotReady event. |
| 23:27:21.947853–23:27:36.418778 | Provider Stop Instance operation `computeoperation-e00kxm5ev20gjpyfg2`. |

The recorded stop begins 291.95 seconds after NodeStatusUnknown. It does not itself explain the earlier heartbeat loss. Disk-update timing is correlation only; subsequent healthy-monitor fields are now stale. No host, driver, or provider-log forensics were attempted.

## Separate from the OF3 application result

This is post-study capacity loss after the completed matrix and normal cleanup. The six saved per-trial node receipts show Ready=True and no reported GPU error during the earlier qualification. The [OF3 report](../snapshot/openfold3/report.md) retains three restored first-request CUDA faults and three ordinary-load repeated-key faults, plus twelve follow-on poisoned-context failures.

Those six independent application failures must not be relabeled as this later capacity interruption. Normal-load controls failed without process restoration; the originating kernel/component remains unresolved. The negative application qualification and HOLD decision remain unchanged.

The [23:31:26 final inventory](final-cluster-state.json) records all 27 model deployments at desired readiness and no evaluation namespace objects. It also includes fkt and xjaw node objects, both Ready=True—not absent. Their recovery was not investigated in this bounded check.

The Nebius CLI and Kubernetes skills constrained this collection to read-only commands with explicit profile/context. No restart, create, delete, reset, allocation, scale/quota change, new benchmark, or provider mutation was performed. Existing peer evidence remains untouched.
