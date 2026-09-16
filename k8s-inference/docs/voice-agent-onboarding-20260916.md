# Resident voice models — 2026-09-16

Three additive independent NeMo runtimes complement the existing Nemotron
English/multilingual speech Apps; neither existing speech runtime was replaced.
The native GPU contract is qualified on one regular L40S per resident worker.
Public authorization, metering and artifact retention use ordinary App grants
and operation fences, not a separate unauthenticated speech service.

## Provenance and license

The official [Voice Agent](https://github.com/NVIDIA-NeMo/Voice-Agent) reference
is pinned at `a01fa68f0907a52cca1b07115e0e37c68ca580e5`; NeMo Speech is pinned at
`3d91009e8f2ef690112eabeb6907da00ea308d7f`. Official model cards:

- [Parakeet EOU 120M](https://huggingface.co/nvidia/parakeet_realtime_eou_120m-v1): English cache-aware 80 ms chunks, model-produced EOU markers.
- [Magpie multilingual](https://huggingface.co/nvidia/magpie_tts_multilingual_357m): pinned current v2607 checkpoint; actual speaker IDs Aria 0, Jason 1, John 2, Leo 3, Sofia 4.
- [Streaming Sortformer](https://huggingface.co/nvidia/diar_streaming_sortformer_4spk-v2.1): optional four-speaker activity, anonymous labels rather than person identification.

All three and NanoCodec use the NVIDIA Open Model License. Revisions, checkpoint
SHA-256 and byte sizes are in `components/voice-runtime/src/fs2_voice/contracts.py`
and content-addressed catalog manifests. The ByT5 tokenizer is pinned; no ByT5
language-model weights are loaded. Magpie's unused NanoCodec training
discriminator is explicitly disabled before restore (upstream otherwise
downloads WavLM then immediately deletes it). Inference tensors are unchanged.

## Contract and deployment

`components/voice-runtime/README.md` defines typed streaming inputs and events.
Magpie is honestly **phrase-incremental**, not codec-token streaming: each bounded
phrase uses official `do_tts`, then PCM chunks are sent before later phrases.
The public `POST /v1/voice/synthesize` retains the complete WAV as an operation
artifact before `audio.done`. `GET /v1/operations/{id}/result` is the durable
result. Parakeet/Sortformer use `/v1/voice/stream`; native `/generate` supports
complete authorized audio artifacts. Diarization runs only in its own selected
App and is not loaded into Magpie, Parakeet or bot-only workshop workers.

`models/voice-agent/render.py` accepts digest-pinned images and caller-selected
GPU/region placement. Checked-in zero-replica templates are controller inputs;
they must not be applied as a separate unmanaged production fleet.
`acceptance/voice-agent-20260916/prepare_registration.py` reads the current live
envelope, renderer bundles and route ConfigMaps and prepares additive immutable
ConfigMaps, Helm values and three App proposals. It preserves existing pools,
limits and models, refuses replacement, and validates all proposals. The manager
owns the combined control-plane/Helm rollout. Prepared Apps use min 1/max 2 on
the already-existing compatible L40S pool. Normal scale-in drains existing
sessions; abrupt preemption yields an explicit failure, never a silent replay.

Temporary qualification deployments are `fs2-voice-{magpie,parakeet,sortformer}-r20260916`.
Canonical service aliases currently point at these task-owned workers. After
managed Apps become ready, the controller owns canonical service selectors;
preview Deployments must be scaled to zero and their temporary service aliases
removed only after the manager confirms a healthy managed cohort. Do not remove
the canonical Services. No other workload or cloud limit was changed.

## Evidence and limits

`acceptance/voice-agent-20260916/native-results.json` records two distinct real
native GPU requests/results per model. Only synthetic text/audio was used and
both temporary storage objects were deleted. `probe.py` regenerates five voices,
three same-profile warm iterations, German output, deterministic 15 dB noisy
headset input, paced live input, overload, cancel, reset and reconnect checks.
Hardware: NVIDIA L40S 48GB, driver 580.173.02, PyTorch 2.8.0+cu128, SM89.

The regional image and verified weight layers are retained with `IfNotPresent`.
Measured first Parakeet image pull was 140.915 s; cached Magpie layer refresh was
0.270 s. Initial restore/warmup: Parakeet 10.80/24.89 s; Sortformer 7.83/22.87 s;
Magpie 34.29/1.95 s, excluding interpreter/import and checkpoint verification.
These are not new-node boot times. The image has neither CRIU nor
`cuda-checkpoint`; no voice snapshot capture/restore or restored public cohort
has been qualified. Snapshot flags remain disabled.

Synthetic English outputs are not medical accuracy or clinical validation.
Speaker labels may change across new sessions and are not persistent identities.
H100/Blackwell compatibility, new-node elastic cold starts and restored
snapshots are not inferred from the L40S measurements.
