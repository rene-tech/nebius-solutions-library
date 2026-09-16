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

```bash
bash /home/tux/.codex/skills/playwright/scripts/playwright_cli.sh \
  -s=mindeval-auto-finish-mindguard run-code \
  --filename /absolute/path/to/tests/browser-auto-finish.js
```

The test creates exactly one spoken run, pauses it, selects its current role,
waits for takeover, opts into automatic Finish and captures the synthetic device.
It never clicks Finish recording. It records only received policy/model boundary
events, verifies the retained human text and authenticated WAV, and aborts in
cleanup. Manual Finish stays the unchecked default. The WAV assertion accepts
the CLI sandbox's Uint8Array, not only a Node Buffer.

Evidence from r8 is under `output/playwright/auto-finish/`. The initial receipt is
preserved with its test-decoding failure; `verification.json` records the separate
successful RIFF/WAVE/browser-decode verification of that same artifact. The fixed
whole script was not rerun to avoid creating an extra run during the cohort window.
