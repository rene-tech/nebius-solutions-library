# Speech snapshot restoration — 2026-09-16

Both clean resident NeMo workers were captured and restored in **fresh Pods**
on the same existing H100 nodes. Restoration did not fall back to ordinary
model loading. This is private-worker evidence, **not public cold-start or
multi-replica qualification**. Kernel 6.11.0-1016-nvidia, driver 580.159.04;
float32, greedy_batch, 560 ms, one session, CUDA graphs disabled.

| Measured phase | English | Multilingual |
| --- | ---: | ---: |
| CUDA checkpoint | 2.546 s | 3.157 s |
| CRIU capture | 62.743 s | 76.519 s |
| CRIU restore | 5.749 s | 7.344 s |
| CUDA restore | 0.952 s | 1.188 s |
| CUDA unlock | 0.016 s | 0.019 s |
| Sum of the three restore calls | 6.717 s | 8.552 s |

These sums **exclude** scheduling, image pull, supervisor setup, compatibility
checks, storage mounts and readiness propagation. One observation per model,
not p95. The shared snapshot PVC is `fs2-fleet-snapshots-rwx-r20260907`; no
H100 local NVMe claim. The original donor Pods were removed after capture;
checkpoint directories and evidence were retained. Different-GPU UUID remapping
was subsequently tested below; production controller publication remains open.

## Correctness checks after restore

The English restored worker processed both complete English consultations over
HTTP and WebSocket. The multilingual worker processed complete English case 01
and German Herzrasen over both transports. All four HTTP transcripts matched
the corresponding pre-snapshot medical transcripts **exactly**; every live
transcript and audio duration matched its file result. These live uploads were
unpaced; the separate medical cohort contains real-time-paced recordings.

| Recording | English HTTP / live | Multilingual HTTP / live |
| --- | ---: | ---: |
| English consultation 01, 457.92 s | 23.982 / 24.666 s | 24.462 / 24.390 s |
| English consultation 02, 559.20 s | 29.923 / 30.065 s | — |
| German Herzrasen, 421.86 s | — | 21.797 / 22.122 s |

Two additional, distinct synthetic native fixtures passed on each restored
worker, retaining “telescope” and “microscope.” They bind reproducible protocol
inputs in the native catalog; **they do not replace the complete medical
recordings, long-file tests or customer-path acceptance**. An English fixture
also exposes a missing space between adjacent transcript segments (`platformWe`);
that remains a text-assembly defect, not a passing quality claim.

## Reproduction and retained identities

- `render_snapshot_donor.py`, `render_snapshot_restore.py`: task-owned manifests.
- `en-snapshot-r2-capture.log`, `multi-snapshot-r2-capture.log`: capture records.
- `en-snapshot-r2-restore.log`, `multi-snapshot-r2-restore.log`: restoration phases
  and exact image, source, GPU and driver identities.
- `*-donor-r2-live.json`, `*-restore-r2.json`: exact Pod specifications.
- `*-restored-http-live-r2.json`: complete consultation transcripts/alignment.
- `*-native-contract-r3.json`: supplementary native protocol receipts.
- `server_probe.py`: temporary task-owned objects in Rene's existing bucket;
  signed URLs stay in memory/stdin, objects are deleted after the run.

Model-specific images carry checksummed pinned weights and run with
`HF_HUB_OFFLINE=1`. Runtime source is `7b286d489`; image digests are pinned in
the native catalog. In-process Inductor compilation avoids the forked CUDA
mapping problem retained in the failed English r1 capture log. No host security
settings, capacity limits or quotas were modified.

Public Apps use canonical IDs `nemotron-speech-en-0-6b` and
`nemotron-speech-multilingual-0-6b`. The measured private worker profile retains
its `0.6b` spelling; the gateway resolves this explicitly. The new catalog
declarations intentionally leave public-route, cold-start and scaling flags
false, and production snapshot startup disabled pending integration tests.

## Cross-node / different-GPU restore r3

The same immutable clean r2 bundles were restored on the existing reserved H100
nodes, explicitly remapping to different physical GPU UUIDs. Normal-load fallback
was disabled. Both workers completed two full medical recordings via HTTP and
unpaced WebSocket (eight results total); every transcript and decoded duration
exactly matched its earlier r2 result. This is additional private-worker evidence,
not production snapshot activation or a public cold-start SLA.

| Measurement | English | Multilingual |
|---|---:|---:|
| CRIU restore |5.913s|7.735s|
| CUDA restore |1.141s|1.227s|
| CUDA unlock |0.023s|0.021s|
| Sum of restore calls |7.077s|8.983s|
| Pod creation to restore-complete log marker |22.326s|16.860s|
| Immutable bundle bytes |8,501,526,502|10,901,327,725|
| Bundle file count |222|222|

One observation/model. Image/shared-filesystem caches were retained. The Pod
clock includes scheduling/init work but ends at the supervisor marker, **not**
at externally observed Ready or first result; it excludes node provisioning.
Do not compare it as a controlled speedup against an unrelated image-cold run.
Kernel/driver remain6.11.0-1016-nvidia/580.159.04, H10080GB, unchanged profiles.
Full parsed identities, remapping, per-recording timings and exact-match checks:
`cross-restore-analysis-r3.json`; reproducible parser `analyze_cross_restore.py`.
Raw source/Pod/log/HTTP/live evidence is retained with `cross-`/`r3` filenames.
Read-only bundle hash manifests are `en-snapshot-r2-manifest.json` and
`multi-snapshot-r2-manifest.json`; no customer audio entered the saved bundles.

The two task-only r3 restored Pods were removed after these checks and manifest
capture. Their exact live specifications/UIDs are retained in
`cross-restored-pods-before-cleanup-r3.json`. Shared snapshots and public Apps
remain. Matched repeated cold-start trials, production renderer/fallback and
public snapshot policy qualification are still outstanding.
