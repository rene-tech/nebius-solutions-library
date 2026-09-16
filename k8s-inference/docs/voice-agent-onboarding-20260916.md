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
the already-existing compatible L40S pool, with rolling updates, zero unavailable
replicas and one surge replica. Normal scale-in drains existing
sessions; abrupt preemption yields an explicit failure, never a silent replay.

`models/voice-agent/servicemonitor.yaml` is an independent declarative monitoring
resource. Apply it after Prometheus Operator is installed; it selects only these
three controller-managed Services, never temporary qualification aliases. It is
separate from each individual App bundle so three controllers do not compete for
one monitoring object. Infrastructure installs can consume the same file, e.g.:

```hcl
resource "kubernetes_manifest" "voice_agent_metrics" {
  manifest = yamldecode(file("${path.root}/../../models/voice-agent/servicemonitor.yaml"))
}
```

The example path is relative to `stages/workloads`; adapt it to the install root.
Runtime readiness/occupancy, request outcomes/durations, first output histograms
and accepted/generated audio seconds are `fs2_voice_*` metrics. Node GPU metrics
continue to come from the existing DCGM collector; no extra GPU exporter loads.

The three normal Apps are published and their canonical Services select only
controller-managed Pods. Temporary qualification Deployments
`fs2-voice-{magpie,parakeet,sortformer}-r20260916` are retained at zero replicas;
their three temporary Services were deleted. Canonical Services were preserved.
The one-time preview cutover used `promote_services.py`: verified exact matching
ModelDeployment UIDs, added controller ownership to these previously unowned
task-created aliases, then atomically switched selectors after managed Pods
became Ready. Service UIDs and ClusterIPs did not change. A fresh install lets
the controller create canonical Services and needs no alias transfer.
No neighboring workload or cloud limit was changed.

Admin fixed-replica transitions exposed an existing KEDA ownership conflict.
The controller now transfers only `spec.replicas`, after exact owner, lease,
UID/resource-version and absent-autoscaler checks. A separate temporary SSA
manager claims that single field; the normal full apply remains non-forcing,
then the temporary manager relinquishes ownership. Interrupted cleanup retries
on reconciliation even when desired content already matches. No managedFields
editing or broad template ownership override is used. A real disposable paused
Deployment verified forward and reverse ownership with zero Pods/GPUs and was
deleted; see `replica-handoff-results.json` and regression tests.

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

## Measured acceptance

Initial full control-plane suite: 2031 passed, 102 skipped. Additional native/input
catalog tests: 84 passed; registration/legacy profile tests: 15 passed;
deployment storage/model coverage: 2 passed; resident runtime lifecycle: 13 passed.

| Resident L40S worker | First output, three warm runs | End-to-end RTF, three runs | Sampled peak GPU memory |
| --- | --- | --- | --- |
| Magpie, Sofia | 1.19 / 1.23 / 1.24 s | 0.737 / 0.746 / 0.734 | 4411 MiB |
| Parakeet | 0.57 / 0.66 / 0.66 s | 0.657 / 0.677 / 0.678 | 1681 MiB |
| Sortformer | 0.50 / 0.58 / 0.68 s | 0.363 / 0.379 / 0.384 | 1711 MiB |

RTF includes connection, delivery and session cleanup; streaming ASR first
output is measured from request start, not just model kernel time. Paced input
naturally has wall/audio ratio above one and is recorded separately. Parakeet
produced an actual `turn.eou` model token in clean and noisy streams; no EOB token
was observed in this fixture, and the service does not fabricate one.

All 12 documented Magpie languages produced finite nonempty speech using their
requested tokenizer; all five actual named voices produced distinct WAV audio.
`quality-results.json` records the multilingual cohort and three alternating
Sofia/Jason/Sofia utterances: normalized English WER 0.0 clean and 0.0222 with
12 dB room noise plus echoes. Sortformer produced anonymous labels 0 and 1 with
zero best-permutation error on 128 central speech frames for each case. This
excludes 0.5 seconds at turn boundaries and is explicitly **not official DER**.

Real overload returned 429. Disconnecting long Magpie synthesis released the
worker in 2.04 s; the next request produced a complete WAV. Draining Parakeet
rejected a new stream while the admitted stream completed with the full text;
readiness then returned 503. A fresh cached-image Parakeet Pod became Ready in
36 s from creation, 30 s from container start. Production preStop drains and
waits up to 1850 s for the active session, within its 1900 s termination budget.

`public-managed-results.json` records the published ordinary customer path:
Magpie first PCM in 2.93 s and exact stream-to-durable-WAV equality; Parakeet
partial in 0.36 s after session readiness, actual EOU, complete retained result
and 0.43 s finalization; Sortformer partial in 0.62 s after readiness and 0.58 s
finalization. Both pre-existing Nemotron Apps still return HTTP 200 with complete
synthetic English/German transcripts. The short-lived test key was revoked.
These are measured request examples, not guaranteed latency percentiles.

`managed-baseline.json` captures actual managed images, Pod/node identities,
Service owners/selectors, endpoints and Prometheus samples. Initial managed
Pod creation-to-Ready was 185 s Parakeet, 190 s Sortformer and 221 s Magpie on
existing regular L40S nodes with uncached weight layers. This is distinct from
cached restart measurements and is not new-node provisioning time.
The independent public Pipecat reference also completed Jason/Sofia synthesis,
PCM frame delivery, complete WAV preservation, Nemotron transcription and
RTVI events; its own evidence belongs to the gateway acceptance directory.

Raw GPU samples and exact Pod/node/image/startup provenance are retained in
`runtime-provenance.json` and three CSV files. GPU means include idle periods and
must not be presented as sustained throughput utilization.
