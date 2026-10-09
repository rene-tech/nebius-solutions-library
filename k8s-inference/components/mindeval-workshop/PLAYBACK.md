# Live spoken experience

The workshop's native browser playback uses Web Audio and an authenticated
WebSocket, not a Pipecat/RTVI adapter. It is a spoken-experience workflow, not
canonical text MindEval. Existing canonical prompts, turn routing and judging
are unchanged. Spoken or intervened runs remain excluded from default canonical
comparisons.

`/v1/workshop/runs/{run_id}/playback` accepts a first JSON message containing the
participant token. The server verifies the existing workshop grant and exact run
owner before subscribing. Tokens never enter database notifications. One pooled
PostgreSQL LISTEN connection per workshop replica fans out notifications to
authenticated sockets; workers publish through ordinary short pooled queries.
The DSN must be direct PostgreSQL or session pooling, not transaction PgBouncer.
The current `fs2-control-db-rw.fs2-data.svc.cluster.local:5432` service points to
the existing CNPG primary.

PCM16 mono chunks are packetized into 2 KiB pieces. Their versioned, sequenced JSON
envelopes remain below 4 KiB, under PostgreSQL's 8 KiB NOTIFY limit. The browser
schedules each arriving chunk immediately; it does not await a complete WAV or
ASR response. Playback needs a browser user gesture, exposed as Enable live audio.
Creating a spoken run attempts to enable it from that user gesture. A 64-event
per-listener queue has explicit gap signaling; missing segments stop that live
stream until the next utterance. Reconnect is explicit/automatic, and joining
mid-turn plays only newly arriving audio. Completed recordings remain available.

The producer now waits **before each notification** to stay at most 0.5 seconds
ahead of estimated playback. First audio is immediate; tiny subsequent provider
fragments are coalesced in the existing segment PCM buffer, and the last fragment
is flushed at segment completion. This is at most 47 full chunks in the lead
window even at 96 kHz, below the unchanged 64-event listener queue. No queue,
memory, GPU or infrastructure limits are increased. This reduces healthy burst
overflow risk; it cannot guarantee continuity through a slow/disconnected client
or a database-listener reconnect. Those failures still emit explicit gaps.

Per-chunk waits are cancellable and close the TTS HTTP response on cancellation.
Pacing intentionally applies backpressure to the upstream streaming reader:
the request can remain open roughly until audio duration minus the 0.5-second
lead, potentially increasing TTS model/lease occupancy depending on upstream
buffering. ASR still begins only after that bounded segment's synthesis response
has closed, and the remaining playback is drained after ASR/retention. Reports
separate chunk pacing time, total pacing time, configured lead and maximum
observed producer lead. Live GPU-occupancy/throughput cost requires deployed
measurement; the worker cannot prove when a remote model releases its lease.

Pause, Take over, Abort and microphone controls synchronously stop all local
scheduled and retained-recording playback before awaiting a network request.
Versioned control notifications stop other connected views. Spoken workers poll
their existing row/version/lease at 250 ms intervals and cancel superseded HTTP
work. This is best-effort request cancellation, not proof that a provider has
stopped computation or incurred no usage. Pauses/takeovers/aborts are durably
labeled as operator-requested barge-in; human-intervened runs remain labeled.
Resume also clears a pending takeover while the counterpart is running, fencing
that older in-flight result rather than leaving a future takeover trap.

## Long replies and retained evidence

Lossless sentence-first, then word-boundary segmentation uses at most 1,024
characters per synthesis request, below Magpie's 4,096-character limit. An
overlong word is split explicitly; no source text is dropped. Every segment uses
the same voice, mono PCM16 and unchanged sample rate. Each segment has its own
bounded ASR upload and durable WAV; recognized text is joined in order into one
conversation turn. An individual segment still fails explicitly if its WAV would
exceed 8 MiB; there is no whole-turn 8 MiB cap. Workers release PCM between
segments and pace each segment to keep browser buffering bounded. The report
records source generated text, segmentation, per-segment TTS/ASR results, voice,
latencies, audio duration and ordered recording URLs.

Run the normal workshop migration hook before deploying this revision. The only
schema addition is `fs2_workshop.audio_segments`; existing serving tables and
legacy `fs2_workshop.audio` WAV routes are untouched. Artifacts are keyed by run,
turn, attempt and segment. Version/owner/unexpired-lease checks fence persistence.
An interrupted attempt's already-retained segments remain explicitly partial
artifacts in run events; only a successfully committed turn references them in
the transcript. Replay routes authenticate the run owner and do not expose
cross-team audio. UI recordings are ordered segment buttons; no oversized merged
WAV is constructed in the browser or worker.

Temporary database claim/heartbeat failures retry with bounded delays, not worker
task termination. Expired leases cannot be revived or commit a turn. Uncertain
in-flight work becomes interrupted and requires explicit Resume; it is not
automatically replayed. Transient polling errors use a separate connection-status
message that clears on recovery, without erasing actionable validation errors.
The microphone button remains disabled until the current speaker is taken over.

## Validation

Local tests use a dedicated disposable PostgreSQL database, real LISTEN/NOTIFY,
mocked model HTTP streams and native Web Audio scheduling unit doubles. They
cover early PCM delivery before synthesis completes, 4 KiB envelopes, ownership,
bounded queue gaps, reconnect/WAV references, cancellation, lease expiry,
database recovery, long-text preservation, sample-rate mismatch, segmented ASR,
10 MiB of total audio in bounded uploads, partial artifact fencing and pending
takeover Resume. Native JavaScript tests verify immediate scheduling, PCM16
decoding and synchronous queued-source cancellation.

Deterministic virtual-clock tests deliver a 200,000-byte first TTS frame and 200
tiny tail frames through the actual `LiveAudioBus` callback and consumer at
8/22.05/96 kHz. They verify lossless ordered delivery, immediate audio before
provider completion, lead <=0.5 seconds, fewer than 64 queued events, no gaps and
no wall-clock audio-duration sleeps. A real PostgreSQL intervention cancels a
worker while its pacing wait is pending, closes the HTTP response and retains no
partial turn. Existing LISTEN/NOTIFY delivery and explicit provider-error tests
remain part of the suite.

The r9 spoken run `33c26d98-4ac6-42ef-a3df-0b00f4e32629` completed and retained all
four turns, but a `playback.gap` during the final patient turn means seamless live
audio acceptance **failed**. Its original functional `passed:true` receipt remains
unchanged. The summarized capture omitted `reason`, so queue overflow versus
listener reconnect cannot be proven retrospectively. The revised browser test
retains `reason`/`code`, saves functional completion evidence, and requires zero
`playback.gap` or `playback.error` events for a healthy acceptance pass. Final r10
under-load verification belongs to the coordinated release owner.

These are not a substitute for final deployed-browser, real speech-model and
frozen rehearsal acceptance. The parent deployment task owns those checks and
the final image rollout. No GPU, quota or infrastructure resources were changed
by this implementation.
