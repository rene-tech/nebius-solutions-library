# MindEval workshop release — 2026-09-16

## Scope correction: no hosted workshop webpage

The user requested removal of the workshop website after the r10 tests below.
`/workshop`, its static assets, serving routes and Terraform `workshop_url`
output are removed. The backend APIs, model services, credentials and stored
run/audio evidence remain. Browser results below are historical r10 evidence,
not an instruction to host or recreate a workshop website.

## Entry points and scope

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
| Workshop UI, durable worker, voice policy | `82c624cbc`, Helm 8, applied by Terraform | `sha256:987feef5dc66883351dfae377878f02e233dca5af96004a7453662f0ab6c3392` |
| Calibrated Token Factory gateway | Same Helm 8 add-on | `sha256:4d7c32408cdc7a8bb5df55b73408146d6fe5aee759927f9211790b0874ef2db7` |

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
| Workshop regression | 54 passed plus one environment-dependent ONNX test skipped on the manager host; worker ran all 55 with the actual checkpoint. Seven JavaScript tests pass. The image build also loads the pinned ONNX model as production UID 10001. |
| Gateway error behavior | 69 tests pass, including bounded 429/5xx/transport retries, permanent errors, strict score parsing, fair scheduling and bounded parallel diagnostic collection. |
| Browser typed controls | Fresh r10 run `2b91ef88-b83e-4543-829a-0372f88c72e6`: pause, nudge, patient/clinician takeover, both typed turns, resume, abort and exclusion labels pass in16.6 seconds. Patient/clinician takeover readiness waits191/4,963 ms. |
| Browser manual microphone | r10 run `a26ce896-ea39-426d-9774-d402a91a0eb1`: browser PCM capture, real ASR, retained human transcript and validated159,788-byte /4.992-second WAV, browser decode and abort pass. Initial45-second test deadline while awaiting takeover is preserved; the same queued run was resumed, not replaced. |
| New voice Apps / named MCP | All three named tools, public streaming routes, retained results and ordinary usage pass on CP138. Magpie's downloadable WAV duration exactly matches the output-audio ledger. Both existing Nemotron Apps also pass. |
| Speech scaling | All three Apps reached two managed replicas through ordinary admin proposals. Distinct-worker overlap and scale-out during an admitted stream pass. Parakeet graceful Pod drain passes. Configured floors restored to min 1 / max 2; HPA may retain a second warm replica during normal stabilization. |
| r10 canonical rehearsals and full cohort | Both60-job rehearsals and all six ten-round clinicians pass:126/126 jobs,426 complete patient prefixes,726 successful logical Token Factory calls,733 provider attempts,3,967,901 reported tokens. Seven transport-error retries recovered; no final job failures or truncation. Images/generations/Helm releases remained unchanged. |
| Browser automatic microphone | Full r10 script passes on `22a81a02-4b15-4a49-b1d7-00b80e7aea52`: Silero silence fallback, no manual Finish, HTTP 200 / 69,676-byte WAV and real browser decode (2.176 seconds). Earlier r9 run also exercised genuine Parakeet model EOU. |
| Browser spoken continuity | r10 run `0783b3d4-45d7-4e10-9260-9d0a83dea83e`: four turns, six recordings, all five scores, live PCM before completion, barge-in/resume, zero explicit playback-gap/error events. Maximum producer lead0.499979 seconds. **One ASR readiness/retry wait took138.488 seconds; this is not a real-time latency guarantee.** r9's genuine playback-gap failure remains preserved. |

Persisted server-side spans for the two rehearsals were428.947 and263.509
seconds; the full six-clinician cohort took617.909 seconds. GLM's slowest call
took115.141 seconds with near-zero gateway queue time. The first rehearsal's
observer elapsed time also includes a separately recorded52-second coordination
pause; it is not presented as inference time. The seven recovered transport
errors were Gemma judging calls in repetition1. Token usage above is reported
successful-response usage; consumption on failed transport attempts is unknown.

The initial typed-controls and microphone harness deadlines (90 and45 seconds)
expired while awaiting a patient turn under load. Both original runs later became
ready, were exercised and aborted; diagnostics are retained. The fresh full
controls test uses a300-second **test-only** wait and records actual elapsed
readiness; no platform concurrency, memory, provider or cloud limits were raised.

The public r8 diagnostic completed 60/60 jobs, all 300 provider calls and all
180 expected patient-prefix classifier observations without retries. It is
intentionally **not** final frozen-release acceptance: later testing found and
fixed the generic native-WAV parser and the pending-takeover UI control race.
Earlier Host-check, microphone-finish and image-permission failures are also
preserved rather than rewritten as successes.

Final rehearsal receipts belong under
`components/mindeval-gateway/evidence/20260916-final-frozen-r10/`.
Older r9's126-job canonical pass remains historical evidence, not the deployed
audio-fix release. Browser receipts are under
`components/mindeval-workshop/evidence/browser-r10/`, automatic-Finish receipts
under `components/mindeval-workshop/output/playwright/auto-finish/`, and fresh
controls/manual-microphone receipts under repository-root `output/playwright/browser-r10/`;
voice model benchmarks and deployment receipts are under
`acceptance/voice-agent-20260916/`.

## Explicit limits and remaining gates

- Public MindGuard4B/8B weights load and infer successfully after the Hugging
  Face approval. The separately gated `swordhealth/MindGuard-testset` pinned
  parquet still returned authenticated403 at18:10 UTC; gold-label comparison
  remains blocked. Generated probes cannot establish labeled accuracy.
- Sword's private MindGuard v2 clinician artifact has not been supplied. The
  public classifiers are not substitutes for that clinician.
- New voice/classifier GPU snapshot restore is **not qualified and is disabled**.
  Regional images/weight caches and hot replicas are active. Do not advertise
  cached weights as a GPU checkpoint, or set a snapshot option without a real
  restored public-path cohort.
- Graceful Pod drain, bounded protocol failures and database lease recovery are
  covered; no controlled physical-node/preemptible-VM failure was injected.
  During the final spoken run, a burst H100 node naturally became unreachable,
  one native ASR attempt recorded `runtime_transport_error`, and its retry
  succeeded on the continuously Ready reserved hot pod. The failed attempt's
  endpoint identity was not retained, so direct causal attribution is an
  inference.138.488 seconds is observed readiness/recovery, **not an enforced
  maximum**. See [ASR failover diagnosis](nemotron-asr-latency-r10-20260916.md).
  Current Service routing has no hot-first preference. Scoped connect-timeout
  and hot-first/burst-fallback improvements need separate implementation and
  failure testing; generic long-recording read timeouts were not lowered.
  Follow-up Task Deck card: `fs2-workshop-asr-failover-latency-r20260916`
  (prepared/ready, not implemented or running).
  Existing unavailable nodes were not deleted or called healthy.
- Real Pipecat/RTVI framework execution uses centralized public HTTP/streaming
  endpoints; it is not a qualified browser WebRTC/RTVI transport. The browser
  instead uses the tested native WebSocket/Web Audio path.
- The fifty-worker rehearsal measures canonical text evaluation, with a small
  number of concurrent browser/voice checks. It is not qualification of fifty
  simultaneous spoken sessions. Speech evidence covers the documented paired
  workers, languages/voices, scope of scale/drain tests and individual full
  spoken flows. Select hot speech floors for the actual event mix, not from the
  text-worker count alone.
- Optional automatic microphone Finish is English-only. Actual model EOU/EOB,
  Silero silence fallback and phrase-rule observations have distinct provenance;
  no fabricated model EOB or automatic backchannel speech is presented.
- The existing platform retention-maintenance foreign-key failure is documented
  in the operator runbook and was not hidden by deleting scientific evidence.

## Deployment and handoff

The additive Helm chart has an accompanying Terraform module and example root.
Terraform init/validate/fmt and Helm lint pass. The live release was initially
installed with Helm, imported into the module's protected Terraform state, and
then **successfully updated to r10 by Terraform**: zero creates, one in-place
Helm update, zero destroys, completed in 32 seconds. Both workshop replicas are
Ready. A subsequent plan exited zero with **No changes**. This qualifies adoption
and an in-place add-on update, not creation of a new cluster or a destructive
teardown test.

Operator-only state, exact inputs and saved plan are in
`/home/tux/secure-handoff/scientific-ai-mindeval-20260916/`:
`workshop.tfstate`, `workshop.tfvars.json`, `workshop-r10-clean.tfplan`.
The example root is `k8s-inference/examples/mindeval-workshop`; always select this
existing protected state rather than applying against an empty default state.
The successful apply used:

```sh
terraform -chdir=k8s-inference/examples/mindeval-workshop apply -input=false \
  -state=/home/tux/secure-handoff/scientific-ai-mindeval-20260916/workshop.tfstate \
  /home/tux/secure-handoff/scientific-ai-mindeval-20260916/workshop-r10-clean.tfplan
```

Terraform outputs expose the workshop API and gateway API listed
above; the webpage URL output is removed. Public HTTPS was checked with normal certificate verification: the live
Let's Encrypt certificate includes the IP in its SAN, and GET `/workshop` returns
200 with TLS verification result zero. The final r10 runner does not disable TLS
verification; older r9 diagnostic invocations used `--insecure` and remain labeled
as such in their receipts.

Read the [API documentation](../components/mindeval-workshop/README.md)
and [operator runbook](../components/mindeval-workshop/docs/operator-runbook.md).
The reproducible prerecorded fallback is clearly labeled, includes both voices,
and was tested offline in Chromium with no external requests. Its ZIP is in the
protected handoff directory, SHA-256
`63bf4df12661c958c09331364faa002a286b72bef2cc6247a21a78800bb340bc`.
It must never be presented as a live response.
