# Scientific workload cohort r03

All 14 scientific operations passed, with no client retry, cancellation,
manual recovery or scientific HTTP failure. The second Proteina submission and
exact replay succeeded, and the RF four-shard batch recovered automatically from
a real priority preemption. Every terminal attempt reported resource release.

This is a scientific functional pass, **not a strict clean cross-lane cohort**:
root retained two explained test-host browser read failures and an unscheduled
Qwen startup-retention failure. Two natural Qwen burst Pods were deactivated
after the existing idle cooldown before they could start; hot-capacity requests
still succeeded. The [Qwen startup evidence](../observer/QWEN-UNSCHEDULED-STARTUP-20260908.md)
preserves both failed startups and the query defects. These are not scientific
API failures, but are not erased from the customer-experience verdict. Root owns
browser/ordinary-service closure and the next acceptance gate.

Run `trial-customer-remediation-20260907-r03` lasted from
07:59:29.008593Z to 08:24:52.683487Z on 2026-09-08: **25m23.675s**.
The runner exited 0 naturally. Deployed source stayed
`bc264980f3fedc33c8fdc6d59c93096d8db9ccaf`; campaign evidence HEAD was
`aea2d2ce4da176a9479f5280be478f7fc216d8a3`.

The unchanged wrapper verified original runner/scenario bytes, fixture and input
hashes, parameters, priorities and the four-client ceiling. Three sequential
requests (RFdiffusion → Protenix → mosaic) preceded eleven mixed requests.
Later clients waited locally before submitting; all fourteen operations were not
durably queued at once. The RF four-shard operation is the actual server-side
batch. No production, fixture, capacity or policy change occurred in this lane.

Evidence: [campaign plan](results-r03/campaign-plan.json),
[aggregate](results-r03/aggregate.json),
[full measurements and trace hashes](results-r03/measurements.json).

## Observed customer times

All values are seconds. Accepted→terminal includes server queueing and execution.
Client wall also includes uploads, submit/replay, polling and two downloads.
Local slot wait occurs before submission and is excluded from those two clocks.
These are small-fixture observations under concurrent normal traffic, not
fresh-node cold-start measurements, scientific efficacy results or an SLA.

| Case | Accepted→terminal | Client wall | Local slot wait |
|---|---:|---:|---:|
| RFdiffusion switch | 77.278 | 86.717 | — |
| Protenix v2 switch | 63.632 | 70.944 | — |
| mosaic switch | 102.443 | 109.413 | — |
| BoltzGen 1 | 804.638 | 815.058 | 0.000 |
| BindCraft 1 | 658.725 | 669.187 | 0.001 |
| Proteina-Complexa 1 | 251.740 | 258.546 | 0.001 |
| ESMFold2 | 191.118 | 198.477 | 0.001 |
| BoltzGen 2 | 936.822 | 945.669 | 198.567 |
| BindCraft 2 | 401.274 | 409.166 | 258.638 |
| Proteina-Complexa 2 | 249.991 | 257.123 | 667.823 |
| mosaic 2 | 226.435 | 234.612 | 669.273 |
| RFdiffusion four-shard bulk | 434.973 | 441.372 | 815.152 |
| ESMFold2-Fast | 92.267 | 98.277 | 903.904 |
| AlphaFold3 | 68.658 | 76.205 | 924.967 |

All rows passed. Exact runtime image/model/recipe digests and execution identities
are retained in each receipt; requested labels correspond to these variants:

| Requested profile | Runtime variant |
|---|---|
| Proteina-Complexa | `upstream-dev-20260827` |
| BoltzGen | `upstream-v0-3-2` |
| mosaic | `mosaic-boltz2-proteinmpnn-v1` |
| BindCraft | `v1-5-3-pyrosetta-academic` |
| RFdiffusion | `rfdiffusion-v1-1-0` |
| ESMFold2 / ESMFold2-Fast | `biohub-v3-4-0`, separate image/execution digests |
| Protenix v2 | `upstream-v2-0-0` |
| AlphaFold3 | `upstream-v3-0-4` |

## Delivery and recovery checks

- Fourteen exact replays returned the same fourteen operation IDs.
- Fourteen server semantic validations passed, including the required RF shards.
- Twenty-eight artifacts were downloaded and hash-verified: one result manifest
  and one bounded scientific output per operation.
- All 994 scientific HTTP calls succeeded: 938×200, 28×201 and 28×202.
  There were 840 status polls; the slowest took 1.112281s. Maximum measured
  submit/replay response was 0.838088s and maximum download was 0.723640s.
- Forty-six stage attempts are retained: 45 succeeded and one was preempted;
  28 GPU attempts and 18 CPU attempts. Every attempt released its resources.
- The runner stopped normally; no owned scientific client remains active.

The export's peak-admitted-attempt value is 5, an observation derived from
scientific receipts, not total physical cluster GPU use. Its generic historical
cancelled-case caveat is inherited from the frozen exporter; r03 had no
cancellation. Independent observer records own cluster occupancy, actual Pod
cleanup and ordinary-service recovery evidence.

### Proteina admission returned the committed operation correctly

Second Proteina operation `acf64a64-ae82-4728-8723-7246e1c28d2e` received
HTTP202 on initial submit at 08:15:07.059275Z (`reused=false`) and exact replay at
08:15:07.714769Z (`reused=true`). It completed all stages, result validation,
downloads and release. The r02 HTTP409 failure remains preserved separately;
this passing run does not prove that the identical historic database
interleaving occurred. Deterministic concurrent PostgreSQL coverage is in the
[admission regression evidence](ADMISSION-RACE-FIX-r20260908.md).

### BindCraft used a fitting reserved pool without lowering resources

Second BindCraft operation `65fe6dbd-3982-48cc-9454-bf7174292272` retained
16CPU/96Gi plus the 100m/256Mi collector: 16,100m CPU, 98,560Mi memory and one
GPU for the full Pod. Frozen affinity allowed only `h100-reserved-8x`.
It was admitted/scheduled at 08:08:18Z on
`computeinstance-e00p3acr87k9k4mckj` and Ready at 08:08:29Z.
Unlike r02, fitting capacity was immediately available: **there was no
reserved-quota wait in this case**. Observer evidence is
`r03/observer/bindcraft2-fitting-reserved.json` in the private release root.

### RF priority preemption recovered on the original operation

RF operation `b6d302d9-3f24-40ae-906b-7373def0afab` retained the original
four shards and bulk priority −100. AlphaFold3 priority0 preempted
`design-002` at 08:19:43Z, corroborated by exact Kueue events in
`r03/observer/rf-bulk-shard002-preemption.json`. This was priority scheduling,
not preemptible-node loss.

The first attempt released resources. After the other current shards finished,
the platform automatically started attempt2 at 08:23:21.529143Z; it succeeded
at 08:24:33.939043Z. The collector then completed at 08:24:47.495940Z.
The client continued polling the original operation ID and downloaded the
validated result. No client resubmission, cancellation or manual Job operation
was used. The r02 finalization failure and separate post-release recovery remain
unchanged in their original records.

### Snapshot and startup evidence

Protenix operation `1001f32b-4ebf-4f1f-98bb-137a3ed30041` actually used
the existing CUDA-CRIU snapshot. Pod `ce329cc5-d043-4b15-acaf-17d6fca76f7e`
started its scientific container at 08:01:36Z; the
`scientific_snapshot_request` marker reported `cuda-criu-restored` at
08:01:40.184542014Z. The reconciled terminal ledger reports:

- Restore: 4.184542 GPU-seconds.
- Active compute: 17.815458 GPU-seconds.
- Scheduler occupied: 35 GPU-seconds.
- Occupied but idle: 17.184542 GPU-seconds, including restore.

There are no ledger gaps or reconciliation delta. Do not add restore to idle
again, or confuse this startup interval with end-to-end client time or pure
device-copy bandwidth. Exact evidence is
`r03/observer/protenix-switch-restore-loki.json` and observer samples; the browser
independently displayed the 4.184542s source boundary. Other scientific models
are not claimed to have used a GPU snapshot based on an available option alone.

Mosaic2 incurred a measured 109.781s image pull on a preemptible node, from
08:15:20 to 08:17:10. Its runtime-reported image size was 4,204,444,871 bytes;
that is not measured network wire bytes or GPU restore data. This cost is
separate from the scientific execution and helps explain its longer customer
time. BoltzGen2's configure stage was 122.379s versus 19.432s in the first run;
these are complete stage intervals, not an unqualified weight-loading claim.

## Offline validation and next gate

All nine harness/equivalence/export unit tests passed after export. Original
fixtures and harness remained unchanged. Raw HTTP traces and credentials stay
private; exported receipts retain exact trace hashes without credentials.

Root requires two fully clean cross-lane cohorts, including browser transport.
R03 remains a scientific functional pass with the two browser read failures and
the two prematurely deactivated Qwen burst startups retained. R04/r05 must use
the corrected startup-retention release with unchanged scientific inputs; no new
traffic starts without root's explicit authorization.
