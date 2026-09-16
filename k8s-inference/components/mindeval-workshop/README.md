# MindEval workshop

A resumable, multi-team workshop on the Scientific AI platform. The browser UI
is `/workshop`; its API is `/v1/workshop`. Existing platform API keys authenticate
both. The upstream Token Factory credential never goes to an attendee/browser.

This is a research demonstration with simulated patient profiles, not a clinical
product or a validated measure of therapeutic safety. MindEval's fixed judge and
MindGuard's observational classifications have different purposes. A missing
classification is **unavailable**, never a safe result.

## Participant workflow

See [the attendee guide](docs/attendee-quickstart.md) for controls, comparison and
exports, and [the operator runbook](docs/operator-runbook.md) for deployment,
recovery and event acceptance. The UI provides two deliberately separate modes:

- **Text benchmark:** original MindEval prompts and alternating role-relative
  histories. Only completed, untouched text runs enter the comparison table.
- **Spoken experience:** clinician/patient text is synthesized using distinct
  Magpie voices, transcribed by centralized speech, and the heard text becomes
  the next role's input. Original text, audio and ASR differences remain in the
  report; scores are not pooled with the text benchmark.

Pause, nudge, typed/microphone takeover, return-to-model and abort are recorded
as interventions. Work is fenced by a run version and worker lease. A late model
response cannot overwrite human intervention. Disconnecting the browser does
not discard server-side runs. A process loss is an explicit interruption, not a
silent replay of possibly billed provider work.

## Architecture and persistence

Two CPU API/worker replicas use managed PostgreSQL and a separate additive
`fs2_workshop` schema. A shared database claim admits at most five workers per
tenant/principal, further bounded by the verified API-key concurrency policy.
The gateway separately accounts for provider RPM/TPM and dispatches fairly
across teams. It is a **single scheduler with a persistent SQLite volume**, not
an HA distributed queue; see [its documentation](../mindeval-gateway/README.md).

The workshop stores run configuration, original profiles/prompts, registration
and catalog snapshots, transcripts, per-turn timings, token metadata, generated
WAVs, judgments, classifications and append-only run events. Active-run API keys
are encrypted with a deployment-specific Fernet key and removed on terminal
completion. Neither provider nor attendee credentials belong in evidence or
Helm values. Back up the database and its encryption key together; losing the
key prevents pending work from authenticating.

The interface authenticates with a key held only in tab memory. Reports and audio
are accessible only to the authenticated tenant/principal that owns the run.
No separate "scientific access" identity scheme is introduced.

## HTTP API

All paths below are relative to `/v1/workshop` and require a normal Bearer key.

| Path | Purpose |
| --- | --- |
| `GET /catalog` | Eligible clinician/patient catalog, fixed judge, profiles, verified team identity and limits. |
| `POST /runs` | Create the profile × clinician matrix. Requires `Idempotency-Key`; replaying the same key/body returns the original runs. |
| `GET /runs` | List the caller's runs. |
| `GET /runs/{id}` | Current state, complete transcript, provenance and results. |
| `POST /runs/{id}/interventions` | `pause`, `nudge`, `takeover`, `say`, `resume` or `abort`; select `patient`/`clinician`. |
| `GET /runs/{id}/events?after=0` | Ordered persisted run events. |
| `GET /runs/{id}/report` | Download the JSON evidence report. |
| `GET /runs/{id}/audio/{turn_index}` | Download a retained WAV for a spoken/human turn. |
| `WS /runs/{id}/microphone` | Authenticated PCM16LE/16kHz/mono microphone takeover; authentication is in the first message, never a URL token. |

Example creation body (exact model IDs come from `/catalog`):

```json
{
  "profile_ids": ["profile-001"],
  "patient_model": "<eligible patient model ID>",
  "clinician_models": ["<eligible clinician model ID>"],
  "mode": "canonical",
  "max_turns": 10,
  "max_completion_tokens": 4096
}
```

At most 20 distinct profiles and eight clinicians are accepted in a request.
The number of **rounds** is clinician+patient pairs; the transcript also includes
the original seed greeting. A short diagnostic run is not a full acceptance
cohort. Invalid, truncated or reasoning-only completions stop visibly rather
than silently producing a score.

## Deploy and test

Use the [add-on Helm chart](../../charts/addons/mindeval-workshop/README.md) or
the [Terraform module](../../modules/mindeval-workshop/README.md). Existing
database, provider and encryption-key Secrets are prerequisites; the module
does not create cloud resources or put secret bytes into Terraform state.
The migration hook touches only `fs2_workshop`, not platform migration history.

```bash
uv sync --frozen
uv run pytest -q
uv run ruff check src tests
```

Database tests require a **dedicated disposable PostgreSQL database** on
`127.0.0.1:15496` by default. They truncate their test schema. Never set
`WORKSHOP_TEST_DATABASE_URL` to a production/platform database. Tests cover
claim concurrency, idempotency, owner isolation, interventions, lease recovery,
atomic audio/transcript persistence and asynchronous speech completion.

Public acceptance is driven by
`../mindeval-gateway/scripts/rehearse_workshop.py`, using ten restricted test
principals rather than an administrator/provider key. Preserve failed and
diagnostic evidence; do not relabel it as a passing release rehearsal.
