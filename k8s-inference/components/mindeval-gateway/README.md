# MindEval public Token Factory gateway

This is the server-side inference port of MindEval v1 for the Scientific AI
workshop. It executes the original patient (`v0_2`), clinician (`v0_1`) and judge
(`v0_1`) templates from SWORDHealth/mind-eval revision
`1c17f9e66c092d9480c4bda5a2bebc80b7f84961`. Template and data SHA-256 digests are
in `src/fs2_mindeval/assets/provenance.json`. `scripts/import_upstream.py` extracts
the exact string constants without executing upstream Python. The original
unbounded LiteLLM loop and default-to-three parser are not imported or executed;
`adapter.py` replaces that inference and parsing path.

`patches/mindeval-token-factory.patch` also modifies the original upstream
inference, interaction and judgment scripts directly. Apply it to a checkout of
the pinned revision with `git apply --check` followed by `git apply`, and install
this component package. Set `MINDEVAL_GATEWAY_URL` and a normal
`SCIENTIFIC_AI_TOKEN`; the patched scripts never receive a Token Factory key.
Pass the same `run_id` in patient/clinician/judge API parameters, exact catalog
model IDs for patient/clinician, at most 20 original profiles and at most five
workers. For a non-prefix profile subset, include its original `profile_id` on
each JSONL row. The comparison-wide `comparison_models` list belongs in clinician
API parameters. The judge ignores caller model overrides and uses the registered
fixed judge. The patch preserves the original templates and records full
completion usage/finish metadata through gateway telemetry.

Only `https://api.tokenfactory.nebius.com/v1` is supported. Catalog discovery
excludes dedicated endpoints and non-chat modalities. Each run registration
persists exact public metadata, pricing, provider limits, discovery time and
catalog digest alongside prompt/data provenance. Replaying the same registration
returns its original snapshot; changes return 409. Registration is mandatory,
immutable, and limited to 20 distinct upstream profiles. There are 50 available
profiles, with IDs `profile-000` through `profile-049`.

## API contract

All `/v1/mindeval/*` requests carry the caller's ordinary platform bearer token.
The gateway revalidates it on each request through `/internal/ext-authz`, reads
the verified tenant/principal/token, JSON `x-fs2-scopes` and `x-fs2-models` headers,
and integer `x-fs2-max-concurrency`. Client identity headers are never trusted.
`inference.invoke` and a `mindeval` workflow grant (or matching model grant / `*`)
are required. Raw Token Factory keys exist only in the gateway secret mount.

| Endpoint | Response / request |
| --- | --- |
| `GET /v1/mindeval/catalog` | `{data:[provider_metadata + family + patient_eligible + clinician_eligible], judge_model,judge_family,provenance,limits}` |
| `GET /v1/mindeval/profiles` | `{data:[{id,name,age,depressive_symptoms,anxious_symptoms}],upstream_revision,...provenance}` |
| `GET /v1/mindeval/profiles/{id}` | `{id,profile,patient_system_prompt,clinician_system_prompt,upstream_revision,patient_template_version,clinician_template_version}` |
| `POST /v1/mindeval/runs/{id}/register` | Send `{profile_ids,patient_model,clinician_models}`; returns immutable `{run_id,catalog_snapshot,provenance,judge_model,judge_family,profile_ids,patient_model,clinician_models}` |
| `POST /v1/mindeval/completions` | Send `{run_id,profile_id,role:"patient"\|"clinician",model,messages,max_completion_tokens?,temperature?}` |
| `POST /v1/mindeval/judgments` | Send `{run_id,profile_id,clinician_model,interaction:[{role,content}],max_completion_tokens?}`; builds the pinned rubric server-side |
| `GET /v1/mindeval/runs/{id}/events?after=0` | `{data:[{id,at,profile_id,role,model,status,usage,queue_ms,latency_ms,retries,retry_codes,request_id,error_code?}]}` |
| `GET /v1/mindeval/queue` | Aggregate active/queued counts and workers-per-team |

Completion returns `{model,provider_model,provider_request_id,content,reasoning,
finish_reason,usage,telemetry:{queue_ms,latency_ms,retries,retry_codes,
token_parameter,request_id}}`. Judgment adds `judgment`, an object mapping all
five exact upstream criterion names to finite scores between 1 and 6, and
`overall_score`. Fractional scores are intentional. Provider reasoning is kept
separate from the visible answer and omitted from telemetry logs.

Errors have `{error:{code,message,telemetry}}` with an appropriate HTTP status.
Empty/reasoning-only answers, length finishes and invalid judgments fail visibly;
none become ellipses, a passing result or an invented score. Caller retries remain
explicit workflow attempts. The adapter retries only 429, transient 5xx and
transport errors, at most four total attempts with bounded backoff. A 400 changes
`max_completion_tokens` to `max_tokens` only when the provider explicitly identifies
the former parameter as unsupported. Unrelated 400s fail immediately. Invalid
provider JSON, malformed completion structures and unknown finish reasons fail.

## Original interaction sequence

Initialize clinician history with its system prompt and `user: Hello`.
Initialize patient history with its system prompt and `assistant: Hello`.
For each full round, generate the clinician then patient, append each answer to
both histories with the corresponding reversed role. The transcript includes
the initial `user: Hello` and both messages from every round. No clinical scores
are generated until a completed transcript is judged. User interventions should
be stored as separate provenance by the workshop service.

The rehearsed patient is Qwen3-30B-A3B-Instruct-2507. Gemma3-27B rejected the
canonical patient history with HTTP 400 because it begins with an assistant
message; the gateway therefore does not offer Gemma as a patient. Available
clinicians are the explicitly contract-tested Qwen30B/235B, Nemotron Nano30B,
GPT-OSS120B, GLM5.1 and DeepSeekV4Pro. The catalog still exposes exact public model
metadata while eligibility flags constrain workshop selection. The calibrated
judge is fixed in the image's `judge_selection.json`; any clinician family
conflict is rejected across the entire comparison suite. Qwen distillations
count as Qwen. Hermes counts as Llama.

## Scheduling and persistence

One gateway replica and one Uvicorn worker are required. The Kubernetes strategy
is `Recreate`, with SQLite/WAL on an RWO PVC. This is an explicit single-scheduler
deployment, not a distributed or HA queue. Team identity is `(tenant_id,
principal_id)` so ten teams sharing one tenant remain separate. Round-robin
dispatch admits at most five concurrent upstream calls per team, further bounded
by the verified token concurrency policy, and 50 globally. Per-model provider
RPM/TPM sliding windows are shared globally across all teams and retries. A
blocked model does not block that team's other models. Reservations use UTF-8
bytes plus protocol overhead as a conservative token upper bound and settle to
actual provider usage. Burst judge work waits in the fair queue.

Run registrations, catalog snapshots and telemetry survive restart. Queued calls
are deliberately owned by the workshop's durable execution/lease machinery; it
must resubmit after interruption. In-flight provider calls may have incurred
usage before a lost response; there is no false claim of exactly-once billing.
SQLite stores no bearer credentials or transcript content. The workshop is
responsible for storing transcript/results and exposing these telemetry events
in Scientific AI run observability.

## Verification and deployment

```
uv venv .venv
uv pip install --python .venv/bin/python -e '.[test]'
.venv/bin/pytest -q
.venv/bin/ruff check src tests
.venv/bin/fs2-mindeval-rehearse contracts --output evidence/contracts
.venv/bin/fs2-mindeval-rehearse calibrate --output evidence/calibration
```

The rehearsal reads the existing protected Token Factory credential file without
printing it. Calibration uses the published 60 human-annotated conversations,
excludes all profiles appearing in the judge's in-context examples and groups
remaining profiles across clinician models into deterministic selection and
validation halves. The selection rule is minimum selection MAE among candidates
with valid judgments for every selection example. Validation is reported once,
without fitting a score offset. Axis mapping follows Appendix C's 2+2+2+2+1
human sub-axes. Out-of-scale zero ratings are missing; undocumented `criterion11`
is excluded. Mapping assumptions, per-axis bias and human agreement limits are
reported with the evidence. These scores are research workshop results, not
clinical validation or a claim of therapeutic safety.

Build `Dockerfile` and resolve the image to a digest. Production integration uses
`../../charts/addons/mindeval-workshop` and the optional
`../../modules/mindeval-workshop` Terraform module. Do not also apply the
standalone `deploy/preview.yaml` to resources owned by Helm. The task-owned
deployment is in `fs2-system` with service
`http://fs2-mindeval-gateway.fs2-system.svc:8080`. Supply Secret
`mindeval-token-factory` key `api-key` from the protected credential file. The
existing control plane must expose the extended verified policy headers, and its
NetworkPolicy must allow gateway pods to reach the authorization endpoint. No
shared control-plane/admin/website deployment is performed by this component.

The separate public workshop acceptance runner uses ten ordinary platform PATs,
not the upstream Token Factory credential. A protected JSON file contains
`{"teams":[{"label":"team-01","token":"..."}, ...],"denied_token":"..."}`.
The ten principals must share one tenant and each permit five workers; the extra
denied PAT must lack the MindEval model grant. Never place this file in evidence.

```
.venv/bin/python scripts/rehearse_workshop.py \
  --base-url https://YOUR_WORKSHOP_ORIGIN \
  --keys-file /protected/rehearsal-keys.json \
  --output evidence/public-rehearsal --run-label unique-rehearsal-label \
  --gateway-image REGISTRY/gateway@sha256:DIGEST \
  --workshop-image REGISTRY/workshop@sha256:DIGEST
```

Run only after the coordinator confirms deployment readiness and image stability.
Two repetitions create 60 durable jobs each (ten teams, six clinicians), plus one
controlled intervention run. The same label replays idempotently; a new label
creates new jobs. The runner tests registration limits without creating 400
inference jobs, owner isolation, grant denial, idempotent creation, fair progress,
five-worker limits, reconnect, pause/takeover/nudge/resume/abort, exact five-axis
judgments, token/queue/latency telemetry, and explicit classifier coverage. It
saves public reports and gateway events, fails on any failed/invalid run, and
does not silently retry failed jobs. `--insecure` is an explicit exception only
for the workshop's self-signed rehearsal certificate. Image digest attestation
remains the coordinator's cluster check, not a claim made by the public API.

For the six-model full-length cohort, add `--full-dialogue-turns 10
--full-dialogue-all-clinicians --repetitions 0`. This creates six canonical jobs
on one common profile, each with 21 messages and eleven patient-prefix classifier
assessments. The fixed judge excludes all six comparison families. Every job's
full report is retained even if a cohort member fails. Using the same command
without `--repetitions 0` first runs the default two ten-team repetitions.
