# Real public Pipecat / RTVI round — 2026-09-16

Result: **passed**, 35.084626 seconds, no pipeline failures. This is a live
in-process Pipecat pipeline and RTVI protocol test, not a browser/WebRTC claim.

Executed `uv run --frozen python example.py` against `https://89.169.99.188`,
profile `profile-000`, one canonical round, run ID
`fs2-pipecat-live-20260916`. The protected ordinary team-01 platform PAT was read
from its handoff file; no administrator or Token Factory key was used. The
existing self-signed rehearsal certificate was accepted explicitly. All inference
used public platform endpoints; no service, image, model or GPU was deployed by
this client.

- Pipecat 1.10.0, upstream `f67c18afddbfb0609991cd6830355713baaad01b`, frozen
  Python dependency lockfile; RTVI protocol 2.1.0.
- Qwen235B clinician and Qwen30B patient, original server-pinned profile/prompts;
  two real completions, two Magpie syntheses, two Nemotron English STT calls,
  one fixed Gemma judgment with five finite named scores.
- Actual RTVI `bot-ready`, LLM text/start/stop, TTS start/stop, transcription,
  metrics and application `server-message` events are retained in `report.json`.
- Voice worker confirmed managed public Magpie ready and held it unchanged for
  this baseline. Its subsequent scale/drain tests began only after completion.
  Parent coordinator owns deployment-image and GPU provenance.

| Role / voice | WAV duration | First audio | Synthesis stream | STT | Normalized WER |
| --- | ---: | ---: | ---: | ---: | ---: |
| Clinician / Jason | 1.997s | 2.499s | 2.915s | 2.683s | 0.00% |
| Patient / Sofia | 25.170s | 1.592s | 18.189s | 4.011s | 4.55% |

Both files decode as mono PCM16, 22,050Hz, with nonzero amplitude. Sequence and
sample/chunk counts match each `audio.done`. Exact sizes, sample counts, peak/RMS,
SHA-256 hashes, generated text, recognized text and operation IDs are in the
report. Sofia's recognition errors remain visible; no transcript was repaired.
WER is a case/punctuation-normalized word-edit measure, not semantic accuracy or
clinical safety. Audio listening/perceptual quality is a separate review; this
test establishes actual framework wiring and measurable speech round-trip output.

TTS operations:

- Jason: `dda40ab4-cd04-4898-b29e-7e53d78693ac`
- Sofia: `acd6b2e7-1e16-4bb1-96aa-9946aa7e9438`

The patient audio's 25-second duration is independent of its 18-second synthesis
latency. The CLI records output; it does not pretend to have paced speaker playback
or tested microphone interruption. The separate workshop UI owns those behaviors.
