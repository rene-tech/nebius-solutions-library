# MindEval workshop release — 2026-09-16

## Entry points and scope

- Participant interface: <https://89.169.99.188/workshop>
- Resumable workshop API: <https://89.169.99.188/v1/workshop>
- Token Factory gateway: <https://89.169.99.188/v1/mindeval>
- Existing platform administration and MCP: `/admin` and `/mcp` on the same origin.

Use an ordinary platform key granted the `mindeval` workflow and the requested
speech/classifier models. There is no separate attendee identity system. The
ten September rehearsal principals have a five-worker cap and one-day keys;
these are **not** October event credentials. Issue event-dated keys through the
normal administration workflow before admitting attendees.

This is a synthetic-profile research workshop, not a clinical service. Canonical
text runs use the original pinned MindEval prompts and a fixed Gemma judge.
Spoken and human-intervened runs are explicitly excluded from untouched text
comparison aggregates. MindGuard is an observation, never the judge, clinician,
or a newly imposed request blocker.

## Frozen deployment

Project `project-e00rene`, region `eu-north1`, cluster
`mk8scluster-e00j5z9te7x5dd9g6a`. The existing regular L40S pool hosts the three
new speech Apps and two public MindGuard classifiers. No cloud quotas, node
groups, neighboring model images, or administrator credentials were changed.

| Component | Source / release | Immutable image digest |
| --- | --- | --- |
| Shared control plane and model controller | `1f745bf18`, Helm 138 | `sha256:d0b3b02e201d8dc34ca7368e8ad34fece02e15841595de8bb970748c3dc300d6` |
| Workshop UI, durable worker, voice policy | `762e7c5ea`, Helm 7 | `sha256:377451e2a4296b7713aec33fb4ad6e2fd9bc49dec6291866bc4b6ca7e5a6261c` |
| Calibrated Token Factory gateway | Same Helm 7 add-on | `sha256:4d7c32408cdc7a8bb5df55b73408146d6fe5aee759927f9211790b0874ef2db7` |

Regional repository prefix is
`cr.eu-north1.nebius.cloud/e00akg9ndpx77eaexh/fs2-platform`.
Control-plane image name is `fs2-serve-control-plane`, workshop image name is
`fs2-mindeval-workshop`, and gateway image name is `mindeval-gateway`.
Build attestations and OCI archives are retained in the protected operator
handoff directory, not alongside participant credentials in the repository.

The integration branch is `agent/fs2-mindeval-workshop-r20260916` in
`rene-tech/nebius-solutions-library`. It starts from deployed speech/storage
source `b045b941e`, preserving those features. The unrelated dirty main worktree
was not overwritten. Previous control-plane digests and failed diagnostic
releases remain in Helm history and the Task Deck work logs.

## Functional evidence

| Path | Evidence and current result |
| --- | --- |
| Final control-plane regression | 2,117 passed, 102 skipped; skips are not counted as passes. |
| Workshop regression | 49 passed plus one environment-dependent ONNX test skipped on the manager host; worker ran all 50 with the actual checkpoint. Seven JavaScript tests pass. The image build also loads the pinned ONNX model as production UID 10001. |
| Gateway error behavior | 64 tests pass, including bounded 429/5xx/transport retries, permanent errors, strict score parsing, fair scheduling and diagnostic collection. |
| Browser typed controls | Final r9 run `0fab24b1-a0a1-4ace-9915-8b1784775783`: pause, nudge, patient/clinician takeover, both typed turns, resume, abort and exclusion labels pass under concurrent rehearsal load. |
| Browser manual microphone | Final r9 run `39e53681-913a-4e1e-9d74-807adcac3cdf`: browser PCM capture, real ASR, retained human transcript/WAV and abort pass. |
| New voice Apps / named MCP | All three named tools, public streaming routes, retained results and ordinary usage pass on CP138. Magpie's downloadable WAV duration exactly matches the output-audio ledger. Both existing Nemotron Apps also pass. |
| Speech scaling | All three Apps reached two managed replicas through ordinary admin proposals. Distinct-worker overlap and scale-out during an admitted stream pass. Parakeet graceful Pod drain passes. Configured floors restored to min 1 / max 2; HPA may retain a second warm replica during normal stabilization. |
| r9 canonical rehearsals and full cohort | Both 60-job rehearsals and all six ten-round clinicians pass: 126/126 jobs, 426 patient prefixes, 726 Token Factory calls, 3,883,164 tokens. Images remained unchanged. This is canonical-path acceptance, not a seamless-live-audio claim. |
| Browser automatic microphone | Final full r9 script passes on `22cbfebe-b3cb-47b7-8438-da37a3f93af3`: Silero silence fallback, no manual Finish, HTTP 200 WAV and real browser decode. Earlier r9 run also exercised genuine Parakeet model EOU. |
| Browser spoken continuity | Four turns, six retained WAV segments, live PCM, barge-in/resume and all judge scores completed on `33c26d98-4ac6-42ef-a3df-0b00f4e32629`, **but one live playback gap interrupted the last turn**. Durable audio is intact. This is not seamless-live acceptance; producer pacing fix and stronger no-gap test are pending. |

The public r8 diagnostic completed 60/60 jobs, all 300 provider calls and all
180 expected patient-prefix classifier observations without retries. It is
intentionally **not** final frozen-release acceptance: later testing found and
fixed the generic native-WAV parser and the pending-takeover UI control race.
Earlier Host-check, microphone-finish and image-permission failures are also
preserved rather than rewritten as successes.

Final rehearsal receipts belong under
`components/mindeval-gateway/evidence/20260916-final-frozen-r9/`.
Browser receipts are under `components/mindeval-workshop/evidence/browser-r9/`;
voice model benchmarks and deployment receipts are under
`acceptance/voice-agent-20260916/`.

## Explicit limits and remaining gates

- Public MindGuard 4B/8B weights load and infer successfully after the Hugging
  Face approval. Access to the separately gated gold test dataset must be
  verified independently; generated probes cannot establish labeled accuracy.
- Sword's private MindGuard v2 clinician artifact has not been supplied. The
  public classifiers are not substitutes for that clinician.
- New voice/classifier GPU snapshot restore is **not qualified and is disabled**.
  Regional images/weight caches and hot replicas are active. Do not advertise
  cached weights as a GPU checkpoint, or set a snapshot option without a real
  restored public-path cohort.
- Graceful Pod drain, bounded protocol failures and database lease recovery are
  covered; abrupt physical-node/preemptible-VM loss has not been demonstrated.
  The two pre-existing unavailable H100 nodes were not deleted or called healthy.
- Real Pipecat/RTVI framework execution uses centralized public HTTP/streaming
  endpoints; it is not a qualified browser WebRTC/RTVI transport. The browser
  instead uses the tested native WebSocket/Web Audio path.
- Optional automatic microphone Finish is English-only. Actual model EOU/EOB,
  Silero silence fallback and phrase-rule observations have distinct provenance;
  no fabricated model EOB or automatic backchannel speech is presented.
- The existing platform retention-maintenance foreign-key failure is documented
  in the operator runbook and was not hidden by deleting scientific evidence.

## Deployment and handoff

The additive Helm chart has an accompanying Terraform module and example root;
Terraform init/validate/fmt and Helm lint pass. The live release was initially
installed with Helm and has now been imported into the module's protected
Terraform state. Its first adoption plan contains zero creates, one in-place
Helm update and zero destroys. Apply is pending the next qualified workshop
image; validation/import are not represented as a completed apply test.

Read the [attendee quickstart](../components/mindeval-workshop/docs/attendee-quickstart.md)
and [operator runbook](../components/mindeval-workshop/docs/operator-runbook.md).
The reproducible prerecorded fallback is clearly labeled, includes both voices,
and was tested offline in Chromium with no external requests. Its ZIP is in the
protected handoff directory, SHA-256
`63bf4df12661c958c09331364faa002a286b72bef2cc6247a21a78800bb340bc`.
It must never be presented as a live response.
