# Reproduce optional automatic Finish

Use a separate Playwright CLI session and a dedicated ordinary workshop team key.
Never print the key or collect sent WebSocket frames: the first frame authenticates.
Do not create runs while the rehearsal submitter is measuring run-count idempotency.

Configure Chromium with `--use-fake-device-for-media-stream`,
`--use-fake-ui-for-media-stream`, and
`--use-file-for-fake-audio-capture=/absolute/path/to/synthetic-speech-then-silence.wav`.
Grant the context `microphone` permission. The qualified fixture is mono PCM16,
16 kHz, 5.461375 seconds, with SHA-256
`5fc2ec9dddc07fc857990c2746adbf9bc4ce3529464d73616ebef5ffaec0de49`.
It contains synthetic English speech plus three seconds of silence, no customer audio.

After the exact image has two updated/ready replicas, open `/workshop` in the
isolated session, snapshot the form and sign in without exposing the token in CLI
output. The CLI echoes run-code source; any login code containing a key must have
all stdout/stderr captured into protected mode-0600 logs, never repository files.
Use a freshly loaded page for each execution, so the opt-in checkbox starts at
its HTML default instead of retaining a previous capture's user selection.

```bash
bash /home/tux/.codex/skills/playwright/scripts/playwright_cli.sh \
  -s=mindeval-auto-finish-mindguard run-code \
  --filename /absolute/path/to/tests/browser-auto-finish.js
```

The test creates exactly one spoken run, pauses it, selects its current role,
waits for takeover, opts into automatic Finish and captures the synthetic device.
It never clicks Finish recording. It records only received policy/model boundary
events, verifies the retained human text and authenticated WAV, and aborts in
cleanup. Manual Finish stays the unchecked default. The test records the original
browser HTTP status/type/length, independently checks RIFF/WAVE bytes with an
authenticated APIResponse, and requires the actual audio element to decode with
positive duration and no media error. Authorization remains in memory and is
never emitted in evidence.

Evidence from r8 is under `output/playwright/auto-finish/`. The initial receipt is
preserved with its test-decoding failure; `verification.json` records the separate
successful RIFF/WAVE/browser-decode verification of that same artifact. The fixed
whole script was not rerun to avoid creating an extra run during the cohort window.

Final unchanged-release r9 qualification is in `r9-final-result.json` and
`r9-verification.json`, with screenshot `auto-finish-r9.png`. The complete updated
script passed at 18:09:54 UTC on image digest
`377451e2a4296b7713aec33fb4ad6e2fd9bc49dec6291866bc4b6ca7e5a6261c`.
It exercised the real Silero silence fallback, retained a 65,580-byte WAV, decoded
2.048 seconds in Chromium, and aborted the intervened run. The earlier r9 capture
exercised genuine Parakeet EOU; its failed binary test assertion and independent
successful replay/decode verification remain in `r9-initial-*.json`. No runtime
change or platform replay failure was demonstrated. The original BrowserResponse
binary mismatch was not conclusively explained, so the test now verifies the
durable bytes and browser decoding separately rather than treating that assertion
as a platform failure.

Chromium's synthetic microphone can begin partway through its repeating fixture;
the final short transcript (`at n`) is not a word-accuracy qualification. Neither
capture exercised EOB/backchannel candidates or automatic backchannel speech.
These are intervened spoken experiences, not canonical consultations or clinician
quality evidence.
