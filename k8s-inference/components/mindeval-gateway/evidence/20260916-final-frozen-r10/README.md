# Frozen r10 canonical acceptance — PASS, with measured latency qualifications

The complete public customer-path programme passed on unchanged workshop r10,
gateway and CP138 releases from 2026-09-16 18:24:37.507834 to
18:48:03.265443 UTC. All requests used ordinary platform PATs and normal trusted
TLS verification. No failed job, classifier error or invalid judgment was
discarded or changed into a passing result.

## Results

| Cohort | Completed jobs | Logical TF calls / attempts | Successful-response tokens | Completed patient prefixes | Persisted server span |
| --- | ---: | ---: | ---: | ---: | ---: |
| Ten teams, six clinicians, two rounds — repetition 1 | 60/60 | 300 / 307 | 1,601,096 | 180/180 | 428.947237 s |
| Ten teams, six clinicians, two rounds — repetition 2 | 60/60 | 300 / 300 | 1,616,765 | 180/180 | 263.508631 s |
| All six clinicians, ten rounds, profile-020 | 6/6 | 126 / 126 | 750,040 | 66/66 | 617.909446 s |

Total: 126 completed benchmark jobs, 726 successful logical TF calls,
733 provider attempts including seven recovered transport failures,
3,967,901 reported successful-response tokens and 426 completed classifier
prefixes. Zero benchmark jobs failed or were aborted. The separate intervention
control run was intentionally aborted and excluded from benchmark eligibility.
`metrics.json` retains exact persisted timestamps and microsecond differences.

Every benchmark report passed exact finite 1–6 values for all five named rubric
criteria, the fixed Gemma27B judge, canonical benchmark eligibility, exact
transcript length (five or 21 messages), and classification of every patient
prefix without error or truncation. MindGuard4B was an observer, not an expert
adjudicator. No human gold labels exist for these generated conversations; no
classifier accuracy, clinical validity or safety guarantee is claimed. Maximum
full-cohort classifier input was 13,909 tokens. Short-context warnings remain
in the raw reports.

## Actual failures, latency and measurement boundaries

- Seven Gemma judge calls in repetition 1 each encountered one
  `transport_error`, retried within the existing bounded policy, and returned
  a valid judgment. All seven original codes, request IDs and telemetry remain
  in the reports and `metrics.json`; no retry was needed in repetition 2 or the
  full cohort. The transport category does not identify whether the underlying
  failure was specifically a timeout or another network error.
- Token totals describe successful provider responses only. Failed-attempt
  usage and any billing for it are unknown. A recovered call's reported
  `queue_ms` does not include scheduler wait from a failed transport attempt;
  its end-to-end `latency_ms` does include retries. Do not treat that queue field
  as a complete latency decomposition. Maximum recovered logical-call latency
  was 375.577 s.
- Repetition 1 mean reported queue / logical latency was 35,942.869 /
  50,115.714 ms; repetition 2 was 17,135.541 / 23,204.047 ms. Bursts waited for
  the shared budget instead of receiving fabricated scores.
- The six full dialogues had mean reported inference queue 0.072 ms and mean
  logical-call latency 9,439.730 ms. GLM was substantially slower on this
  occasion: its longest successful clinician call took 115.141 s with 0.082 ms
  gateway queue, no retry and unchanged generation parameters. It was allowed
  to finish its original run. The sixth durable job also waited for the
  principal's five-worker limit; job duration is not model-only latency.
- Observer elapsed times including read-only receipts were 488.217 s,
  280.471 s and 623.216 s. Repetition 1 includes a documented 52-second local
  observer pause to let the coordinator create a spoken browser run before
  repetition 2. Durable jobs continued and finished during that pause.
  `operator-observation-pauses.json` records it; server spans above exclude it.
- After the all-terminal poll, the two 60-job receipt phases took another
  12.486 s / 11.328 s. Reports are collected with maximum concurrency five and
  deterministic ordering. These GETs do not create inference work.

## Fairness, grants and interventions

Ten distinct ordinary principals shared tenant `mindeval-rehearsal`. All ten
first progressed within 11.969–19.933 s in repetition 1 and 13.866–19.638 s in
repetition 2. Peak selected-job counts were 50 running plus ten queued and
49 running plus eleven queued respectively; team ten also had a coordinator
spoken job during repetition 2. No sampled team exceeded five running test
jobs and none starved. This was event-shaped concurrent load, not isolated
provider throughput measurement.

Twenty-profile gateway registration succeeded. Twenty-one profiles returned
422 at gateway and workshop without creating extra inference jobs. Missing PAT
returned 401, denied model grant 403, and cross-team detail/events/report 404.
Each repeated creation key returned the same six IDs with no hidden jobs.
Pause, fresh-client reconnect, takeover, typed human turn, nudge, resume and
abort passed; audit history persisted and late responses did not modify the
aborted transcript. See `controls.json` and named checks in `summary.json`.

## Exact full cohort

Patient: `Qwen/Qwen3-30B-A3B-Instruct-2507`. Judge:
`google/gemma-3-27b-it`. Every run registered the entire six-clinician suite, so
judge-family exclusion covered all selected clinicians. Original `profile-020`,
canonical `Hello` initialization, ten rounds, temperature 0.7 and maximum
4,096 completion tokens were unchanged. No speech synthesis is implied by
these canonical-mode jobs.

| Clinician | Run ID |
| --- | --- |
| Qwen/Qwen3-235B-A22B-Instruct-2507 | 97cf6268-9d81-455a-87a4-2146da710715 |
| Qwen/Qwen3-30B-A3B-Instruct-2507 | 5ac013e1-866d-4c09-a7f9-0d71e6da96a6 |
| deepseek-ai/DeepSeek-V4-Pro | f090e43a-a8dd-4aa3-acda-feb1be9ba00b |
| nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B | f81a6521-ca92-4050-ab84-d6be4a7acbd1 |
| openai/gpt-oss-120b | 2d034dca-a89f-4d6d-a7e9-e6a9c0b0e729 |
| zai-org/GLM-5.1 | d43a0d53-ce33-4da4-a0fe-57db73f89e35 |

Judge calibration and its human-field mapping, bias and sparse paired-coverage
limitations remain in `../20260916/CALIBRATION.md`. These synthetic scores are
not a statistically established model ranking or clinical validation.

## Verified frozen release

`freeze-before.json` and `freeze-after.json` independently read Kubernetes and
Helm. Deployment generations, digests, readiness and Helm revisions are
identical. The after receipt at 18:48:54 UTC also records the six actual running
container image IDs, all ready with zero restarts. Namespace `fs2-system`,
context `fs2-storage-h100`:

- Workshop: `sha256:987feef5dc66883351dfae377878f02e233dca5af96004a7453662f0ab6c3392`,
  generation 8, two ready replicas, Helm revision 8.
- Gateway: `sha256:4d7c32408cdc7a8bb5df55b73408146d6fe5aee759927f9211790b0874ef2db7`,
  generation 1, one ready replica.
- CP: `sha256:d0b3b02e201d8dc34ca7368e8ad34fece02e15841595de8bb970748c3dc300d6`,
  generation 249, three ready replicas, Helm revision 138.

The coordinator applied the saved Terraform plan with zero creates, one update
and zero destroys before this freeze. This test worker performed no rollout,
provisioned no GPU and changed no resource limit. LLMs used only public
`https://api.tokenfactory.nebius.com/v1`; model metadata and provider regions
persist per registration. Hosted physical GPU/preemptibility is not exposed
by that API. Gateway scheduling is explicitly singleton, not distributed/HA.

The coordinator's separate r10 spoken-browser test reported four completed
turns, six retained WAVs, valid five-axis scoring, actual PCM playback,
barge-in/resume and zero explicit playback gaps/errors. That does not establish
seamless real-time service: one ASR operation had a measured 138.488-second
readiness delay after `runtime_transport_error` and successful retry. A
concurrent H100 node became unreachable, but the first attempt's serving Pod
was not recorded, so a causal connection is inference rather than proven.
The existing Nemotron hot floor was already one. No timeout or quota was raised
during this freeze. Browser, ASR and speech operational receipts are owned by
the coordinator/voice worker, not included in these canonical TF totals.
No controlled physical-node-loss test is claimed; graceful speech Pod drain
and the naturally observed node issue are different evidence.

## Reproduce and inspect

From the gateway component, only after coordinator approval of a ready frozen
release (this creates paid real inference):

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

Do not add `--insecure`: this r10 programme verified the publicly trusted TLS
certificate normally. Coordinate submission windows with same-principal clients
and capture independent before/after release provenance. Every saved response
was checked against all supplied token values; neither PATs nor the raw TF
credential appear in these artifacts. Synthetic profiles/transcripts are not
real patient data. `sha256sum -c ARTIFACTS.sha256` verifies this receipt set.

Local deterministic suite: 69 gateway/rehearsal tests passed and Ruff passed.
Coverage includes bounded 429/500/502/503/504 retries, Retry-After limits,
transport recovery/exhaustion, permanent failures, exact compatibility fallback,
reasoning-only/empty/length-finished/invalid judgments, fair queues, grants,
read-only recovery, bounded concurrent receipt collection, identity/credential
checks and strict classifier failure preservation. Faults were injected into
mock transports, not production. Two dependency deprecation warnings remain.
The separately pinned Pipecat reference has ten framework contract tests and
real two-voice evidence plus a clearly labeled prerecorded offline fallback;
it does not claim browser WebRTC transport or a NeMo Voice Agent application.
