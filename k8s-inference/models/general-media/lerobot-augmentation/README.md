# Cosmos3-Nano LeRobot augmentation App

This directory owns the dataset workflow around Cosmos3-Nano. It is a separate
App from the single-video Cosmos App: callers submit a dataset reference and a
typed augmentation policy, and receive one complete LeRobot dataset artifact per
variant. Cosmos receives individual videos, prompts, and controls; it never
receives a client-local dataset path.

## Frozen compatibility

- LeRobot dataset format: v3.0.
- LeRobot reader/writer: `lerobot==0.6.1`, source revision
  `7e241bd630a3719a56157a497ce5d08f244784f1`.
- Model: `nvidia/Cosmos3-Nano` revision
  `7a312c868bcce8e40b3eb40861300a9d0ba3fde1`.
- Serving contract: vLLM-Omni revision
  `eb11446b7f2e30ca582f8aff3afe12e9a2e66f6c`.

The LeRobot v3 reader is the acceptance authority. A run is not successful merely
because Cosmos returned MP4 bytes: every output must be finalized, reopened with
the pinned `LeRobotDataset`, and have every output frame decoded before the
artifact is published.

## Input references

The request schema accepts exactly three source kinds:

1. `huggingface`: a dataset repository ID plus an immutable 40-hex revision.
   Credentials, when needed, come from the tenant's server-side credential
   binding and never from the request.
2. `object-store`: a server-configured storage binding plus a relative object
   prefix and expected bundle manifest digest. A raw bucket URL or bearer URL is
   not accepted.
3. `uploaded-bundle`: the canonical scientific input artifact. The bundle is a
   zstd-compressed tar with a checked file inventory.

Paths on a participant's laptop are deliberately invalid. Upload the bundle or
place it in an authorized object/Hugging Face dataset repository first.

All localized inputs use `fs2-bundle-manifest.json`, containing a sorted `files`
list with `path`, `size_bytes`, and `sha256`. Uploaded and object-store bundles
must already contain it; localization creates it for an exact immutable Hugging
Face revision. The worker verifies the inventory before importing LeRobot or
contacting a GPU.

## Public scientific-run envelope

The App is an independent catalog identity,
`cosmos3-lerobot-augmentation`; it is not an operation on the single-video
`cosmos3-nano` App. Its eventual HTTP route is
`POST /v1/models/cosmos3-lerobot-augmentation:submit`, and its typed MCP tool is
`submit_cosmos3_lerobot_augmentation`. Both accept the canonical
`fs2-serve.nebius.ai/scientific-run-request/v1` envelope. The augmentation
object is nested under `parameters`, not passed as top-level tool arguments.
See `fixtures/scientific-run-request.json`.

The outer `input_manifest` points to a finalized JSON artifact with schema
`fs2-serve.nebius.ai/scientific-artifact-manifest/v1` and exactly one logical
entry:

- uploaded bundle: name `lerobot-dataset`, semantic type
  `lerobot-v3-bundle/v1`, media type `application/x-tar`, compression `zstd`;
  its artifact pointer must exactly equal `parameters.source`;
- Hugging Face or object-store source: name `lerobot-source`, semantic type
  `lerobot-source-reference/v1`, media type `application/json`, compression
  `none`. Its JSON is exactly `{"schema":
  "fs2-serve.nebius.ai/lerobot-source-reference/v1", "source": ...}`, where
  `source` is the submitted source object (including a `null`
  `credential_binding` when none is used).

The second form prevents a request from switching a reviewed external source
after artifact admission. Examples are in `fixtures/source-reference.json` and
`fixtures/scientific-input-manifest.json`.

## Dataset behavior

The first implementation preserves the source episode count. For each requested
variant it creates a complete new dataset:

- selected camera frames are replaced by Cosmos output;
- unselected cameras and episodes are copied through the LeRobot reader/writer;
- timestamps and frame indexes are recreated at the original integer FPS;
- task, action, state, and other non-index features are preserved by default;
- the source action trajectory is preserved exactly;
- `dataset.finalize()` is mandatory before validation and packaging.

Forward- and inverse-dynamics are deliberately not accepted by this App. Live
qualification of the pinned H100 runtime on 2026-09-15 reached diffusion/decode
but then killed its worker with SIGBUS; a restored CUDA snapshot failed forward
dynamics earlier with `cudaErrorNotSupported`. Those modes may be added only as
a new, separately qualified contract. Publishing visually changed clips with
unverified replacement actions would corrupt the LeRobot training contract.

Every selected episode must contain 16 to 400 frames, and every selected video
feature must be RGB, no larger than 1280x720, with width and height divisible by
16. These are admission bounds for the pinned Cosmos recipe, not general LeRobot
limits. Longer episodes require a separately qualified chunk/stitch contract.

## Queue, progress, and artifacts

The App is intended to use the existing `scientific-batch-v1` admission,
idempotency, priority, cancellation, Kueue, object-artifact, usage-accounting, and
Apps-admin paths. The worker writes bounded JSON-lines progress and a terminal
result manifest for the existing companion collector. It is a CPU dataset worker
that submits one child Cosmos operation at a time through scoped control-plane
routes: upload/finalize the episode MP4, admit generation, poll its durable
operation, then download the resulting artifact. The same serving admission,
model grants, request budgets, GPU reservations, queues, and usage records apply.
The initial coordinator processes variants and cameras sequentially.

The renderer injects `FS2_SCIENTIFIC_WORKLOAD_CAPABILITY` and
`FS2_SCIENTIFIC_INTERNAL_API_URL` into the exact LeRobot model stage. The worker
uses `/internal/scientific-workloads/cosmos/v1/...`; the credential is the existing
signed scientific attempt capability. No customer bearer is copied into the
container. The control plane resolves the original tenant, principal, and token
from the persisted parent and checks its current policy on every request.

PostgreSQL migration `0032_scientific_child_delegation.sql` adds
`parent_operation_id` and `parent_attempt_id` to child operations. An upload or
generation shares its active parent's concurrency slot, including when the
customer limit is one. Only one child per parent can be active, enforced both
under the existing token lock and by a partial unique index. Every upload and
generation retains its own ordinary request and GPU accounting. Idempotency is
namespaced by attempt; a retried parent cannot adopt a previous attempt's child.
Cancellation, terminal parents, attempt replacement, and token revocation fence
further admissions and are checked by the worker heartbeat and queue cleanup.
Artifact and operation reads are restricted to children of that exact attempt.

Apply the migration before deploying the updated control plane. Publish a new
worker image from this tree and bind its digest in the execution map; the old
worker expects public bearer authentication and is incompatible with these
scoped routes. The compiler and collector are installed, while catalog exposure
remains gated independently by qualification.

No route is customer-ready yet. Promotion requires a digest-pinned worker image,
catalog/execution-map integration, live public-path runs for two distinct
augmentation dimensions (one lighting), and successful reload of both published
datasets through `LeRobotDataset`.

Local evidence from 2026-09-17 covers the LeRobot reader round trip, the scoped
client transport, and real PostgreSQL concurrency, accounting, replay, and stale
attempt fencing, including current owner disable/app restrictions on delegated
HTTP calls. The [local CPU build receipt](activation/local-build-20260917.json)
records the non-root image's offline fixture encode/normalize/decode check. Its
local image ID is not a publishable registry manifest digest. This evidence does
not qualify a deployed GPU workflow or the Apps UI.

## Local validation

Run the complete suite (the LeRobot round trip needs the pinned runtime
dependencies, PyAV, and FFmpeg):

```bash
python -m pytest models/general-media/lerobot-augmentation/tests
```

Build the locked non-root CPU worker image:

```bash
docker build \
  -f models/general-media/lerobot-augmentation/runtime/Containerfile \
  models/general-media/lerobot-augmentation/runtime
```
