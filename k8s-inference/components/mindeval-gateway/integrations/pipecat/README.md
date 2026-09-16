# Pinned Pipecat / RTVI reference client

This is an executable **Pipecat pipeline**, not a claim that the bespoke workshop
browser is a Pipecat client. It uses `pipecat-ai==1.10.0`, released upstream at
[`f67c18afddbfb0609991cd6830355713baaad01b`](https://github.com/pipecat-ai/pipecat/releases/tag/v1.10.0).
Python 3.13 and all resolved dependencies are pinned by `uv.lock`; no GPU or model
weights are loaded locally. The only inference destination is the configured
Scientific AI public origin. A normal platform PAT is used, never the Token
Factory credential or a new credential system.

## Implemented boundary

```
registered profile -> MindEval LLM frames -> Magpie PCM frames -> Nemotron STT -> recorder
                          |                      |                  |
                          +---------- RTVI messages + metrics -----+
```

- `MindEvalProcessor` consumes `ModelTurnFrame` and emits actual
  `LLMFullResponseStartFrame`, `LLMTextFrame`, `LLMFullResponseEndFrame`. It fetches
  the pinned server prompts, preserves upstream patient `Hello` initialization,
  alternates clinician/patient roles, and uses the registered server-side models.
- `MagpieProcessor` converts NDJSON `/v1/voice/synthesize` responses into actual
  `TTSStartedFrame`, `TTSAudioRawFrame`, `TTSStoppedFrame`, and `MetricsFrame`.
  Patient=Sofia, clinician=Jason. PCM is mono, signed 16-bit little-endian,
  22,050Hz. Sequence, format, lengths, final sample/chunk counts and `audio.done`
  are checked; partial/failed streams cannot become successful utterances.
- `NemotronSTTProcessor` accepts a complete mono PCM16 `InputAudioRawFrame`, or
  audits each generated utterance, through `/v1/audio/transcriptions`. A cold
  `202 Location` is polled without resubmission. It emits a real finalized
  `TranscriptionFrame`. ASR observations **do not replace canonical model text**.
- `PipelineWorker` / `WorkerRunner` execute the processors. The installed
  `RTVIProcessor` and `RTVIObserver` handle a genuine 2.1.0 `client-ready` /
  `bot-ready` protocol handshake and emit `bot-llm-text`, `metrics`, transcription,
  and application `server-message` events. These are captured as actual
  `OutputTransportMessageUrgentFrame` messages, not hand-built lookalikes.
- A final server-side MindEval judgment must contain all five valid named scores.
  Output includes canonical text, recognized speech, provider/token/queue
  telemetry, first-audio/STT times, RTVI messages, WAV artifacts and SHA-256 hashes.

The official version-pinned interfaces used here are
[`FrameProcessor`](https://github.com/pipecat-ai/pipecat/blob/f67c18afddbfb0609991cd6830355713baaad01b/src/pipecat/processors/frame_processor.py),
[`PipelineWorker`](https://github.com/pipecat-ai/pipecat/blob/f67c18afddbfb0609991cd6830355713baaad01b/src/pipecat/pipeline/worker.py),
[`WorkerRunner`](https://github.com/pipecat-ai/pipecat/blob/f67c18afddbfb0609991cd6830355713baaad01b/src/pipecat/workers/runner.py), and
[`RTVIProcessor`](https://github.com/pipecat-ai/pipecat/blob/f67c18afddbfb0609991cd6830355713baaad01b/src/pipecat/processors/frameworks/rtvi/processor.py).
The newer worker interfaces are intentional; older PipelineTask/PipelineRunner
examples are deprecated in this pinned release.

## Install and contract tests

From this directory:

```sh
uv sync --frozen
uv run --frozen pytest -q
uv run --frozen ruff check adapters.py example.py tests
```

Seven tests passed on 2026-09-16 using the actual installed framework. Network
responses are deterministic fixtures, **not live model-quality evidence**.
`tests/two_voices.json` contains distinct short PCM wire fixtures and separate
Sofia/Jason text expectations; those four-byte PCM payloads are intentionally
not intelligible synthesized speech. Tests cover real pipeline/RTVI execution,
both voice selections, successful and queued STT, rejected incomplete NDJSON,
invalid judgments, invalid PCM and rejected cross-origin polling.

## Real endpoint verification

Run after the coordinator confirms the public gateway, voice and STT endpoints
are ready. The PAT must grant `inference.invoke`, `mindeval`,
`magpie-tts-multilingual-357m`, and `nemotron-speech-en-0-6b`. Server policies still
apply. The file can contain one plaintext PAT or the protected rehearsal JSON
`{"teams":[{"token":"..."}, ...]}`; `--team` selects its entry.

```sh
uv run --frozen python example.py \
  --base-url https://89.169.99.188 \
  --token-file /protected/scientific-ai-platform-pat \
  --run-id pipecat-voice-verification-UNIQUE \
  --profile profile-000 --rounds 1 \
  --output output/live --insecure
```

`--insecure` is an explicit exception for the existing self-signed rehearsal
certificate. Use `--ca-file` or ordinary certificate validation otherwise.
One round makes two LLM calls, two Magpie requests, two STT operations and one
judge call. Read `output/live/report.json`; exit zero requires successful
pipeline/audio/STT/RTVI completion and valid scoring. Listen to both emitted
`*-clinician-Jason.wav` and `*-patient-Sofia.wav` files to review real audio.
No live endpoint success is claimed by the fixture tests above.

Use a unique run ID per invocation: registration is replay-safe, but repeating
this reference command **does make new model calls**. It does not implement the
workshop's durable job resumption, five-worker job scheduler, intervention UI or
encrypted-credential persistence. Those remain server responsibilities.

## Explicitly unsupported

- No browser, microphone, WebRTC/ICE/signaling server, Daily room or public RTVI
  WebSocket transport is deployed here. RTVI is genuinely exercised in-process;
  connecting the official browser SDK still requires a real Pipecat transport
  and authenticated session lifecycle. The custom `/v1/audio/stream` protocol is
  **not** an RTVI wire endpoint. No adapter pretends otherwise.
- No live Parakeet streaming adapter or VAD/barging-in is implemented. The input
  frame contract is one complete utterance; feeding individual microphone PCM
  packets as separate utterances is incorrect. Nemotron file transcription is
  the implemented STT path.
- NeMo Voice-Agent's application/orchestration is not launched by this client.
  The central voice runtime separately vendors reviewed upstream NeMo
  Voice-Agent helpers; this client calls its public endpoints and does not
  duplicate those checkpoints/services or claim their orchestration behavior.
- Endpoint limits remain visible: Magpie accepts at most 4,096 text characters;
  the small-file STT path accepts at most 8MiB WAV. This example does not silently
  truncate text, audio, or transcripts to fit. A long generated turn may fail
  speech conversion explicitly. The canonical judgment is suppressed on any
  pipeline failure.
- These synthetic mental-health conversations and model judgments are research
  workshop artifacts, not evidence of clinical safety or therapeutic efficacy.
