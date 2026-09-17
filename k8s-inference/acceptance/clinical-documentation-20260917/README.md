# Consultation-audio documentation workflow

Status: **implemented and tested for supervised draft evaluation; not signed off
for unattended clinical use or a separate LibreChat deployment**. Twenty-two
deterministic tests pass. Five complete public audio workflows pass transport,
output and source-span integrity checks; independent fidelity testing still
finds omissions, false-negative review decisions and imperfect question proposals.

This feature packages consultation audio → unchanged transcript → structured
facts → German Arztbrief/English report draft → separate suggested questions.
It is a reusable client/agent skill, **not a new server MCP endpoint, a new
frontend, an EHR integration or a clinically validated product**.

Source: `rene-tech/nebius-solutions-library`, integration branch
`agent/fs2-mindeval-workshop-r20260916`, initial parent
`977a945ba9c168afc76a75fff9f03d1011c9d0e5`. Per-run manifests pin the actual
Python source digests, prompts, provider/model, input hashes and invocation IDs.
Task Deck: `fs2-clinical-documentation-skill-r20260917`, NIM Fast Start Platform.

## Implementation and use

- Skill: `../../integrations/librechat/skills/clinical-documentation/SKILL.md`.
- Portable helper: `scripts/clinical_report.py` inside that skill directory.
- Ordinary platform APIs provide artifact uploads, durable ASR operations and
  result retrieval. No administrator key is used by the helper.
- English ASR: `nemotron-speech-en-0-6b`; German:
  `nemotron-speech-multilingual-0-6b`.
- Recommended report profile: `Qwen/Qwen3-235B-A22B-Instruct-2507` on
  `https://api.tokenfactory.nebius.com/v1`, with an explicitly configured
  executor-side provider credential. Catalog discovery precedes inference.
- No model, cluster, API quota, user-access policy or live UI changes.

Configure an ordinary platform key via `FS2_API_KEY_FILE`/`FS2_API_KEY`, and the
report provider key via `CLINICAL_REPORT_API_KEY_FILE`/`CLINICAL_REPORT_API_KEY`.
Inject credentials in the trusted executor, not in agent-visible arguments.
Non-secret defaults may be set once in `CLINICAL_REPORT_MODEL` and
`CLINICAL_REPORT_BASE_URL`. Then run:

```bash
uv run k8s-inference/integrations/librechat/skills/clinical-documentation/scripts/clinical_report.py \
  --audio /user-workspace/consultation.wav --language de \
  --report-provider https://api.tokenfactory.nebius.com/v1 \
  --report-model Qwen/Qwen3-235B-A22B-Instruct-2507 \
  --output /user-workspace/consultation-report
```

The output directory contains `transcript.txt`, the raw ASR `transcript.json`,
`report.md`, `follow-up.md`, `document.json`, `review.md`, `review.json`, `run.json` and call
receipts. Reuse exactly that directory/configuration to resume an interrupted
operation. Changing a model does not require another transcription: supply
`--transcript old-run/transcript.txt` and a new output directory.

## Why this is more than a summarization prompt

The helper processes actual uploaded bytes, preserves the original transcript,
verifies externalized result artifacts by SHA-256/length, and retains operation
IDs. It does not expand audio into model tool arguments. Empty inputs and
truncated model responses fail visibly.

Extraction uses overlapping chunks and numbered source segments. Constrained
JSON decoding requires fields and valid source references. The helper constructs
literal quotes and offsets; it does not trust model-generated offsets. Each
fact is checked **in isolation**, with only its own evidence. A rejected fact
gets one explicit citation-location attempt and isolated recheck. Original
references and repair reasons remain available. Persistent unsupported
candidates are excluded and surfaced in the review output. Report text is
rendered from the resulting facts, not a second free-form narrative that can
introduce additional findings. Suggested questions never enter the report body.

These mechanisms improve traceability but do **not** prove semantic correctness
or completeness. The extractor and verifier can share a model and share errors.
A literal source link is not equivalent to a medically correct interpretation.

## Test cohorts and negative evidence

| Cohort | Meaning |
| --- | --- |
| `live-r1` | First full English/German public audio attempts with cluster Qwen-8B; failed report/schema handling. Includes actual cold activation. |
| `nemotron-r1` | NVIDIA Nano default reasoning exhausted 7,000 output tokens; no partial report accepted. |
| `nemotron-r2` | NVIDIA Nano non-thinking; schema/question failures and incomplete extraction. |
| `qwen235-r1` | Larger model baseline; literal quote copying caused discarded facts. |
| `qwen235-r2` | Numbered sources without constrained JSON; missing-field failures remained. |
| `live-r2`, `nemotron-r3`, `qwen235-r3` | Schema-constrained comparison on identical retained transcripts; small models dropped important facts and used poor references. Larger model was more complete. Not a blinded clinical benchmark. |
| `full-r1` | All five complete audio recordings succeeded technically; subsequent independent test exposed cross-fact evidence leakage in batch review. This is not the final traceability acceptance. |
| `full-r2` | Full-audio cohort using per-fact isolated review and bounded citation repair. See `verification.json` and independent-forward-test findings. |
| `final-projection` | No new inference: final deterministic handling moves unclear-source claims into a readable review queue, fixes misleading empty-section text, and retains every candidate in report or review. Original live evidence is untouched. |

Measured `full-r2` wall time includes upload, ASR, report extraction, isolated
review and question generation. These are five observed executions, not a
latency SLA or a clinical accuracy score:

| Recording | Complete audio | Workflow wall time |
| --- | ---: | ---: |
| English gastrointestinal consultation | 457.92 s | 87.12 s |
| German palpitations | 421.86 s | 95.32 s |
| English eczema | 559.20 s | 107.81 s |
| German cold symptoms | 629.26 s | 131.08 s |
| German polyarthritis discussion | 438.39 s | 94.02 s |

`verification.json` checks unchanged complete ASR text/duration, 167 exact
source spans, completed provider responses and temporary-key revocation.
It does **not** certify that the cited text entails every statement. The final
postprocessing-only change is covered by deterministic regression tests and
`project_review.py`; the retained timings are not relabeled as a new live run.

An independent agent used its own synthetic German consultation and recorded
expectations before seeing results. The first attempt exposed missing fields,
the second exposed wrong citations falsely approved using other facts' evidence.
These failures drove actual fixes, not relaxed assertions. Saved independent
test evidence identifies the exact implementation tested on each attempt.
Read `independent/v1/assessment.md`, `independent/v2/assessment.md`, and
`independent/v3/assessment.md`. V3 fixed cross-fact input leakage, but still
incorrectly rejected a recorded pulse and follow-up arrangement and proposed
redundant/speculative questions. The final postprocessing fix puts unclear
source claims into `review.md` rather than treating them as supported facts and
never infers absence from an empty assigned section. It does not claim to solve
all model interpretation errors. No physician has adjudicated these outputs.

The first acceptance harness used an incorrect revocation URL. Its original
negative receipts remain intact; `cleanup.json` records successful DELETE
revocations of exactly those task-created keys. The corrected harness revokes
its temporary ordinary key in `finally` and reports cleanup status.

## Reproduce and verify

All raw live/provider cohorts, including request/response/state receipts, are
packaged without credentials in `call-receipts.tar.gz` to avoid hundreds of
tiny versioned files. Extract from
this directory with `tar -xzf call-receipts.tar.gz` before running verification.
Final readable transcripts/reports, summaries and independent negative findings
stay directly versioned. Local raw receipts were retained, not deleted.

```bash
uv run --with pytest --with httpx pytest -q \
  k8s-inference/acceptance/clinical-documentation-20260917/test_workflow.py

python3 k8s-inference/acceptance/clinical-documentation-20260917/verify_results.py \
  k8s-inference/acceptance/clinical-documentation-20260917/full-r2 \
  --assets /home/tux/demo-assets/medical-speech-en-de-20260916
```

`live_acceptance.py --help` describes the operator-only full-audio runner.
It gets the administrator bootstrap only to create/revoke temporary ordinary
test keys; actual workflow requests use those ordinary keys. Provider secrets
are never retained. This distinction does not make the customer helper an
administrator tool.

Fixtures: two full [PriMock57](https://github.com/babylonhealth/primock57)
role-play consultations (CC BY 4.0, Babylon Health,
revision `cd2ac707ad03cb4d2531f4ec6b90c659bf4357c5`) and three full HHU teaching
consultations (CC BY 3.0 DE, Heinrich-Heine-Universität Düsseldorf). Source links,
individual creator attribution, transformations and licenses are recorded in
`/home/tux/demo-assets/medical-speech-en-de-20260916/README.md`. Large audio is
not committed. German fixtures include teaching narration and have no verified
full human transcript: assess faithfulness to ASR separately from correctness
against the recording. English human transcripts are primary over clinician
notes, which themselves contain unspoken additions.

HHU sources/credits: [WWSZ/palpitations](https://media.hhu.de/video/good-practice-wwsz-technik/5b4e0fb9a15939a76a025c06d3fd505f)
and [cold-symptom history](https://media.hhu.de/video/good-practice-wwsz-technik-und-anamnese-bei-grippalem-infekt/1405493ccdbe3188d779c4e2d0bebb37)
by Olaf Reddemann, Christian Cujovic and Marlon Jarek;
[SPIKES/polyarthritis](https://media.hhu.de/video/good-practice-schlechte-nachrichten-berbringen-spikes-modell/5fa45b3f9155318d841e4af94dc07398)
by Prof. Dr. Jürgen in der Schmitten, Susan Fararuni and Marlon Jarek.
Audio was extracted/downmixed/resampled to 16 kHz without cuts; ASR transcripts
and report drafts are machine-generated adaptations, not author-approved notes.

## Deployment boundary and remaining limitations

The skill is shipped in the LibreChat integration bundle. The helper can run
here or in a configured per-user executor. **This is not evidence of deployment
or end-to-end testing in a separate LibreChat instance.** Its operator must
mount the skill/scripts, install the pinned Python dependency, connect the
attachment/file bridge, provide per-user platform authentication, configure
the report backend, and expose downloadable outputs. See the integration
`HANDOVER.md`. No workshop webpage has been reintroduced.

ASR usage is recorded as platform operations. Direct Token Factory report calls
retain usage in workflow receipts, but are not yet attributed/billed as platform
Apps. Do not tell customers one platform token alone enables the recommended
profile before that server-side integration exists.

This version supports uploaded recordings and transcripts, not live microphone
capture, speaker identity verification, signed letters, automated EHR writes,
or complete discharge documentation. Two-hour ASR transport limits are not a
claim that every two-hour multi-speaker consultation has been quality-tested.
Long transcripts are chunked; question synthesis explicitly reports omission
if the complete fact set exceeds its configured context budget. No diagnosis,
drug normalization, dose, clinically important absence or follow-up suggestion
should be used without professional review. Suggestions are not a validated
specialty checklist or proof that the clinician forgot a question.

## Relevant upstream work

[NVIDIA Ambient Provider](https://github.com/NVIDIA-AI-Blueprints/ambient-provider)
demonstrates consultation transcription, clinical note templates and source
citations. This solution reuses that workflow pattern, not its separate UI/auth
stack or a copied implementation.
[NVIDIA Digital Health Skills](https://github.com/NVIDIA/digital-health-skills)
focus on clinical-ASR data/adaptation/evaluation; they are not a prevalidated
German Arztbrief generator. No MedGemma or new GPU model was deployed here.
The tested NVIDIA Nano non-thinking setting follows
[NVIDIA's documented per-request control](https://github.com/NVIDIA/xr-ai/blob/main/ai-services/llm/nemotron3_nano/README.md).
