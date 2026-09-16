# Nemotron Speech onboarding — 2026-09-16

Status: native runtime, long-file, medical and snapshot milestones passed;
public integration underway, **not ready for customer use**. Task Deck card:
`fs2-nemotron-speech-full-streaming-file-onboarding-r20260916`.

Storage handover is complete as documentation, not full storage acceptance. The
user owns the exhausted IAM storage policy quota. Speech work must not increase
limits, modify Stockholm storage, resume BioIR, or implement LibreChat UI changes.

## Source and runtime decision

Clean worktree: `/home/tux/worktrees/fs2-nemotron-speech-20260916`, detached from
`1cb5e6839` (deployed storage-compatible release plus handover). No new branch.
Main checkout contains unrelated uncommitted work; never build it wholesale.

| App | Upstream checkpoint | Exact revision | License |
| --- | --- | --- | --- |
| English | nvidia/nemotron-speech-streaming-en-0.6b | ebe59e5a817142986528bbbee5dba8db7b38ed50 | NVIDIA Open Model License |
| Multilingual | nvidia/nemotron-3.5-asr-streaming-0.6b | ea30d66debe3740a08b573244286791d423d6b3e | OpenMDW 1.1 |

Both Hugging Face API records are public/ungated. This establishes checkpoint
access, not NIM entitlement. NVIDIA documents one Nemotron NIM with English and
multilingual profiles; its engine supports streaming only. A file API can feed
the complete decoded recording through a streaming session and aggregate finals.

On this development host, the read-only registry lookup for
`nvcr.io/nim/nvidia/nemotron-asr-streaming:latest` failed at `/v2/` with HTTP 403.
No image digest was obtained, and no credential or entitlement diagnosis can be
inferred from that alone. Do not deploy `latest`. NIM option coverage remains
unqualified. The deployment guide and support table also disagree on default
batch sizes (64 versus 128); actual immutable profile metadata must decide.

The initial source adapter uses NVIDIA NeMo's existing cache-aware streaming
pipeline at `3b08b2acacc13ec1268e53653346266202b2335f`. It reuses NVIDIA's feature
buffering, prompt conditioning, encoder caches, decoder and EOS handling rather
than implementing a new ASR engine. NIM remains a candidate, not falsely marked
upstream-unsupported. Transformers 5.17.0 was also inspected: the pinned
multilingual HF encoder config omits right-context 1 (160 ms) although the model
card documents it, so full chunk coverage cannot be assumed from that path.

## Capability coverage and outstanding work

Current detailed evidence is in
[`medical quality`](../acceptance/nemotron-speech-20260916/MEDICAL-QUALITY.md)
and [`fresh snapshot restore`](../acceptance/nemotron-speech-20260916/SNAPSHOT-RESTORE.md).
Both 30-minute synthetic files and all five supplied medical recordings were
processed completely. English approximate mixed-speaker WER is 16–18% for the
English model and 19–22% for multilingual; medical-term errors remain. The HHU
German long recordings have no verified transcript; their first paced partial took 9.015 s from playback
start, about0.615s after the model-aligned first word (not verified onset). Do not
claim clinical accuracy, acceptable live latency or customer readiness.

The separate complete human-transcribed MultiMed German test split is now
benchmarked: **16.94% WER,8.88% CER** across1,091clips/30,709words,3.795haudio
processed in12.43min on the warmed restored H100. All90timing-repeat transcripts
match; one spoken-reference clip produced no transcript. See the full
[German benchmark report](../acceptance/nemotron-speech-20260916/GERMAN-MULTIMED.md),
which retains failures, normalization, exact conditions and raw word alignments.

Public backend and both Apps are deployed. Complete files, typed MCP and full
real-time-paced live streams pass through ordinary customer keys, including a
nine-call mixed cohort. Multipart scratch, shared-App policy, KEDA name length
and busy-worker failures have been corrected. The mixed cohort exposed a burst
startup scratch-space defect; its fix and final scaling qualification are in
progress. See [public integration evidence](../acceptance/nemotron-speech-20260916/PUBLIC-INTEGRATION.md).
Negative receipts remain; these results do not complete all acceptance gates.

Initial H100 direct-runtime evidence is retained in
[`acceptance/nemotron-speech-20260916`](../acceptance/nemotron-speech-20260916/README.md).
Both models passed six short synthetic-English repetitions, with live partials
before EOS. This does not qualify the public path, long files or all languages.
All rows still require public-path evidence; CPU tests alone do not qualify them.

| Capability | Upstream / selected adapter | Platform status |
| --- | --- | --- |
| Live incremental audio, early partials, final flush | NeMo `Frame` / `transcribe_step` | Both models passed complete public paced consultations at560ms, including simultaneous file/MCP work |
| Complete file transcription | Same stream, all frames through EOS | All five complete medical recordings passed public multipart/artifact APIs;30-minute recordings passed privately, public30-minute qualification remains |
| English 80/160/560/1120 ms | Left context 70, right 0/1/6/13 | Strict options and profile validation; GPU matrix pending |
| Multilingual additionally 320 ms | Left context 56, right 0/1/3/6/13 | Strict options and profile validation; GPU matrix pending |
| 32 out-of-box locales | 19 primary + 13 broad-coverage | Identifiers CPU-tested; English/German recordings measured, other languages unqualified |
| Eight adaptation-only locales | Require an adapted checkpoint | Explicitly rejected; never advertised ready |
| Explicit locale / automatic language selection | Per-stream NeMo language prompt | Adapter passes resolved language; real verification pending |
| Keep/remove language tags | Immutable worker profile | Matching enforced; detection metadata/API routing pending |
| Native punctuation/capitalization | Base checkpoint output | Medical quality measured; medical-term errors and occasional missing segment-boundary spaces remain |
| Greedy and MALSD beam decoding | Pinned NeMo pipeline supports both | Immutable profile settings; GPU/API qualification pending |
| Segment/word output and confidence | NeMo pipeline outputs/configuration | Native alignments retained in full medical receipts; confidence remains null in measured default profile |
| Phrase boosting / external n-gram LM | NeMo pipeline supports biasing | Not implemented; capability gap, not upstream unsupported |
| Alternate sample rates/stereo/file codecs | Incremental ffmpeg decoding | Implemented with sample-preservation CPU tests; full codec GPU matrix pending |
| ITN / translation / diarization | Separate pipeline components/models | Not claimed as base-checkpoint functionality; separate-model work out of scope |
| GPU snapshots | Clean loaded worker, no customer data | Both fresh-Pod restores passed with exact full-recording parity; restore-call sums 6.717/8.552 s, public startup/controller publication pending |

## Integration sequence

1. Verify pinned NeMo runtime on task-owned existing GPU capacity. Test short
   synthetic audio, early partials and final words; fix runtime defects before
   wrapping an unproven engine in public routes.
2. Add bounded authenticated WebSocket sessions and file compatibility/native
   routes through the existing gateway. Use existing tenant/principal/grant and
   operation mechanisms. One active connection stays pinned to its worker.
3. Reuse existing uploads/artifacts and durable operations for long recordings;
   authorize references with the caller identity, never an arbitrary bucket URL.
   Customer buckets remain optional while their separate provisioning is blocked.
4. Add two Apps, typed file/job tools, admin visibility, configurable runtime
   profiles, live-session admission metrics and capacity-aware scaling. Separate
   interactive reservation from batch dispatch so files cannot occupy every slot.
5. Complete feature matrix, public customer-path cohorts, cold/warm measurements,
   snapshot feasibility and LibreChat handover. Do not mark ready before evidence
   and exceptions are presented to the user.

## Engineering gates before qualification

These are test gates, **not a customer SLA**. Freeze the exact candidate release,
GPU, precision, runtime profile and corpus before a cohort.

- Direct-runtime diagnostic: synthetic known speech, audible tail word retained,
  nonempty partial before EOS, one warmup plus three retained repetitions in both
  real-time-paced and file-speed modes for each model. No percentile claims.
- Public correctness: short, multi-minute and 30-minute files/model; every audio
  sample consumed once, bounded memory, expected transcript content and tail;
  silence and malformed requests handled explicitly. Record WER/CER against the
  chosen public/synthetic fixture transcripts and distinguish tested languages.
- Latency: on the initial 560 ms profile, aim for first useful partial within
  2 seconds of fixture speech onset and finalization within 2 seconds of EOS at
  admitted baseline load; warm file real-time factor < 1. Report failures, never
  silently increase thresholds. Wider chunk profiles have separate baselines.
- Lifecycle: zero silent truncations or stuck successful jobs, attributable
  errors and cancellation, no cross-tenant result leakage, explicit worker-loss
  retry semantics. Overload must queue or reject understandably, not hang.
- Mixed live/batch and concurrent tenants: sustain the measured admitted stream
  count, demonstrate scale-out/from-zero, drain active sessions on normal
  scale-down, and interrupt only task-owned workers. Two consecutive identical
  release cohorts must pass. All mandatory capabilities remain release gates.
- Snapshot: clean-model capture only; exclude customer/session state, verify
  restored live and file behavior, and compare acquisition/load/restore phases.
  Unsupported runtime constraints require precise evidence, not a enabled flag.

## LibreChat contract verified from local build source

`/home/tux/worktrees/librechat-scientific-branding/life-science/bionemo-librechat/Dockerfile.base`
pins LibreChat `3f27726e10bdd35d98f5fbbae7aab35af94c8436`.
Its `api/server/services/Files/Audio/STTService.js` sends multipart `file`, `model`
and optional two-letter `language`, with bearer authentication, to the configured
STT URL. The locale is shortened: native requests must retain full locale support,
and compatibility aliases must document their chosen locale (en→en-US, fr→fr-FR,
pt→pt-BR, es→es-US).

`useSpeechToTextExternal.ts` collects MediaRecorder chunks and submits on stop.
Changing the STT URL alone does not provide live partial transcription. A separate
LibreChat client adapter is needed for live PCM/WebSocket use. This task provides
the server contract/reference client and handover, not that UI implementation.

## Resources / deployment boundaries

Read-only inventory on 2026-09-16: Scientific AI cluster
`mk8scluster-e00j5z9te7x5dd9g6a` / project-e00rene / eu-north1 has unused
preemptible H100 and regular L40S GPU slots. Initial diagnostics reused H100s without
creating nodes or changing quotas. Initial diagnostic Jobs completed and were
removed. The two private restored benchmark Pods were removed after retaining
evidence; clean snapshot directories remain on the existing shared PVC. Two
public speech Apps are configured with one hot H100 replica each and maximum two. Gateway
rollout includes the speech backend/audio upload scratch; sibling registrations
are preserved. Production snapshots are not yet enabled. Resource identities,
raw traces and phase-separated timings are recorded in the acceptance directory.
Storage limits remain user-owned.

## Sources

- [Pinned English card](https://huggingface.co/nvidia/nemotron-speech-streaming-en-0.6b/blob/ebe59e5a817142986528bbbee5dba8db7b38ed50/README.md)
- [Pinned multilingual card](https://huggingface.co/nvidia/nemotron-3.5-asr-streaming-0.6b/blob/ea30d66debe3740a08b573244286791d423d6b3e/README.md)
- [NVIDIA NIM deployment](https://docs.nvidia.com/nim/speech/latest/asr/deploy-asr-models/nemotron-asr-streaming.html)
- [NVIDIA NIM support matrix](https://docs.nvidia.com/nim/speech/latest/reference/support-matrix/asr.html)
- [Pinned NeMo pipeline](https://github.com/NVIDIA/NeMo/blob/3b08b2acacc13ec1268e53653346266202b2335f/nemo/collections/asr/inference/pipelines/cache_aware_rnnt_pipeline.py)
- [Pinned LibreChat STT service](https://github.com/danny-avila/LibreChat/blob/3f27726e10bdd35d98f5fbbae7aab35af94c8436/api/server/services/Files/Audio/STTService.js)
