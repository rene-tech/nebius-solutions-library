# Medical report quality experiment — 2026-09-16

**Reports were generated successfully, but the tested pipeline is not suitable
for unchecked final medical documentation.** The drafts preserve useful history
and plans. They also contain unsupported additions and lost facts. Improving
ASR alone will not fix errors introduced by the downstream report generator.

This is an engineering, text-to-text evaluation of public teaching/research
material, **not clinician adjudication, a listening review or clinical approval**.
No production model, cluster, prompt, endpoint or user grant was changed.

## Read the actual full reports

These are the original, uncorrected model outputs, deliberately retained as
evidence. They must not be treated as medical advice or accepted patient records.

| Audio / language | English-only ASR → report | Multilingual ASR → report | Human-reference control |
| --- | --- | --- | --- |
| Gastrointestinal consultation / English | [Draft](report-quality-r1/generation/en-01-en.md) | [Draft](report-quality-r1/generation/en-01-multi.md) | [Control](report-quality-r1/generation/en-01-reference.md) |
| Eczema consultation / English | [Draft](report-quality-r1/generation/en-02-en.md) | [Draft](report-quality-r1/generation/en-02-multi.md) | [Control](report-quality-r1/generation/en-02-reference.md) |
| Herzrasen / German | Not applicable | [Entwurf](report-quality-r1/generation/de-herzrasen-multi.md) | No human reference |
| Grippaler Infekt / German | Not applicable | [Entwurf](report-quality-r1/generation/de-grippaler-infekt-multi.md) | No human reference |
| Polyarthritis / German | Not applicable | [Entwurf](report-quality-r1/generation/de-polyarthritis-multi.md) | No human reference |

English-only Nemotron is not evaluated as a German recognizer. Reports remain
in the consultation language; this is not a translation test.

## What worked, and what did not

Across both English variants, the main gastrointestinal history survived:
three days of watery diarrhea, 6–7 stools/day, left-sided cramping, resolved
initial vomiting, ability to take fluids and the conservative management plan.
Both eczema drafts preserved the main symptoms, prior treatment attempts and
follow-up. They recovered fexofenadine from corrupted ASR spellings correctly in
these particular outputs; that does not establish reliable drug-name recovery.

Agent inspection found at least one unsupported or incorrect statement in
**each of the four English ASR-based full reports**. This is a case-level
observation on two consultations, not a population error rate. All three German
full drafts also contain fidelity/uncertainty issues when compared with their
ASR input; their accuracy against the actual speech remains unverified.

| Case | Directly checked example | Attribution |
| --- | --- | --- |
| English GI / English ASR | Adds absence of severe infection/dehydration requiring intervention; the conversation does not establish that exclusion. | Report generator |
| English GI / multilingual ASR | Adds “No travel history”; travel was not discussed. | Report generator |
| English eczema / English ASR | Adds an allergic-contact-dermatitis differential and an unsupported explanation that no examination occurred because consultation was remote. | Report generator |
| English eczema / multilingual ASR | Claims differential exploration was limited by a remote consultation; that setting/reason is not established. | Report generator |
| German Herzrasen | Changes prior depression “a few years ago” into “six years ago.” | Report generator; verified against ASR only |
| German Infekt | Turns a conditional, pre-examination viral assessment into exclusion of bacterial superinfection. Also changes a six-month project already halfway through into six months remaining. | Report generator; verified against ASR only |
| German Polyarthritis | Reconstructs the great-aunt’s specific rheumatoid-arthritis diagnosis from corrupted words for rheumatism without retaining uncertainty. | ASR ambiguity plus report inference; no human transcript |
| German excerpt 338 | Birth year 1963 disappears from ASR and is absent from the report. | ASR information loss |
| German excerpt 526 | Oligurie becomes Aligorie in ASR and disappears from the generated note; the reference-input control retains it. | ASR corruption followed by summarizer omission |

The German blood-pressure excerpt [1090](report-quality-r1/generation/multimed-1090-multi.md)
preserved 130/90, the clinician's description of it and the planned blood work.
The empty ASR challenge [193](report-quality-r1/generation/multimed-0193-multi.md)
produced an explicit no-content response rather than an invented consultation.
Nevertheless, no-content is a missed transcription, not successful documentation.
Another excerpt [939](report-quality-r1/generation/multimed-0939-multi.md) retained
largely unintelligible ASR wording: the LLM cannot reliably recover information
that was lost by recognition.

The [agent-checked findings](report-quality-r1/agent-checked-findings.json)
retain 12 illustrative findings with actual report quotes and source excerpts.
These examples were checked against the saved texts; they are not an exhaustive
or clinician-scored severity inventory. Correction time was not measured.

## Controlled experiment and sample limits

- ASR inputs: retained complete-file outputs in `medical-r1-summary.json`,
  already measured on H100s; no recognition parameters were changed or rerun.
- Speech models: `nvidia/nemotron-speech-streaming-en-0.6b` and
  `nvidia/nemotron-3.5-asr-streaming-0.6b`; exact speech revisions/runtime and
  benchmark conditions remain in [medical quality](MEDICAL-QUALITY.md) and
  [German benchmark](GERMAN-MULTIMED.md).
- One fixed downstream generator: `Qwen/Qwen3-235B-A22B-Instruct-2507`, actual
  shared Nebius Token Factory endpoint `https://api.tokenfactory.nebius.com/v1`,
  temperature 0, max output 6,000 tokens, JSON response. Same system prompt for
  all inputs; language and consultation/excerpt type are supplied separately.
  It is a hosted model alias, not a pinned immutable weights revision.
- Generation received **only its own transcript**, language and input type.
  No human reference, case diagnosis label, clinician note, other variant's
  report, or evaluation finding was supplied to an ASR-report generation call.
- English: two complete PriMock57 role-play consultations, four ASR reports and
  two control reports generated from human TextGrids. Controls use the same
  onset-ordered flattened reference used for WER: this is not a gold diarized
  report baseline. Overlap and some source ambiguity remain.
- German full consultations: three complete HHU teaching recordings. Clearly
  introductory narrator prefixes were excluded at recorded transcript-text
  markers, so their names and diagnoses were not imported into patient reports.
  This is conservative text selection, not listening-verified segmentation.
  No human transcript or gold German Arztbrief exists for these recordings.
- German referenced supplement: **30 systematic clips** at indices
  `floor(i*1090/29)` for i=0..29, plus the separately labeled missing-output case
  193. Each clip gets its own reference-input and ASR-input note: 62 outputs.
  No unrelated excerpts were combined into a synthetic patient consultation.
  The corpus includes nonmedical teaching/general speech; these 31 items are
  **not 31 complete medical encounters** or a homogeneous clinical benchmark.
- Total: **71/71 generation requests and 36/36 reviewer requests completed**;
  these are transport/JSON completion counts, not quality passes. Only 35/36
  reviewer outputs matched the expected report-list structure; the malformed
  `multimed-0112` response is preserved and flagged, not scored as a pass. No truncated
  response was accepted. There was one generation per input, not repeatability
  or model-ranking evidence. No failed call was omitted or retried.

Generation used 88,995 reported tokens; the automated review used 152,712.
Requests ran with bounded concurrency four. Full-consultation generation calls
took 33.74–54.83 seconds each, excluding speech recognition and queueing in the
Scientific AI platform. These are shared-API observations, not platform SLAs.

## The reference notes are not infallible either

The clinician-written English notes contain discrepancies with the actual
transcripts. Examples: GI note says bilious vomit while dialogue says normal
food colour; note broadens household illness beyond the one child described;
eczema note specifies Betnovate BD and Cetraben QDS even though those names/doses
were not spoken. Follow-up timing also differs. Patient and clinician disagree
within the eczema transcript about rash distribution.

We therefore used transcript evidence as the primary reference and documented
note conflicts. We did not count an invented drug/dose as correct just because
it appeared in a reference note, or penalize a draft for not reproducing it.

## Automated reviewer limitation

`openai/gpt-oss-120b` via the same hosted provider reviewed each case in a separate
context, with temperature 0, medium reasoning, max output 14,000 tokens. Its raw
responses are retained under `report-quality-r1/review/` for reproducibility,
**not accepted as adjudicated scores**.

This reviewer missed the GI reports' unsupported additions and sometimes
confused ASR text or structured evidence fields with final report text. Of its
111 suggested findings in the 35 structurally usable responses, 34 had nonliteral report quotes and nine had nonliteral
reference quotes. Nonliteral quotations are not automatically false findings,
but these checks demonstrate that its assertions require verification. For
example it claimed the final eczema reports used corrupted fexofenadine names,
while those reports actually used the corrected name. It also missed the
six-years addition in the German report. No automated quality percentage,
clinical pass rate or global severity score is reported from this judge.

The conclusion above rests on directly checked source/output discrepancies,
not the automated reviewer's reassuring assessments or invented quotations.

## Recommendation

Continue as a draft-generation evaluation, not an unattended medical-record
workflow. The next technical experiment should compare an explicitly
evidence-extractive report structure with this free-text baseline, preserving
missing/uncertain facts and separating stated assessment from inference. Do not
silently correct the saved baseline outputs. Drug names, quantities, negations,
speaker assignment and planned-versus-completed actions deserve targeted checks.
Clinical review and measured editing effort are still needed before deciding
whether this actually saves clinicians time. This review requirement is also
consistent with [NHS England's clinician guidance](https://digital.nhs.uk/data-and-information/information-governance/guidance/using-ai-enabled-ambient-scribing-products-in-health-and-care-settings/guidance-for-health-and-care-professionals).

Neither an improved prompt nor a successful automated review alone would
establish clinical readiness. No such workflow or new policy was deployed here.

## Reproduction and attribution

Verification: six report-experiment tests pass, including reference-blind
request payloads, all 71 stored reports/hashes, literal example quotations and
preservation of the malformed reviewer response. The combined acceptance-helper
suite passes 22 tests in the existing control-plane virtual environment with
its source/tests directories on `PYTHONPATH`. A first system-Python attempt to
collect that broader suite lacked its existing `test_model_deployment` import
path; it was rerun with the correct environment, not fixed by changing product
code. Pytest emitted existing temporary-directory permission-cleanup warnings;
those unrelated old directories were not modified manually. Document links
resolve and `git diff --check` passes.

```bash
cd k8s-inference/acceptance/nemotron-speech-20260916
python3 -m pytest -q test_report_quality.py
python3 report_quality.py prepare --output report-quality-new
python3 report_quality.py generation --output report-quality-new
python3 report_quality.py review --output report-quality-new
python3 summarize_report_quality.py report-quality-new
```

Uses Python standard library plus pytest for tests and an existing Token Factory
credential from its documented environment variables or protected key file.
Never embed a key in the command line or evidence. All request bodies (without
authorization headers), input hashes, raw responses, token usage, timestamps,
prompts and Markdown reports are retained in `report-quality-r1/`. Cached
receipts are reused only for exactly matching request hashes; use a new output
directory to repeat the experiment. The scripts do not deploy or alter services.

Input attribution: Babylon Health / PriMock57, CC BY 4.0, revision
`cd2ac707ad03cb4d2531f4ec6b90c659bf4357c5`; HHU recordings, CC BY 3.0 DE
(Olaf Reddemann, Christian Cujovic, Marlon Jarek; and Prof. Dr. Jürgen in der
Schmitten, Susan Fararuni, Marlon Jarek for Polyarthritis); MultiMed German/test,
dataset-card MIT, revision `459d0ab6db332904f9d7b76a8baabf3333958fa8`.
Full URLs, recording-level credits and source licenses are in the original
asset README `/home/tux/demo-assets/medical-speech-en-de-20260916/README.md` and
the linked prior benchmark reports. These are generated adaptations, not
original clinician-authored notes. No source audio is added to the repository.
