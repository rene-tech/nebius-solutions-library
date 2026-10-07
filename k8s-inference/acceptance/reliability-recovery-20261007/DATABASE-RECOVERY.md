# PostgreSQL replica recovery, 7 October 2026

This is an incident record, **not a script to rerun on a healthy cluster**.
Cluster: `fs2-data/fs2-control-db` in `mk8scluster-e00j5z9te7x5dd9g6a`.

The primary `fs2-control-db-1` and replica `fs2-control-db-3` remained healthy.
Replica 2 was trying to rejoin after an old timeline divergence; `pg_rewind`
needed WAL `000000040000002500000018`, which was unavailable. Repeated restarts
could not recreate that historical WAL.

## Actual repair

1. Saved Cluster, Pod, PVC and primary/replication evidence privately. Confirmed
   instance 1 was still the writable primary and instance 3 was streaming.
2. Used the official CloudNativePG 1.30.0 plugin's `destroy ... 2 --keep-pvc`
   operation on the **failed instance only**. Neither the primary nor the healthy
   replica was stopped. The plugin archive checksum was verified against the
   release checksum before execution.
3. CloudNativePG retained the old directory as
   `/var/lib/postgresql/data/pgdata_20261007T130949Z` and created the replacement
   join Job. The old copy is about 39 GB; the primary base backup is about 82.5 GB.
4. Expanded only replica 2's PVC from 100 to 200 GiB to retain both old and new
   copies. No project quota or other database volume was changed. The Nebius CSI
   driver required offline expansion, so the **join Job**, not a database primary,
   was suspended while that volume detached. Then the same Job was resumed.
5. The filesystem grew to about 197 GiB and the replica restarted its base backup.
   No WAL/rewind error was hidden and no forced primary failover was performed.
6. Base backup completed at 13:54:21 UTC. The retained PVC was still deliberately
   detached by `destroy --keep-pvc`, so the operator could not see it in its
   owner-indexed PVC list and kept adopting the already-completed join Job.
   After checking the completed Job and the original ownership record, restored
   **only the original Cluster ownerReference** with UID/resource-version guards
   (`replica-pvc-readoption.json`). This lets the operator mark the PVC ready and
   create the replica normally; no manual Ready condition, data rewrite, primary
   restart or repeat base backup is needed.

PVC `fs2-control-db-2` kept UID `5260b916-fee8-400e-9844-18d192323b5e` and PV
`pvc-5260b916-fee8-400e-9844-18d192323b5e` (Nebius disk
`computedisk-e00z3vmdyn9jc0k00s`). The initial join Pod was disposable and was
replaced during offline resizing; its partial backup cleaned itself up. The old
database directory remains recoverable on the retained PVC.

The desired ordinary database volume size remains 100 GiB; this replica's 200 GiB
is a documented recovery exception. Do not attempt to shrink it or reconcile it
by deleting data. Review disposal of the retained old copy separately after
recovery is confirmed and an actual backup policy is agreed.

## Acceptance

Require 3/3 Ready instances, unchanged primary identity, two streaming replicas,
replay positions caught up, and preserved volume/old-directory identity. The
final state and timestamps are recorded in the adjoining README and receipts.

Pre-existing limitations are not solved by this repair: all three database Pods
are placed on one system node, and no CloudNativePG Backup/ScheduledBackup was
configured. Three Ready instances therefore do **not** establish node-failure
resilience or point-in-time disaster recovery. Those architecture changes are
outside this narrowly authorized incident repair.

Official procedure references:
[CloudNativePG troubleshooting](https://github.com/cloudnative-pg/cloudnative-pg/blob/main/docs/src/troubleshooting.md)
and [plugin instance management](https://github.com/cloudnative-pg/cloudnative-pg/blob/main/docs/src/kubectl-plugin.md).
The exact 1.30.0 [PVC completion handler](https://github.com/cloudnative-pg/cloudnative-pg/blob/v1.30.0/pkg/reconciler/persistentvolumeclaim/status.go)
and [owner-indexed reconciliation](https://github.com/cloudnative-pg/cloudnative-pg/blob/v1.30.0/internal/controller/cluster_controller.go)
explain why re-adoption is needed after retaining the old PVC.
Protected before-state and plugin evidence are under
`/home/tux/secure-handoff/fs2-reliability-recovery-20261007/database/`.
