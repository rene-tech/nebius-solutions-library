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
