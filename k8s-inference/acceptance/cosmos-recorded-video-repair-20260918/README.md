# Recorded Cosmos video repair — 18 September 2026

This is a preparation and evidence gate, not a declaration that the public
LeRobot customer workflow is complete. `prepare.py` never applies resources.

## Findings and isolated candidate

The frozen customer request used real recorded ALOHA coffee imagery: 640×480,
64 frames, 25 FPS, seed 20260918, 35 steps and guidance 6. The original operation
and independent diagnostic replay failed after CUDA/CRIU restore. The worker
completed denoising but WanVAE decoding raised CUDA `operation not supported`
at `_upsample_nearest_exact2d`. Identical fresh loading on the same physical H100
succeeded. This isolates a restore compatibility defect; it does not establish
which asynchronous CUDA state originally caused it.

Fresh inference exposed a separate contract failure: temporal compression padded
64 requested frames to 65. The adapter now validates actual decoded dimensions,
FPS and frame count, removing only a known padded tail with lossless encoding.
An attempted stream-copy solution failed B-frame pixel-preservation tests and
was discarded. Tests compare the first N decoded pixels and audio bytes, not
merely headers. Generated audio is preserved even when longer than the video.

Transfer had two explicit-dimension overwrites, in preprocessing and bucket
selection. A derivative of the exact existing image patches those two sites,
guarded by the original Python source SHA256. No weights, precision or model
kernels change; default bucket behavior remains intact. Explicit dimensions
are honored before generation, never by post-hoc camera resizing.

Candidate image:
`cr.eu-north1.nebius.cloud/e00akg9ndpx77eaexh/fs2-models/cosmos3-contract@sha256:5e2680aa1f8332413638ec1bc962c3796a79a314c1c84f5456d32aa916839e32`

Source: `711590a103384931e5378c1b5d6cea76a6f0d3a3`. Adapter source is unchanged
from `3be8a63ea`. Snapshot log-bridge versioning is separate (`7993ef0c8`): the
old entrypoint stays byte-identical, and new bundles explicitly select the new
entrypoint hash. No historical snapshot records are rewritten.

Isolated v4 evidence on one H100 80GB, driver 580.159.04:

| Test | Outcome |
| --- | --- |
| Frozen recorded V2V | 14.01 s; exactly 64 frames, 640×480, 25 FPS |
| Four V2V shapes/frame counts | All passed |
| T2V and I2V | Both passed |
| Recorded edge transfer | 50.19 s; exactly 64 frames, 640×480, 25 FPS |
| Video plus longer generated audio | Passed; audio retained |
| Three concurrent private adapter calls | Active and queued calls completed; third got bounded 429/Retry-After |

These are 11 successful generations and one expected queue rejection. They do
not establish public key fairness, cold-start/scale-out timing or new snapshot
compatibility. All v1/v2/v3 failures remain in the protected evidence tree.

An independent fresh V4 Pod also passed three differing-frame-rate requests
against the same recorded 25-FPS source. Edge transfer returned exactly 64 frames
at requested 24 FPS (50.30 s) and 30 FPS (49.92 s); first-frame V2V returned 64 frames
at 24 FPS (13.88 s). All outputs are 640x480. This explicitly changes playback duration,
not the number of frames: 2.666667 s at 24 FPS and 2.133333 s at 30 FPS. LeRobot always
preserves source FPS for alignment. The pinned video-serving encoder prioritizes
explicit request FPS over pipeline metadata; pipeline conditioning FPS alone is
not sufficient to infer final output timing. No FPS patch was necessary.
Receipts, decoded metadata and pinned encoder source identity are in
`isolated-fresh-v5`.

A first separately warmed V4 snapshot capture failed: CUDA checkpoint
completed in 15.31 s, but CRIU reached the existing 600 s dump timeout after writing
approximately 42 GiB to shared storage. The helper recovered the donor and the
isolated Pod was removed. Original r7 and the failed new directory are preserved;
neither is a valid snapshot for this successor. Fresh inference and this capture
failure remain independent acceptance results.

A second capture of the fully exercised runtime changed only CRIU dump logging
from debug (`-v4`) to warning (`-v2`), leaving the same 600-second timeout,
resource bounds, precision, weights and checkpoint flags. The approximately
42-GiB capture completed: CUDA 15.78 s, CRIU 133.65 s, flush 2.37 s,
153.73 s total command/transport time. Required shared-memory files were then
captured separately with their hashes. The original r7 remains unselected;
neither an incomplete directory nor a CUDA-only checkpoint is publishable.

Two subsequent isolated Pod lifecycles restored that checkpoint with
`fallback=fail`. On the original physical H100, CRIU restore took 30.40 s and
CUDA restore 5.61 s. Pod creation to Ready was 54 s, including 14 s of scheduling
wait; container start to Ready was approximately 37 s. This is a same-GPU,
warm-host-cache experiment, **not an empty-node cold-start measurement**.
The frozen recorded request and ten broader successful video outputs were
byte-identical to the donor, and two previously unseen shapes/seeds passed.
A different-node/GPU restore is a separate gate, not implied by these results.

That separate gate subsequently passed on a different reserved H100 node and
GPU UUID, with the same driver/kernel/runtime and strict fresh-load fallback
disabled. CRIU took 28.53 s and CUDA restore 6.04 s. The original request plus
two unseen shapes and six control/repeat calls all passed; all nine outputs
matched the same-GPU restored outputs byte-for-byte. The previously uncached
9.19-GB image took 164.59 s to pull, before restore. A snapshot does not remove
the image dependency; total empty-image-cache start time must include it.

The optional bundle contract separates immutable shared-storage `bundle_path`
from `captured_directory`, the relative path beneath `/checkpoints` that the
captured process used. Omission retains historical `bundle_path` behavior and
byte-identical Pod rendering. Only the storage prefix is used for read-only PVC
subPaths; scratch/cache/log paths retain the captured directory. This allows a
unique new capture directory in shared storage without overwriting r7 or
changing absolute process paths. Both paths must be contained, canonical
relative paths. Terraform chooses `process_checkpoint.py` content by its
recorded digest, allowing the separately frozen quiet helper while preserving
the original source bytes and all historical bundles. These packaging changes
do not select a snapshot, change a customer cache policy or qualify other GPUs.

The final optional package is in `warmed-snapshot/`: a bound qualification
report, `bundle.json`, and the exact isolated `renderer-pod.json` used for the
fourth strict restore lifecycle. Its read-only inventory covers
44,626,867,078 bytes, 487 files and 563 filesystem entries (including modes and
ownership), with manifest SHA256
`573bf6acacb90461ca0cc06f850f550dc48e34579d79f866da2b1691097ff120`.
`prepare_snapshot.py` checks the immutable inventory, successful capture,
exact runtime identity, decoded outputs and cross-GPU content hashes before
constructing the package. It never applies resources or selects a cache level.

The real production renderer was then exercised against the unchanged V4
template, using its native resource bounds and probes, with `fallback=fail`.
The original recorded request and two unseen shapes succeeded and matched
the prior outputs byte-for-byte. That Pod reached Ready 35 seconds after
container start (approximately 40 seconds from scheduling, with image/cache
already present). It has been deleted. This verifies the optional bundle's
storage-path and wrapper integration, not public request routing or cold-image
latency. Its report remains `production_selectable: false` until coordinated
registry publication and an ordinary public request verify that path.
The historical `cosmos3-nano-bundle.json`, old r7 files and owner policy remain
unchanged. The new package is optional input to the existing GPU snapshot
registry; it is not automatically included by a global Terraform default.

Reproducibility is scoped precisely: all four restores reproduce their retained
warmed donor byte-for-byte. The public fresh V4 output instead matches three
earlier fresh V4 runs. Those two retained outputs use the same scientific input,
seed and settings but have decoded SSIM 0.979943, not byte identity. The cause of
this fresh-process/warm-up difference is not established. Do not advertise
seed-exact equivalence across arbitrary initialization states, or interpret
SSIM between two generated outputs as scientific or robotics-task accuracy.

The broader run exposed an independent adapter bug: explicitly requested
derived edge/blur presets were dropped when `control_weight` remained its
default 1.0. A separately versioned adapter candidate preserves those supplied
presets, while retaining the original boolean shorthand when no preset is
provided. CPU tests cover both paths. On a second strict restored lifecycle,
edge `very_low` versus `very_high` and blur `low` versus `high` each produced
distinct, valid outputs; exact repeated low-preset requests were byte-identical.
These six calls took 48.4–54.0 s at 640×480, 25 frames, 25 FPS. This is evidence
that these controls reach the model, not a robotics-task accuracy claim.
This successor adapter is **not** the immutable V4 adapter in the first public
promotion package. It must receive its own digest/template and live acceptance.

`prepare_presets.py` prepares that separate adapter-only successor against
supplied envelope, template-bundle and owner captures. It verifies the frozen
adapter source hash and measured low/high/repeated edge/blur contrast on both
GPUs, then adds a content-addressed ConfigMap and allowed template. Images,
weights, native arguments, every owner setting and historical snapshots remain
unchanged. The helper performs no apply; the release coordinator must rebase
on the latest stable maps, preserve sibling records, and run ordinary public
acceptance after selecting the successor.

The public V4 native replay completed on 18 September at 22:49:53 UTC, operation
`a3bd2106-64f6-4dda-b7b5-8d0d53c2e532`. The original request produced a hash-verified
2,085,126-byte MP4 with 64 decoded frames, 640×480 and 25 FPS. Inference was
15.66 s after activation; admission-to-completion was approximately 464 s,
including capacity wait, new-node image preparation and runtime startup.
The dataset-level LeRobot workflow remains a separate acceptance gate.

First-frame V2V changed robot pose/motion. Edge transfer better followed the
recorded pose in visual inspection, but altered materials and details. Neither
is a verified lighting-only transform or proof of downstream robotics-policy
efficacy. Dataset timestamps/actions must remain byte/semantically unchanged,
and a scientist must evaluate augmented imagery before using it for training.

## Deployment identities

Cosmos has a canonical non-NIM record but no fallback variant or archival static
serving binding. `contracts/canonical-runtime-services.json` explicitly records
its existing managed Service (`fs2-models/cosmos3-nano:8080`), bound to the
original model-record digest and source kind/repository/revision. This agrees
with the retained renderer template
`sha256:a4c96a343622a0e809e61f132c71032dd6e845c9ea7d846563cdb0ce36cf0fab`.
It does not grant a route or choose a new port.

The generic selected-runtime validator permits a null-variant successor only
when such a reviewed declaration exists. Model/source/license, weights/artifact
inventory/cache owner and policy remain unchanged. New entries cannot choose
their own Service or pass NIM content through this path. Existing variant
checks remain unchanged. The actual Registry startup and admin bootstrap are
part of preparation, alongside all current owner/render validations.

The successor versions the adapter ConfigMap and renderer template, preserves
scaling/placement/customer settings and other Apps, disables r7 restore only
for the new owner (`snapshotPreference: Never`), and retains old bundles and
rollback proposals. Qualification for public MCP, cold start, elasticity and
snapshots is deliberately not inherited. Apply only against a freshly captured
stable release with all owner transitions finished, then run customer-path and
real LeRobot output validation before claiming readiness.

Protected evidence (no credentials committed):
`/home/tux/secure-handoff/scientific-qualification-20260918/lerobot-admission-diagnosis/`.
The `isolated-fresh-v4` receipts include output hashes, ffprobe metadata,
runtime/GPU identity and source hashes. `native-replay-r1-after160` retains the
customer-scoped restore failure and actual worker stderr.

The approved candidate descriptor is persisted separately as
`catalog/runtime/deployment-runtimes/cosmos3-nano-recorded-h100-20260918.json`.
Terraform selects it only when the operator's immutable image override matches
its digest; it does not replace a global default. The release owner must retain
that image selection alongside the accepted dynamic owner proposal. Later admin
scaling/placement edits remain authoritative and must not be overwritten by an
old promotion capture. This step changes no live Terraform values.
