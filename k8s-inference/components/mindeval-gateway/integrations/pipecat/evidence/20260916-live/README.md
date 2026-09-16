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

The voice worker's coordinated 17:18 UTC provenance records Service/Deployment
`magpie-tts-multilingual-357m`, Pod
`magpie-tts-multilingual-357m-9c9b4685d-hcnj2`, node
`computeinstance-e00zs7gf1mgdygk6jv`, cluster
`mk8scluster-e00j5z9te7x5dd9g6a` in `eu-north1`, regular/on-demand L40S 48GB,
pool `l40s-1x` (not preemptible). Immutable image:
`cr.eu-north1.nebius.cloud/e00akg9ndpx77eaexh/fs2-models/voice-agent/magpie@sha256:fc9db5fd5df87e819766297d90a4595664aec63e7dbfd6735b3f2053b4725ead`.
Checkpoint revision `19806879b16d3f2ccf28fb112b1bcd16a3c7923e`, SHA-256
`ec675fa8c02b9c1d5382c5c2b5a6acec6492c1e8344866c07cf3892185d18953`.

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
