# r10 spoken pacing and latency qualification

Run `0783b3d4-45d7-4e10-9260-9d0a83dea83e` completed on workshop image
`987feef5dc66883351dfae377878f02e233dca5af96004a7453662f0ab6c3392`.
The owner browser recorded all four turns, six retained WAV segments, five judge
scores, real PCM before turn completion and successful pause/barge-in/resume.
It recorded zero explicit `playback.gap`/`playback.error` events, 5,116 scheduled
audio buffers and two barge-in stops. The report's `seamless_live_passed` flag
means this explicit-gap gate passed, **not** a guarantee of uninterrupted sound
or real-time turn completion. The run is spoken/intervened, not a canonical
clinician-quality comparison.

## Observed timing

These first-audio and round-trip values cover the speech stage; preceding text
generation is separate. Pacing waits overlap production/delivery and must not be
added to TTS operation wall time.

| Turn | Retained audio | First PCM | Speech round trip | Chunk pacing wait |
| --- | ---: | ---: | ---: | ---: |
| Clinician 1 | 11.01 s | 2.25 s | 15.65 s | 8.85 s |
| Patient 1 | 53.55 s | 2.32 s | 62.33 s | 45.64 s |
| Clinician 2 | 103.75 s | 3.10 s | 260.63 s | 89.44 s |
| Patient 2 | 69.01 s | 2.52 s | 82.46 s | 60.15 s |

Maximum measured producer lead was **0.499979 seconds**, below the configured
0.5-second cap, across all four turns. Six successful TTS operations produced
237.308 seconds of audio. Their summed accepted-to-started admission time was
2.421 seconds; summed started-to-completed operation time was 160.934 seconds.
These ordinary owner API receipts are preserved in
[spoken-pacing-r10-timings.json](spoken-pacing-r10-timings.json).

For context only, the earlier r9 run produced 216.642 seconds of retained audio
in 151.857 seconds of summed TTS operation time, but suffered a real live gap.
[The r9 baseline](spoken-pacing-r9-baseline.json) uses different generated text
and concurrent load: it is not a controlled throughput comparison. The observed
operation/audio ratios are approximately 0.678 for r10 and 0.701 for r9. There
is no basis here for claiming either a causal speedup or a measured GPU-occupancy
penalty from pacing. Backpressure can keep an upstream request open longer, but
transport buffers also let an operation finish before browser playback completes.

## Why the second clinician turn took 260.63 seconds

Its second TTS operation, `34404947-bbed-4b27-979e-e709786e108b`, started at
18:35:16.890 UTC and completed at 18:35:46.099: 29.209 seconds of operation time
for 44.536 seconds of audio. The segment was not retained until 18:38:27.349.
Control-plane access logs identified the intervening asynchronous ASR operation
`23e7345c-f777-4e02-beb5-6e3a590ff8f4`; an ordinary owner GET confirmed:

- Accepted 18:36:05.212; activation began 18:36:05.238.
- Ready 18:38:23.700; execution began 18:38:23.704.
- Completed 18:38:26.501; result fetched 18:38:27.341.
- Accepted-to-ready 138.488 seconds; actual execution 2.797 seconds, with model
  processing reported as 2.353 seconds. Successful attempt was 2 of at most 3.

Thus the major delay was **ASR activation/readiness/retry waiting**, not TTS
generation or the PCM lead cap. The receipt's `cold_start_seconds` is an
accepted-to-ready duration and does not prove 138 seconds of model loading.
The readiness/failed-attempt diagnosis is a separate release-owner/voice-worker
investigation. The successful operation identifies the fixed hot Pod UID
`c7d2f95b-af74-432d-ab93-8bf2936d1c1a`. The receipt is preserved in
[spoken-pacing-r10-delayed-asr.json](spoken-pacing-r10-delayed-asr.json).

The observed wait is not a promised latency bound: this operation's deadline was
20:36:05 UTC, roughly two hours after acceptance; the workshop separately limits
async ASR polling to 600 seconds. Neither establishes a 138-second cold-start SLA.

## Usage and acceptance limits

The operation API reports fixed `estimated_gpu_seconds` values of 10,800 for each
TTS request and 7,200 for this ASR request. They are accounting estimates, **not
measured physical GPU utilization**, and are not used as occupancy measurements
here. Audio modality seconds and operation timestamps are reported separately.
Initial deliberately interrupted synthesis is excluded from retained-segment
totals. All reads used the existing ordinary team key or scoped read-only logs;
no runtime changes, extra inference, quotas or infrastructure changes were made.

The separate unchanged r10 automatic-microphone browser test also passed:
run `22a81a02-4b15-4a49-b1d7-00b80e7aea52`, genuine Silero silence fallback,
zero Finish clicks, retained/decoded WAV, then abort and isolated-browser close.
Its synthetic fixture does not establish microphone accuracy, clinical safety,
or readiness under arbitrary speech concurrency.
