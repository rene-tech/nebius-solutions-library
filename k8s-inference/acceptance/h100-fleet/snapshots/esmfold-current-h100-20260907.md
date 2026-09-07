# Current ESMFold2 and ESMFold2-Fast snapshots

Corrected ESMFold2 r5 and ESMFold2-Fast r4 each passed three normal/restore pairs on H100 on 2026-09-07. Both original controller-issued requests passed in every trial, including the real production confidence collector: 24 validated outputs across twelve fresh trial Pods. Donors were deleted before restore, all trial Pods were deleted afterward, and every restore ran on a different node from its donor. Production option selection/public acceptance remains a separate release step; normal loading is unchanged.

| Profile | Normal container→ready median (range) | Restore container→ready median (range) | Normal / restore first valid output median |
| --- | --- | --- | --- |
| ESMFold2 | 20.611s (18.264–60.364) | 13.317s (12.067–15.335) | 33.732s / 25.499s |
| ESMFold2-Fast | 19.104s (18.459–20.442) | 11.754s (11.213–15.659) | 32.124s / 23.912s |

These are n=3 medians/min/max, not percentile estimates. Readiness is independently observed over HTTP after initialized model/CUDA state. Restored health fields retaining donor load time are not restore measurements. First valid output includes fixture localization, original wrapper execution and full CIF/confidence validation. BF16 ESMC, flash attention, loops20, sampling steps200 and seeds101/102 are unchanged. The short and ubiquitin inputs retain their original different raw-input hashes and 320/601 validated atoms.

## Exact identity and cache boundaries

The actual checkpoint/weight revisions are `8fc3ff471022fdce52c77030685eb775de0c00a3` for ESMFold2 and `c6c7958d63f5f2f1f0fed0bb9462316f8ccceea6` for Fast. Both retain the distinct source/profile revision `827ec128e4cdaf80f7d6f95fb367a08980b34918`. These values are separately present in the bundle, report and capability projection. Exact immutable image digests and frozen source hashes remain in each report; Fast uses its own independently captured state. Hardware was H100 80GB HBM3, SM9.0, driver 580.159.04, kernel 6.11.0-1016-nvidia.

Shared data and caches were not deliberately evicted, but ESMFold2 normal2 ran on a newly autoscaled H100 node. Its model image genuinely pulled in 69.252s and its container→ready time was 60.364s; the full Pod-create request→ready path was 168.688s. This is not an all-page-cache-hot cohort. That observed run remains in the range and median calculation. Image pull, scheduling and init are excluded only from the container-start clock, not from the full Pod clock. No reserved-RAM or controlled disk-cold claim is made.

Full Pod-request→ready normal/restore medians were 23.991s/15.613s for ESMFold2 and 24.194s/15.127s for Fast. Their first restore Pod paths were 60.454s/56.978s, including queue time; actual container→ready was 15.335s/15.659s. Trial receipts retain actual node IDs; acquisition events for the new-node ESMFold2 trial are preserved privately. Existing pool autoscaling supplied capacity without raising a quota or pool ceiling.

## Capture and retained failures

Both corrected donors wrote directly to unique prefixes on the existing shared snapshot PVC, followed by durable flush/hash verification without another full copy. ESMFold2 r5 contains 16,367,286,304 bytes and Fast r4 contains 16,202,161,971 bytes, each 228 files. One-time CUDA-checkpoint/CRIU-dump/durable-flush times were 4.936s/204.088s/0.829s and 5.045s/205.883s/1.005s respectively. These are capture costs, not restore startup times. Frozen runtime sources and the original model settings were not changed during recovery.

The historical [ESMFold2 r2 report](esmfold2-h100-20260907-r2-historical.json) and [Fast r2 report](esmfold2-fast-h100-20260907-r2-historical.json) are preserved byte-for-byte. They proved isolated CUDA/structure restoration but used the source revision as the weight identity and did not include the production confidence collector. They do not qualify the current production identity and are not mixed into these new triples. Their old scheduler delays and init ENODATA failures remain historical evidence.

The subsequent Fast r3 restore failed because its root donor inherited capture-only `SYS_RESOURCE`, absent from the production restore capability set. ESMFold2 r4 had the same mismatch and was not retried as a GPU restore after diagnosis; its completed source and verified shared copy remain intact. The correction removes only that unnecessary donor capability, matching the existing five-cap production restore set without widening it. Earlier partial capture/disk-full failure receipts also remain private. No successful checkpoint was deleted or disk expanded during this recovery.

## Current evidence

[ESMFold2 report](esmfold2-h100-20260907.json), [Fast report](esmfold2-fast-h100-20260907.json), [ESMFold2 r5 bundle](esmfold2-bundle.json), [Fast r4 bundle](esmfold2-fast-bundle.json). Reports bind their private receipt hashes and include the production confidence result for every output. Original argv, localized fixtures, full outputs, lifecycle logs, publication manifests and failed-attempt receipts remain under the private `fs2-h100-fleet-snapshots-r20260907/{esmfold2,esmfold2-fast}` evidence directories. Normal-load remains the default until the release owner separately proves production option selection and public outputs.
