# LeRobot untouched-media preservation (Q64)

The original public128-row dataset preserved numeric fields but re-encoded the
unselected wrist camera: mean pixel differences1.298/1.263 across its two
episodes. The historical tolerance-based acceptance remains retained; it is not
retrospectively labeled byte-exact.

The source fix copies original MP4 shards without trimming, remuxing or
re-encoding. Untouched `(episode, camera)` pairs retain their original offsets in
a disjoint chunk namespace. A selected and an untouched episode can share the
source shard without the copy overwriting the generated selected reference.
Corresponding original episode video statistics are restored and aggregate
statistics are recomputed with the pinned LeRobot helper. Numeric data shards
and selected generated references are unchanged by this preservation step.

Temporary redundant writer encoding remains a performance cost before reference
replacement. The pinned writer's feature video info describes the first clip;
if that first clip is copied, its source info is restored. No model settings,
dependency versions, resource limits, quotas or API bounds are changed.

## Reproduce the bounded evidence

- Source repair: `54ccd19a34f2177eb713465f32e2de3df8024fd9`.
- Initial image recipe source: `199ed895c806c996d61ff40a811869ce5cc6bf45`,
  `runtime/Containerfile.untouched-media`; base is deployed coordinator41a01714…,
  overlay is only `dataset.py`. Historical image recipes remain unchanged.
- Initial candidate image (retained, not selected):
  `cr.eu-north1.nebius.cloud/e00akg9ndpx77eaexh/fs2-platform/fs2-lerobot-augmentation@sha256:dfc0622c0a889d95a44c91340f293d390299216d2346fd0562f0490c44d75a88`.
- Final source: `4177c2a2cabd99a8e5882f29a392105d859b16f2`; final image:
  `cr.eu-north1.nebius.cloud/e00akg9ndpx77eaexh/fs2-platform/fs2-lerobot-augmentation@sha256:69fe161d370fac4cc9517d24ecfee7b025bb569a935ffadab8eb31e49c783a11`.
- The recorded fixture is the pinned two-episode/128-row ALOHA source. Generated
  content comes from the retained real public v36 variant, not a synthetic image
  or new GPU call. Set `FS2_LEROBOT_RECORDED_ALOHA` and
  `FS2_LEROBOT_RETAINED_VARIANT` for `tests/test_untouched_media.py`.
- Five focused real-reader tests pass; broader worker suite99 passed/1 optional
  old preview fixture skipped. All new exact-preservation tests ran. The retained
 28 upstream fork deprecation warnings are not silently removed.
- `qualify_image.py` runs inside the exact published image with no source-code
  mount and no network. Inputs are read-only `source/`, `generated/`, and a
  hash-bound `manifest.json`. It rebuilds, packages, reopens and decodes all256
  frame/camera values per dataset, checks6144 numeric values, MP4 hashes, all
  untouched pixels, offsets, and unchanged selected/numeric writer bytes.
- The first container probe failed before loading code because the test input
  root inherited0700 from umask. The failure is retained. The successor explicitly
  sets0750/0640 group-readable inputs; this is harness access, not a runtime fix.

## Same-limit CPU comparison

The original completed successfully, but186 process threads and heavy cgroup
throttling suggested CPU-library oversubscription. One matched run changed only
`OMP_NUM_THREADS=4`, `MKL_NUM_THREADS=4`, and `OPENBLAS_NUM_THREADS=4`.
The successor image sets these standard, overridable defaults for the existing
coordinator4-CPU limit; future CPU-profile changes require retuning. Torch
inter-op remains64; no resources, library versions or model parameters changed.

| Trial | Wall seconds | Limits | Result |
| --- | ---: | --- | --- |
| Initial published image, inherited library defaults | ~492.99 | 4 CPU / 24 GiB | Pass |
| Same image and inputs, three environment overrides | 73.29 | Same | Pass |
| Final published image defaults, no overrides | 73.23 | Same | Pass |

The first wall time uses command/receipt filesystem timestamps; later times use
a monotonic subprocess clock. This is one fixture, not a general throughput
benchmark. The observed matched speed ratio is approximately6.73×. All MP4,
numeric Parquet and episode-metadata bytes match between trials; every untouched
pixel and all6144 source numeric values are exact. JSON statistics differ only
in serialization order and are equal after parsing, so archive digests differ.
Temporary redundant writer encoding remains performance debt.

`image-qualification.json` SHA256
`c3ba08f056c6f9cbe42125308741aa011b7a44a7ba21a56e00359bc66181cba9`
binds the actual final image. `thread-comparison.json` SHA256
`ff314eccc4257d0ff059981a76b3250977b0c16b586ac00c36221e3c9ee38ea2`
retains both earlier receipt hashes and timing bases. The publication receipt in
the model's activation directory binds source/tree/context, registry manifest,
configuration and SBOM hashes, and the exact build/qualification constraints.

## Promotion boundary

`prepare_promotion.py` reuses `acceptance/scientific_runtime_successor.py` and the
actual Registry/admin/bootstrap/scientific-renderer/scheduler/Helm validators.
The exact published-image receipt is mandatory. It preserves every sibling
execution row and snapshot bundle, rebases only justified sibling qualification
references, and resets LeRobot public/scheduler qualification for the new image.
The existing legacy `h100_semantic_receipt` field is explicitly scoped here to
CPU coordinator plus retained GPU media; it is not fresh H100 inference.

The complete private values candidate replaces the scientific execution-map
subtree, not a recursive Helm merge. The release owner must combine it with the
then-current serving-map references; no helper applies or deploys anything.
Fresh public worker completion remains required. Byte/pixel preservation is not
physical action alignment, augmentation quality, policy-training validity or
combined customer readiness.

The baseline177 successor passed actual whole-map Registry/admin bootstrap,
scientific renderer and scheduling checks for11 models, plus Helm rendering.
All10 sibling execution rows and four live snapshot bundles are unchanged;
only justified qualification references are rebased. Source recipe
`68871cd26b62f00a1c436fd24bf73a961acb2f691d2ae9a5c3eb13d1e5bb5dda`
includes the public guidance-only schema commit`e99c7e7b0`. No live deployment
or ordinary public acceptance was performed by this helper.
