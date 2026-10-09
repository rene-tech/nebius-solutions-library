# Bounded public voice coverage — 18 September 2026

Scientist09 completed **11/11 core operations and 7/7 ASR readbacks**, sequentially
through ordinary public APIs. All core operations used one execution attempt.
No model image, settings, limits, quotas or production resources were changed.
This is scoped API/media evidence, not a broad customer-ready verdict.

## Magpie

Five native calls covered Sofia, Jason, Aria, John and Leo, using English/German
source utterances; two further calls used the advertised incremental endpoint.
All seven full WAVs decoded at22050Hz mono16-bit and passed artifact hashes,
non-silence and duration checks. Total generated audio146.193s. Near-clipping
sample fraction was0; near-zero sample fractions were13.2–30.5%, not a VAD
silence diagnosis. Operation start-to-completion RTF0.684–0.814 includes
transport/publication rather than isolated model computation.

| Stream | First audio | Retained audio | Longest inter-chunk gap |
| --- | ---: | ---: | ---: |
| English | 2.435s | 41.796s | 4.308s |
| German | 6.392s | 36.316s | 2.985s |

Both streamed PCM outputs matched the complete retained WAV byte-for-byte.
An idealized client starting playback immediately at the first received chunk
would have a maximum observed delivery deficit0.361s for English,0 for German.
This is phrase-incremental synthesis; no seamless unbuffered playback or browser
audio-device claim is made.

Nemotron multilingual readback preserved full duration for all seven outputs.
Round-trip WER was English native9.52%/6%/0%, German native16%/16%, English
stream3.88%, German stream3.41%; pooled21errors/403reference words=5.21%.
These are **dual-model lexical agreement proxies**, not independently verified
TTS word errors or clinical accuracy. No universal quality threshold was applied.

## Sortformer

| Source/mode | Audio | Model frames | DER |
| --- | ---: | ---: | ---: |
| PriMock consultation01 native | 457.92s | 5724 | 19.68% |
| PriMock consultation02 native | 559.20s | 6990 | 18.20% |
| consultation01 first60s native | 60s | 750 | 17.60% |
| Same crop, real-time public WebSocket | 60s | 750 | 17.60% |

All full-duration outputs had contiguous80ms timestamps and bounded four-speaker
probabilities. The streaming crop delivered first speaker activity at2.651s and
completed its operation in61.733s. Equal aggregate DER does **not** imply identical
native/stream probabilities. DER uses threshold0.5, one global speaker mapping,
zero collar and overlap included against original human doctor/patient
utterance-level TextGrid intervals. It is not paper benchmark reproduction or
an independent speech-activity gold standard. Speaker labels do not identify people.

## Identities, faults and limitations

- Magpie image `sha256:fc9db5fd5df87e819766297d90a4595664aec63e7dbfd6735b3f2053b4725ead`;
  Sortformer `sha256:ef54e92d54aa3f603c9aabdc129c3d3bfea017f8466c26fba87e8056e2e75253`.
  Read-only snapshot found one Ready replica each. Parent immutable checkpoint
  pins and public discovery were retained with the cases.
- Public core receipts contain Pod/node/GPU identities for9/11 operations.
  Both TTS streams have unavailable identity; those are **unknown, not zero GPU
  consumption**. The separate deployment snapshot does not backfill responses.
- Accepted-to-started intervals0.251–2.393s. No cold-start, preemption, high-load,
  browser or remaining-ten-language qualification is claimed.
- One local harness failed before streaming admission because the evaluation
  environment lacked websockets. The original receipt remains; using the existing
  CP environment completed the same case. Dependency preflight now runs before
  any admission.10 offline scorer/transport tests and Ruff pass.

Protected evidence root:
`/home/tux/secure-handoff/scientific-qualification-20260918/cohorts/voice-coverage-r1`;
ASR receipts under sibling `voice-asr-proxy-r1`.

- Frozen cases SHA256 `ad253a0a5cedb4b0a0ac45c5a891b937fe901683b1ca08022ce9e4637b6d80fe`.
- Measurements SHA256 `9d0c41b8ebf3f471f710a21d331a546f567bab3396b24d6f59bd2a3815ac2bd2`.
- Runtime snapshot SHA256 `c8829cb8eddc814a0b8c99ab114edd8200e7c2a5c1c1fec70b7677275fe4bf09`.

Source/reproduction instructions and primary dataset/model links are in README.md.
