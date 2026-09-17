# Snapshot checkpoint storage provenance

These recipes use **ReadWriteOnce block-backed checkpoint volumes, not a shared RWX filesystem**. The live OpenFold3 and Protenix-xjaw volumes are ext4 filesystems on managed Network SSD disks. Kubernetes `volumeMode: Filesystem` describes that mounted filesystem; it does not mean Nebius Shared Filesystem.

## Exact volumes

Read-only checks on 2026-09-15 at approximately 22:45–22:47 UTC confirmed:

| Cohort | PVC | Provider disk | Size | Documented size-derived ceiling* |
| --- | --- | --- | --- | --- |
| OpenFold3 | `fs2-bioir-snapshot/fs2-bioir-of3-checkpoints` | `computedisk-e00fs7s2hmh5s7vd6k` | 64 GiB | 30 MiB/s; 2,000 IOPS |
| Protenix-xjaw | `fs2-bioir-protenix/fs2-bioir-protenix-snapshot-xjaw` | `computedisk-e00w0kxpqcmx2b9tse` | 128 GiB | 60 MiB/s; 4,000 IOPS |

Both disks report `NETWORK_SSD`, READY, 4,096-byte blocks, and attachment to their assigned nodes. Both PVs use `compute.csi.nebius.com`, ext4, and `compute-csi-default-sc`. No explicit IOPS/throughput override appears in the returned disk fields or StorageClass; the class has no parameters. Exact PVC→PV→disk IDs, relevant provider responses and read commands are in [snapshot-storage.json](snapshot-storage.json).

*Calculated from current primary documentation: each 32 GiB Network SSD unit contributes up to 15 MiB/s and 1,000 IOPS in either direction. These are workload-dependent upper bounds, not measured throughput or guarantees. [Nebius disk performance](https://docs.nebius.com/compute/storage/types#disk-performance).

Saved manifests also establish OpenFold2's 64 GiB and Boltz2's 128 GiB RWO checkpoint PVCs, plus the interrupted Protenix cohort's 128 GiB PVC, all on the same StorageClass. Their provider disks were not queried in this check; OF2/Boltz checkpoint volumes were already cleaned. Boltz's separate RWX model-cache volume must not be confused with its checkpoint volume.

## What this means for timings

The existing [OF3 capture receipt](../snapshot/openfold3/raw/fs2-bioir-of3-donor/capture-stdout.json) measures 290.634 seconds of checkpoint flush, versus 1.148 seconds in CUDA checkpoint and 4.525 seconds in CRIU dump. The parent observed an approximately 8.5 GB durable bundle; this bounded check found no finalized inventory and does not derive a byte-rate from that estimate.

The modest documented storage ceilings are relevant context for the exact recipe. They do **not** establish disk saturation, explain a particular share of restore time, or prove an I/O bottleneck. No block-device throughput, cache-state, queue-depth, or per-stage restore I/O measurement was added.

Consequently, report these as results for the tested checkpoint storage, payload, initialization, and restore implementation—not generic snapshot-technology performance and not evidence that BIR is intrinsically slower. Performance on different storage remains unmeasured.

The Nebius CLI and Kubernetes skills constrained this check to an explicit profile/context and read-only inspection. No resizing, storage changes, new I/O benchmarks, or peer-evidence edits were performed.
