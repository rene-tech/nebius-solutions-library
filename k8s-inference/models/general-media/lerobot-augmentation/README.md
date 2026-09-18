# Cosmos3-Nano LeRobot augmentation App

This directory owns the dataset workflow around Cosmos3-Nano. It is a separate
App from the single-video Cosmos App: callers submit a dataset reference and a
typed augmentation policy, and receive one complete LeRobot dataset artifact per
variant. Cosmos receives individual videos, prompts, and controls; it never
receives a client-local dataset path.

The real release148 public MCP upload → augment → download → LeRobot reopen
path passed on 2026-09-18. The [scoped completion receipt](activation/qualification/public-completion-r148.json)
qualifies dataset/media mechanics, not strict visual preservation or physical
action alignment. Two unchanged final-release cohorts are a separate acceptance
step. Use the [ordinary-key directory client](../../../acceptance/lerobot-customer-20260917/README.md)
to run a local dataset without manually constructing artifact manifests.

Release149 passed four single-variant HTTP/MCP dataset-reader cases, then its
two-variant blur case failed on repeated upload content PUT after finalization.
The [negative receipt](activation/debug-failure-r149.json) retains that failure;
the corrected successor requires its own public proof and unchanged cohorts.

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

## Generative changes, not calibrated or action-aligned transformations

`augmentation.dimensions[].strength` is a **prompt-only annotation**. For example,
`0.7` becomes text such as `lighting (strength=0.70)` in `{variation}` or
`{instruction}`. It is not a native denoising parameter, a 70% edit amount, or a
calibrated geometry-preservation control. A prompt template that omits both
placeholders does not use that annotation. The frozen transfer worker separately
sets each selected edge/blur control's native `control_weight` to `1.0`.
`guidance_scale` and `num_inference_steps` are native parameters; they do not make
dimension strength a calibrated quantity.

The first successful public lighting case (edge transfer, 35 steps, guidance 6,
textual strength 0.7) produced an obvious warm/golden appearance change. In
beginning/middle/end samples, the broad bin layout and gripper movement remained
recognizable, but fruit-like objects changed colors/shapes and fine robot
contours/materials changed. This was **not a strictly lighting-only edit**.
There was no blank frame or gross scene cut in those three samples; this sparse
review cannot exclude flicker or other defects between them.

That run's complete two-episode/two-camera output passed the pinned reader,
decoded all 128 camera frames, and retained its stored nonvideo values exactly.
Those facts qualify pipeline/data integrity, **not physical action alignment,
grasp/contact correctness, or downstream training validity**. The reference
fixture itself contains public model-generated imagery/actions, not calibrated
robot telemetry. Review generated trajectories, objects and contacts for the
intended use before treating preserved action arrays as valid labels. No output
mode guarantees their physical consistency with the generated imagery.

The release149 environment samples did not visibly achieve the requested clean
laboratory/pale-blue background and showed arm trajectory/contact and wrist-pose
changes. Their successful full-reader checks establish dataset integrity, not
successful prompt intent, synchronized multi-view edits or action alignment.

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
`cosmos3-nano` App. Its HTTP contract is
`POST /v1/models/cosmos3-lerobot-augmentation:submit`, and its typed MCP tool is
`submit_cosmos3_lerobot_augmentation`. Both accept the canonical
`fs2-serve.nebius.ai/scientific-run-request/v1` envelope. The augmentation
object is nested under `parameters`, not passed as top-level tool arguments.
See `fixtures/scientific-run-request.json`.

The [additive activation procedure](activation/README.md) packages the canonical
profile/schema and preserves the complete existing deployment. Bootstrap
`active` discovery is explicitly unqualified. The historical qualified profile
binds the actual release148 public completion and scheduler receipts. The
upload-reuse successor clears those current proof pointers until its own public
run succeeds; historical receipts and limitations remain retained. A caller needs
both model grants plus `artifacts.write`, `operations.result`, `inference.invoke`,
`operations.read` and `catalog.read`; cancellation additionally requires
`operations.cancel`. The parent and its sequential delegated child share the
key's existing concurrency slot, including a limit of one.
MCP callers additionally need `mcp.invoke`.

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
- canonical fixed-rate timestamps and frame indexes are retained at the original
  integer FPS and source dtype; noncanonical timing is rejected before generation;
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

Compressed source and each output are limited to 5 GiB, expanded source to
8 GiB, and CPU workspace to 32 GiB. The worker checks the full dataset and
requested variant workspace before GPU generation. Generation caps apply only
to selected clips; short or smaller untouched streams remain valid. Full
[runtime limits and numeric-preservation constraints](runtime/README.md) apply.

Source timestamps must already equal `frame_index / fps` in their stored float
dtype, with canonical first-occurrence task-index ordering; unsupported records
are rejected rather than silently normalized. Numeric preservation does not
guarantee matching generated motion: even edge transfer can change object
appearance and fine robot details. The actual lighting run changed fruit colors,
shapes/surfaces and gripper details as well as lighting. Inspect all output before
training; strictly lighting-only edits and physical action fidelity are unproven.

## Queue, progress, and artifacts

The App uses the existing `scientific-batch-v1` admission,
idempotency, priority, cancellation, Kueue, object-artifact, usage-accounting, and
Apps-admin paths. The worker writes bounded JSON-lines progress and a terminal
result manifest for the existing companion collector. It is a CPU dataset worker
that submits one child Cosmos operation at a time through scoped control-plane
routes: upload/finalize the episode MP4, admit generation, poll its durable
operation, then download the resulting artifact. The same serving admission,
model grants, request budgets, GPU reservations, queues, and usage records apply.
The initial coordinator processes variants and cameras sequentially.

Operationally, this coordinator is CPU-only and is not GPU-snapshotted. GPU
snapshot restore, hot-replica counts, idle timeout and scaling settings belong
to the existing `cosmos3-nano` App that its delegated calls use. Changing the
dataset App's CPU resources does not change that Cosmos serving policy.

After terminal success, fetch the scientific result and its finalized
`output_manifest`. Download the public artifact UUIDs in that manifest. Worker
result labels such as `<parent>.variant-00` are logical names, not public download
IDs; match `variant_index` plus SHA-256, size, media type and compression to the
manifest before downloading and reopening the corresponding complete dataset.

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

The release148 debug workflow passed with the pinned worker, actual parent and
two attributed children, concurrency-one sharing, both idempotent replays, and
full independent dataset reload. It is not the final customer release: two
unchanged-release cohorts covering lighting/environment, cancellation and the
ordinary HTTP/MCP surfaces remain a separately recorded gate.

Local evidence from 2026-09-17 covers the LeRobot reader round trip, the scoped
client transport, and real PostgreSQL concurrency, accounting, replay, and stale
attempt fencing, including current owner disable/app restrictions on delegated
HTTP calls. The [local CPU build receipt](activation/local-build-20260917.json)
records the non-root image's offline fixture encode/normalize/decode check. Its
local image ID is not a publishable registry manifest digest. This evidence does
not qualify a deployed GPU workflow or the Apps UI.

The historical CPU image was published as
`cr.eu-north1.nebius.cloud/e00akg9ndpx77eaexh/fs2-platform/fs2-lerobot-augmentation@sha256:a725b52d6b53ff377d05bd87e748d1722399140b08a10dde49918a029734cfc5`.
The [registry publication receipt](activation/registry-publication-20260917.json)
records the independent manifest/config check. It has been superseded by
the [integrity-corrected worker publication](activation/registry-publication-integrity-20260917.json)
and then the [actual-protocol correction](activation/registry-publication-protocol-20260918.json).
The [active onboarding identity](activation/active-onboarding-20260918.json)
retains the pre-qualification state. The canonical execution map binds worker
`sha256:df364675b50cd267cb80dab38ad29fa3ec2f5d704e82e30e29ec104cea511042`;
the profile's public and scheduler proofs reference the actual release148 run.
The initial release147 parser failure remains retained and is not counted as a
pass. Hugging Face/object-store sources and maximum-size requests have not been
qualified by this uploaded-bundle test.

Live direct H100 preview tests on 2026-09-17 produced distinct lighting V2V and
environment transfer MP4s, then rewrote, packaged, extracted and fully decoded
two 16-frame LeRobot datasets with unchanged action/state/timestamp data. These
are mechanics checks only: the gradient fixture cannot establish preserved
robot-scene semantics, and direct preview calls bypass public parent admission.
A realistic public model-generated robot fixture exposed the Cosmos runtime's
default 64 MiB shared-memory limit; the serving recipe now provides a bounded
2 GiB pod-local `/dev/shm` for independent snapshot/media verification. No
LeRobot semantic or customer-ready claim follows from these preliminary runs.

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
