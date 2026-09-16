---
name: nemotron-speech
description: Transcribe uploaded recordings with Scientific AI Nemotron Speech through typed MCP tools and durable artifact jobs. Use for English or multilingual speech-to-text, long audio, consultation transcription, and preparing a real-time streaming client. Not for speech generation, translation, clinical interpretation, or claiming speaker diarization.
---

# Nemotron Speech

## Discover before invoking

Use the connected Scientific AI catalog and typed-tool discovery. The intended
Apps are `nemotron-speech-en-0.6b` (English) and
`nemotron-speech-multilingual-0.6b` (multilingual). A repository declaration or
this skill is not proof of a live route. If an App is absent or not granted to
the current API key, report that fact; do not substitute an unrelated model.

Read the selected tool's current schema and model description. Public App names
may differ from the underlying model ID. Select the App in the invocation and
use the schema's constant model identifier in its `options` object.

## Uploaded recordings

1. Obtain the actual audio file via the client attachment/upload workflow. A
   local path is not readable by the remote server. Do not invent a URL, bucket
   key, artifact ID or transcript.
2. Use the existing model input upload/finalize flow. Transfer audio bytes
   outside the language-model tool arguments. Large audio must never be base64
   encoded or expanded into a JSON tool argument.
3. Keep the finalized artifact reference unchanged: `artifact_id`, `sha256`,
   exact `size_bytes`, `media_type`, and `compression: none`. Pass it as `audio`.
   The server checks ownership with the current tenant/API key.
4. Set `options.language` explicitly when the recording language is known.
   English App accepts `en`/`en-US`. Use the multilingual App for German
   (`de`/`de-DE`) or automatic language selection (`auto`). Adaptation-required
   locales are not enabled simply because an upstream language list names them.
5. Use the typed native transcription tool with a stable idempotency key and
   an asynchronous wait/poll flow. Preserve the operation ID. A queued/202
   response is not a failure; poll its status and retrieve its completed result.
   Do not repeatedly resubmit with new keys while a model is cold or busy.
6. Present only a completed transcript as final. Keep model errors distinct
   from empty/silent audio. Preserve source, operation ID and any limitations.

The file adapter accepts supported compressed formats and converts them to
mono 16 kHz audio. Current implementation bounds are 512 MiB encoded and two
hours decoded for artifact jobs. Check the live schema for authoritative
limits. The small-file `/v1/audio/transcriptions` compatibility endpoint is
limited to 8 MiB; it can also return an asynchronous operation when queued.

## Live transcription is a client connection, not an MCP file call

Use the documented authenticated WebSocket `/v1/audio/stream` only after that
endpoint is deployed/qualified. Send a `session.start` control message with the
selected App and options, then wait for `session.ready`; `session.queued` is not
permission to buffer an entire microphone recording on the server.

The wire audio is binary signed little-endian PCM16, mono, 16 kHz. Send bounded
frames (20 ms / 640 bytes is a suitable client framing choice) and honor socket
backpressure. Finish with `{"type":"input.finish"}`. A dedicated client handles
this connection; an agent's ordinary MCP tool cannot serve as a microphone.

Replace partial text for the same segment ID/revision. Append/seal each final
segment once. Never concatenate every partial or promote an unfinished partial
to a final transcript. Wait for `session.completed` and retain its operation
and result identifiers. Cancellation uses `{"type":"session.cancel"}`.
Disconnect/preemption does not imply seamless resume: retain source audio on
the client and deliberately submit retained audio under a new operation if
needed. Never hide replay or billable duplicate work from the user.

## Options and output boundaries

Chunk size and decoder settings belong to a loaded worker profile. A request
must match its selected App profile; do not silently drop a mismatch or change
an App's deployment to satisfy one request. Consult the advertised options for
chunk sizes, tag handling and word/segment alignment. Missing confidence is
unknown, not zero or a fabricated 100% confidence.

Neither model provides medical verification, clinical advice or speaker
identity through this integration. Language selection is not translation.
For consultation/report workflows, keep verbatim transcription separate from
subsequent summarization, explicitly preserve uncertainty, and never turn
teaching narration into patient findings. A fluent transcript may contain
errors in drug names, quantities, negations or diagnoses and needs review.

Never request or include platform credentials in tool arguments or transcripts.
Authentication comes from the configured MCP/HTTP client connection.
