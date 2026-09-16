# Optional microphone voice policy

Manual **Finish recording** remains the default. The unchecked **Optional automatic
Finish** control is available only for English spoken-experience runs, never the
canonical benchmark. It selects the existing public Parakeet model and completes
microphone input on a genuine model EOU or Silero silence after detected speech.
A pause can end a turn early; keep manual mode for uncertain conditions. There is
no automatic backchannel speech, no text suppression and no invented clinician
turn. Explicit takeover and normal owner/model-grant checks still apply.

The workshop is the CPU voice gateway for this feature. Silero and the policy are
not GPU Apps. One process-local ONNX session is loaded before application startup
completes; recurrent state and 64-sample context are separate for every microphone.
CPU inference is serialized off the event loop, using one ONNX inter/intra-op thread.
Input remains bounded to two minutes. No per-connection weight downloads, Torch,
CUDA, Pipecat runtime or new infrastructure are added.

## Frozen provenance

- [Silero v6.2](https://github.com/snakers4/silero-vad/tree/be95df9152c0d7618fa1edfeb296fc3dae32376f),
  revision `be95df9152c0d7618fa1edfeb296fc3dae32376f`, MIT. File
  `src/silero_vad/data/silero_vad.onnx`, SHA-256
  `1a153a22f4509e292a94e67d6f9b85e8deb25b4988682b7e174c65279d8788e3`.
  Docker downloads at build time with checksum verification. Runtime verifies the
  checksum again, uses only `CPUExecutionProvider`, and follows the pinned
  `utils_vad.OnnxWrapper` 16 kHz / 512-sample / 64-context / `(2,1,128)` state API.
- `onnxruntime==1.30.0`, `numpy==2.3.2`; dependencies and wheel hashes in `uv.lock`.
  Existing Python base image stays digest pinned.
- [NeMo Voice-Agent](https://github.com/NVIDIA-NeMo/Voice-Agent/tree/a01fa68f0907a52cca1b07115e0e37c68ca580e5),
  revision `a01fa68f0907a52cca1b07115e0e37c68ca580e5`, Apache-2.0. The narrow English
  `clean_text`/`is_backchannel` logic and pinned example phrase list are adapted in
  `voice_policy.py`. This is **not execution of the full NeMoTurnTakingService**:
  its Pipecat frame ownership, diarization and bot-speaking suppression are outside
  this microphone-takeover path. Notices ship with the package/image.

The [official NeMo description](https://docs.nvidia.com/nemo/labs-voice-agent/about/core-concepts/speech-pipeline/turn-taking-backchannels/)
distinguishes VAD activity from model EOU/EOB. We use its 0.6 start threshold,
at least 100 ms speech and 1.2 seconds silence fallback, rounded up to 512-sample
windows (128 ms / 1216 ms); Silero's exit hysteresis is 0.45. The volume gate from
Pipecat is not included. These are disclosed workshop defaults, not tuned clinical
or noisy-room quality guarantees.

## Protocol and retained evidence

Browser microphone first message adds `auto_finish: true` and
`model: "parakeet-realtime-eou-120m-v1"`. Without opt-in, existing Nemotron manual
behavior is unchanged. The existing configured speech URL supplies the host; only
`/v1/audio/stream` changes to `/v1/voice/stream` for Parakeet. Parakeet uses top-level
`model`, `session.finish`, `session.done`; Nemotron uses `options.model`,
`input.finish`, `session.completed`. Browser controls stay `session.finish/cancel`.
The proxy serializes finish controls so manual/automatic races send exactly one.

Structured observations:

- `voice_policy.speech_start`: source `silero_vad`.
- `voice_policy.speech_end`: `silero_vad_silence` with `model_eou:false`, or
  `parakeet_model_eou` with `model_eou:true`.
- `voice_policy.backchannel_candidate`: `nemo_phrase_rule` with `model_eob:false`,
  or `parakeet_model_eob` only for a real authenticated `turn.eob` event with
  `source:model_token`. These never discard text or request speech.
- `voice_policy.input_finish`: tells the browser to stop capture without sending
  a duplicate Finish. A completed ASR session and nonempty final text are still
  required before the human turn can be committed.

Observations, model/code pins and CPU provider are retained in the human WAV's
metadata atomically with its turn, under `voice_policy`. Unfinished/cancelled
captures do not become human turns. The existing run version fence is unchanged.
If the optional model is absent, auto mode returns an explicit manual-Finish
fallback error; a wrong checkpoint fails process startup. Default path is
`/opt/voice-policy/silero_vad.onnx`; local tests can set `WORKSHOP_SILERO_MODEL_PATH`.

## Reproduce local qualification

```bash
WORKSHOP_TEST_DATABASE_URL=postgresql://postgres@127.0.0.1:15496/workshop_playback_20260916 \
WORKSHOP_TEST_SILERO_MODEL=/path/to/pinned/silero_vad.onnx uv run pytest -q
uv run python scripts/qualify_voice_policy.py --model /path/to/pinned/silero_vad.onnx \
  --wav /path/to/synthetic-16khz-mono-pcm16.wav
```

The qualifier appends two seconds of disclosed zero silence and reports checkpoint
and waveform hashes, CPU timing and boundary events. Proxy tests use real local
WebSockets and PostgreSQL but synthetic speech events; they do not establish live
Parakeet quality, physical microphone accuracy or a complete production rehearsal.
