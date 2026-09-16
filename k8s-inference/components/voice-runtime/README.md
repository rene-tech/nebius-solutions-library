# Scientific AI resident voice models

Independent NeMo checkpoint runtime. Each process loads exactly one selected
model once; Parakeet, Magpie, and optional Sortformer have separate model IDs,
admission, metrics and deployment lifecycles. Existing Nemotron Apps are reused.
No LLM is loaded. Sortformer is absent from bot-only sessions and can scale to
zero without changing the other services.

The strict contracts and immutable model pins are in
`src/fs2_voice/contracts.py`. GPU/public qualification is recorded separately
under `acceptance/voice-agent-20260916`; source implementation is not proof of
production readiness.

## Native contracts

`GET /healthz`, `GET /readyz`, `GET /metrics`,
`GET /v1/voice/capabilities`. Internal `POST /drain` removes readiness and rejects
new sessions while an established session finishes. Deployments must drain
before SIGTERM and allow the configured session/grace budget.

`POST /v1/voice/synthesize`, `application/json`:

```json
{"model":"magpie-tts-multilingual-357m","text":"Hello. How can I help?","language":"en","voice":"Sofia","apply_text_normalization":false}
```

Response is `application/x-ndjson`: `audio.start`, ordered `audio.chunk` events
with `sequence`, `sample_rate_hz:22050` and `audio_base64` (mono PCM16 little
endian), then `audio.done` with exact samples, duration and processing seconds.
An `error` event is terminal; a disconnected/incomplete stream is not a completed
artifact. Concatenate PCM chunks and wrap a WAV header after `audio.done` to
persist a playable artifact. First audio means first `audio.chunk`, not headers.
Five v2607 voices: Aria, Jason, John, Leo, Sofia. Languages: ar, de, en, es, fr,
hi, it, ja, ko, pt, vi, zh. Recommended distinct demo voices: Sofia and Jason.
The current checkpoint speaker order differs from the older Voice-Agent example.

Synthesis is explicitly **phrase incremental**, calling the official generator
on bounded phrases and delivering each as it completes. It does not claim
codec-token or within-phrase streaming. A request is bounded to 4096 characters;
each inference phrase to 100 characters. Output buffering is two 100ms chunks.
Cancellation stops output and future phrases; an executing GPU call remains
occupied until it finishes. No hidden CPU fallback or model reload per request.

`WS /v1/voice/stream`: first JSON `session.start`:

```json
{"type":"session.start","model":"parakeet-realtime-eou-120m-v1","audio":{"encoding":"pcm_s16le","sample_rate_hz":16000,"channels":1}}
```

Wait for `session.ready`, then send binary PCM frames, at most 32000 bytes each.
Parakeet emits cumulative `transcript.partial` and `transcript.final` per integer
`segment`. Replace a segment's partial with the next partial/final; do not append
the cumulative text. `turn.eou`/`turn.eob` are emitted only for actual model
tokens and include `source:model_token`. The card documents EOU; EOB is optional,
never synthesized from silence. VAD/backchannel policy belongs in the gateway.

Sortformer uses the same socket with model
`diar-streaming-sortformer-4spk-v2-1`, emits `speaker.activity` with a start time,
80ms frame duration and a [frames,4] probability matrix. Labels reflect arrival
order, not named identities. The model does not provide clinical attribution.

Controls: `session.finish` flushes final audio and sends `session.done`;
`session.cancel` stops and closes; `session.reset` discards all state and keeps
the admitted socket. An interrupted socket has no resume state: reconnect is a
new session. Do not replay already accepted audio automatically. Worker loss or
preemption must become an explicit terminal `worker_lost` at the public relay.

One active request/session per replica. Busy is HTTP429 or WS `worker_busy`;
drain is HTTP503/`worker_draining`. Input buffering is bounded by WebSocket limits
and a single model frame. Session limit is 30 minutes, idle timeout 30 seconds.
Scale out with new replicas; a socket remains pinned to its admitted replica.

## Build and deploy

`Dockerfile` pins NeMo Speech, Voice-Agent helper source and the base image.
`Dockerfile.model` adds a single SHA-verified checkpoint plus Magpie's pinned
codec. Final deployment manifests accept only image digests, with GPU placement
and namespace supplied by the environment. Runtime model weights are baked into
regional images. Snapshots remain disabled until real restored public cohorts
pass. See `models/voice-agent` for portable deployment rendering and catalog data.
