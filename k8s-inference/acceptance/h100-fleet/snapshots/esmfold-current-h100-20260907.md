# Current ESMFold2 and ESMFold2-Fast snapshots

Both exact current images passed three normal/restore pairs on H100, with donor deletion before every fresh-Pod restore and two distinct original controller-issued requests per run. All twelve trial Pods were deleted. The independently captured bundles restore on a different node from their donors; Fast does not inherit ordinary ESMFold2 state.

| Profile | Normal container→ready median (range) | Restore container→ready median (range) | Normal / restore first valid output median |
| --- | --- | --- | --- |
| ESMFold2 | 18.321s (17.340–20.087) | 16.110s (15.374–21.063) | 31.990s / 29.380s |
| ESMFold2-Fast | 18.884s (17.985–19.894) | 13.803s (13.443–22.503) | 31.517s / 28.580s |

Readiness is independently observed over HTTP after initialized model/CUDA state. The restored health field containing donor load time is excluded from restore measurements. First valid output includes fixture localization, original wrapper execution and full CIF/confidence validation. BF16 ESMC, flash attention, loops20, sampling steps200 and original seeds101/102 are unchanged. Inputs include the original short sequence and ubiquitin, with different raw-input hashes.

These are existing-cache, fresh-process tests, not disk-cold or reserved-RAM claims. Initial restore Pod paths took342.5s/220.6s because scheduling waited318s/194s respectively; retained Kubernetes events show only1–2s from tools-init start to runtime start. The actual first restore runtime clocks were21.063s/22.503s. Later Pod-create-to-ready clocks were17.7–18.8s. No capacity or resource limits were raised.

Two pre-runtime init attempts per profile failed when `cp -a` encountered shared-filesystem ACL metadata returning ENODATA. Failed attempts remain in the private receipt; they are not successful benchmark samples. The corrected init copies small mutable files with numeric UID/GID, ordinary modes, links and nanosecond timestamps using a private tar archive, without touching shared bundle metadata. Large CRIU images remain read-only mounts. Subsequent fresh restores passed.

Normal loading remains the default: the measured cached gain is modest and each snapshot consumes about28GB. Earlier disk-cold ESM restore measurements around325s belong to a different historical capture, not these trials. The installed option must match image, profile revision, captured source bytes, bundle digest and qualification receipt; production selection/smoke is a separate deployment step.

Evidence: [ESMFold2 report](esmfold2-h100-20260907.json), [ESMFold2-Fast report](esmfold2-fast-h100-20260907.json), [ordinary bundle](esmfold2-bundle.json), [Fast bundle](esmfold2-fast-bundle.json). The report generator retains the private receipt hash and failed-attempt count. Original localization markers, argv, full outputs and Pod manifests stay in the private task evidence directory.
