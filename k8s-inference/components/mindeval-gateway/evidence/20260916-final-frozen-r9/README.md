# Frozen r9 canonical acceptance — PASS; live-audio acceptance remains separate

The complete canonical programme passed on unchanged workshop r9, gateway and
CP138 images from 2026-09-16 18:01:39 to 18:15:35 UTC. This does **not** declare
the whole spoken workshop ready: the coordinator independently observed one
`playback.gap` during a concurrent four-turn spoken browser run. Its retained
WAV was intact, but live playback was incomplete. That real voice defect is
not hidden by these canonical passes; the coordinator owns its fix and the
subsequent r10 acceptance freeze.

## Results

| Cohort | Completed jobs | TF calls | Tokens | Full patient-prefix coverage | Elapsed incl. receipts |
| --- | ---: | ---: | ---: | ---: | ---: |
| Ten teams, six clinicians, two rounds — repetition 1 | 60/60 | 300 | 1,595,949 | 180/180 | 321.785 s |
| Ten teams, six clinicians, two rounds — repetition 2 | 60/60 | 300 | 1,619,038 | 180/180 | 328.023 s |
| All six clinicians, ten rounds, profile-020 | 6/6 | 126 | 668,177 | 66/66 | 172.123 s |

Total: 126 completed benchmark jobs, 726 successful TF calls, 3,883,164 tokens,
426 completed classifier-prefix assessments, zero provider retries, zero failed
or aborted benchmark jobs. The separate intervention-control run was deliberately
aborted and correctly excluded from benchmark eligibility.

Every benchmark report passed exact finite 1–6 values for all five named rubric
criteria, the fixed Gemma27B judge, canonical benchmark eligibility, exact
transcript length (five or 21 messages), and every patient prefix classified
without an error or truncation. No report was repaired or failed job retried.
MindGuard4B was an observer, not an adjudicating expert; no human gold labels
exist for these generated conversations and no classifier accuracy is claimed.
The maximum classifier input in the full cohort was 12,109 tokens. Short initial
prefixes preserve the model's short-context warnings in the raw reports.

## Fairness, limits and queueing

- Ten distinct ordinary platform principals shared tenant `mindeval-rehearsal`.
  Initial progress was observed within 17.161–32.183 s in repetition 1 and
  16.810–21.060 s in repetition 2. No team starved.
- Repetition 1 peaked at 50 running plus ten queued; repetition 2 at 49 running
  plus eleven queued while team ten also had a spoken browser job. No sampled
  team had more than five running test jobs. Existing concurrent browser/spoken
  checks make this an event-shaped rehearsal, not isolated throughput testing.
- All jobs were terminal at 278.859 s / 289.552 s respectively. Remaining time
  was read-only sequential report collection, not inference completion latency.
- Mean queue wait was 16,657.570 / 15,947.915 ms; mean total per-call latency was
  22,952.327 / 27,273.812 ms. Across the two repetitions, 120 Gemma judge calls
  consumed 2,467,342 tokens, with mean queue 68,951.963 ms and maximum queue
  190,448.462 ms. Bursts waited rather than receiving failed or invented scores.
- Twenty-profile registration succeeded; 21 profiles were rejected with 422
  by gateway and workshop without creating extra inference jobs. Missing PAT
  returned 401, missing model grant 403, and cross-team detail/events/report 404.
  Repeating each creation key returned the same six IDs with no hidden jobs.
- Pause, fresh-client reconnect, takeover, typed human turn, nudge, resume and
  abort all passed; the audit trail persisted and late replies did not alter
  the aborted transcript.

## Exact six-clinician full cohort

Patient: `Qwen/Qwen3-30B-A3B-Instruct-2507`. Fixed judge:
`google/gemma-3-27b-it`. Each run registers the entire comparison suite so the
judge-family exclusion covers every selected clinician. All runs used original
`profile-020`, canonical `Hello` initialization, ten rounds, temperature 0.7 and
4,096 maximum completion tokens. No spoken synthesis is implied by this
canonical-mode cohort.

| Clinician | Run ID |
| --- | --- |
| Qwen/Qwen3-235B-A22B-Instruct-2507 | e126a284-d169-441b-9acb-63b301bf683c |
| Qwen/Qwen3-30B-A3B-Instruct-2507 | 96ff13b8-83b7-4d35-8fc9-74f56543d21e |
| deepseek-ai/DeepSeek-V4-Pro | 70addab0-9aa1-40f6-b642-543d37482050 |
| nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B | d468424a-0037-4e72-9423-c7a5ff21e33a |
| openai/gpt-oss-120b | dcf977c4-aaa9-4203-a945-c9622d2c3ac0 |
| zai-org/GLM-5.1 | 9af714bb-be9e-4543-b57c-2363f87d96de |

These synthetic scores are not clinical validation, therapeutic advice, a
statistically established ranking or proof of safety. Fixed-judge calibration
and its human-field mapping/bias limitations are documented separately in
`../20260916/CALIBRATION.md`.

## Verified release provenance

`freeze-before.json` and `freeze-after.json` independently read Kubernetes and
Helm. Deployment generations, image digests, readiness and Helm revisions are
identical before and after the entire programme; the after receipt also contains
actual running container image IDs. Namespace `fs2-system`, cluster context
`fs2-storage-h100`:

- Workshop: `sha256:377451e2a4296b7713aec33fb4ad6e2fd9bc49dec6291866bc4b6ca7e5a6261c`,
  generation 7, two ready replicas, Helm revision 7.
- Gateway: `sha256:4d7c32408cdc7a8bb5df55b73408146d6fe5aee759927f9211790b0874ef2db7`,
  generation 1, one ready replica.
- CP: `sha256:d0b3b02e201d8dc34ca7368e8ad34fece02e15841595de8bb970748c3dc300d6`,
  generation 249, three ready replicas, Helm revision 138.

LLMs used only public `https://api.tokenfactory.nebius.com/v1`; live catalog
metadata and provider regions are retained per registration. Physical hosted
GPU/preemptibility is not exposed by that API. No GPU or service was provisioned
by this test worker. No physical-node-loss test is claimed; graceful speech Pod
drain is separate worker evidence. The gateway remains explicitly singleton,
not a distributed or HA inference queue.

The r9 public runner used `https://89.169.99.188` with `--insecure`, so that
historical invocation did not verify TLS. This is not evidence of a self-signed
certificate: subsequent ordinary `curl` GET verification succeeded with HTTP200,
and the coordinator verified a publicly trusted Let's Encrypt certificate.
Current reproduction commands retain normal TLS verification. Only protected ordinary PATs were used on
the customer path; every saved response was checked against all supplied token
values. Neither PATs nor the raw TF credential are in these artifacts.

## Reproduce the programme

After coordinator approval of a ready, frozen release, from the gateway component:

```sh
.venv/bin/python scripts/rehearse_workshop.py \
  --base-url https://89.169.99.188 \
  --keys-file /path/to/protected-ten-team-keys.json \
  --output evidence/NEW-UNIQUE-LABEL --run-label NEW-UNIQUE-LABEL \
  --gateway-image REGISTRY/IMAGE@sha256:VERIFIED_GATEWAY_DIGEST \
  --workshop-image REGISTRY/IMAGE@sha256:VERIFIED_WORKSHOP_DIGEST \
  --repetitions 2 --full-dialogue-turns 10 --full-dialogue-all-clinicians \
  --poll-seconds 10 --timeout-seconds 2400
```

Use a fresh label to create genuinely new runs, coordinate protected submission
windows with other same-principal clients, and independently capture live image
provenance before/after. This command creates paid real inference work; do not
run it solely to inspect these saved results.

Local deterministic checks: 64 gateway/rehearsal tests passed, including strict
invalid-output behavior, all bounded retry statuses, transport failures,
Retry-After, exact compatibility fallback, fair queues, grants and read-only
receipt recovery. Faults were injected into mock transports, not shared services.
Ruff passed; two upstream test-client deprecation warnings remain non-failing.
