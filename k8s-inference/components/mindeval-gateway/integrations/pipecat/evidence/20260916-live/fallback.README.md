# PRERECORDED offline event fallback

Open `fallback.html` directly after extracting **all** bundle files into one
directory. No server, network, Python runtime, API key, or microphone is needed
to play the recording. Click **Play prerecorded sequence** or the two individual
audio controls. If a browser blocks local media, use the WAV download links or
open the WAV files in a desktop audio player. Always announce: “This is a
prerecorded synthetic demonstration, not a live run.”

Playlist order:

1. `001-clinician-Jason.wav` — clinician / Magpie Jason, 1.997 seconds.
2. `002-patient-Sofia.wav` — patient / Magpie Sofia, 25.170 seconds.

The patient seed `Hello` is in the canonical transcript but was not synthesized.
Recorded 2026-09-16 17:18:23–17:18:58 UTC; source report file finalized at
`2026-09-16T17:18:58.676072487Z`. Run `fs2-pipecat-live-20260916`, synthetic MindEval
`profile-000` / Dennis, age 47. No real patient or attendee recording is included.

Exact models: patient `Qwen/Qwen3-30B-A3B-Instruct-2507`, clinician
`Qwen/Qwen3-235B-A22B-Instruct-2507`, fixed judge `google/gemma-3-27b-it`, speech
`magpie-tts-multilingual-357m`, recognition `nemotron-speech-en-0-6b`. Pipecat 1.10.0
and RTVI 2.1.0 were exercised in-process during recording. `report.json` retains
the exact original profile/prompts, all generated text, unedited ASR output,
five scores, timestamps/operation IDs where exposed, metrics and WAV hashes.
`README.md` records image/checkpoint/GPU provenance and the live methodology.

## Reproducible bundle

From this directory, with Python 3.13 (standard library only):

```sh
python3 fallback.py --output /tmp/scientific-ai-prerecorded-fallback.zip
unzip -t /tmp/scientific-ai-prerecorded-fallback.zip
```

The command validates the recorded WAV hashes, refuses to overwrite an existing
archive, includes a SHA-256 manifest, and writes sorted entries with fixed
timestamps/permissions and uncompressed storage. Identical source files yield
identical ZIP bytes. The archive contains no credentials or hidden working files.
To verify reproducibility, build another new path and compare `sha256sum` values.
Extract the entire ZIP, then open `fallback.html`; do not open it while it is
still inside an archive browser that cannot resolve sibling audio files.

## Playback and bundle checks

Validated on 2026-09-16 at 17:30 UTC using Chromium through Playwright:

- Opened the local `file://` HTML with the browser context configured offline.
  Every captured request was a local HTML or WAV file; there were no remote
  requests or browser console errors. No server was started.
- Both WAVs decoded with `readyState=4`: Jason 1.996916 seconds and Sofia
  25.170431 seconds. The sequence button played Jason through completion and
  automatically started Sofia. Stop paused and reset both clips to time zero.
- Two independently built ZIPs had identical SHA-256 values. `unzip -t` checked
  every entry, and the builder verified each WAV against the original report.
- Ruff formatting and lint passed for the bundle builder.

The automated checks confirm decoded playback progression, not a human
listening assessment. Browser offline emulation was configured explicitly;
Chromium still exposed `navigator.onLine=true` on the local-file page, so that
property is not used as evidence of network isolation.

## Limitations — keep visible when presenting

- This is one short recorded round, not the full ten-round/six-clinician cohort
  or the ten-team load rehearsal. It does not prove current availability.
- Playback performs no live generation, judging, safety classification, microphone
  capture, intervention, turn-taking, streaming connection or browser RTVI call.
- Recorded scores are model judgments, not expert labels, model rankings,
  therapeutic advice, clinical validation or evidence of safety.
- Sofia's measured normalized word error rate was 4.55%; Jason's was 0%. Errors
  remain visible, including the patient's recognized time of awakening. Generated
  text is not silently replaced by corrected ASR text.
- Audio was recorded from the public managed service. It is not a recording of a
  real attendee conversation. The original ordinary PAT is neither required nor
  included; the fallback must never ask attendees to paste credentials.
