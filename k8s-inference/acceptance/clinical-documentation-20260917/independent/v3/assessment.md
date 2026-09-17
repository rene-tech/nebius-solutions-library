# Independent forward test: v3, same synthetic German consultation

**Execution passed; citation handling improved materially, but the resulting letter and questions still have important fidelity/usability failures.** This is a source-to-document comparison on one synthetic case, not independent clinical validation.

## Scope and provenance

One newly authorized attempt used the unchanged synthetic transcript and original preregistered expectations. Exact provider/model: `https://api.tokenfactory.nebius.com/v1`, `Qwen/Qwen3-235B-A22B-Instruct-2507`. No retry, no repository changes, no deployment/resource/policy changes. A fresh ordinary scoped Rene key was revoked in `finally` using DELETE, HTTP 200. Provider credentials remained in the existing protected file.

Source hashes remained unchanged throughout the pass:

- `clinical_report.py`: `51015cd144b5daa69358f51e17b7b413813bf4af7a13434a1964cbf4ec7ee112`
- `document.py`: `9a2bc6785699b734bccd6e0319be0aa4f51c141eed5f3c527db6cda236c3be97`

The original source and expectations remain under `/tmp/clinical-documentation-forward-9bTAMx/`; v2 findings remain under `/tmp/clinical-documentation-forward-v2-6gRQ4q/`. All v3 artifacts are separate.

## Observed execution

- **95.41 seconds** wall time, successful CLI exit, complete report/follow-up/transcript/document/review/run files.
- 28 extracted candidates; **26 accepted facts**, six marked uncertain, four uncertainty entries, five questions, **two excluded candidates**.
- 34 provider calls: extraction 1, initial/repair review 30, source relocation 2, questions 1. All used strict JSON schema; all 30 review requests contained exactly one fact.
- Accepted review verdicts: 24 supported, two unclear. Neither relocation produced an accepted repaired fact: one repair located the correct text but was rejected, while the other selected an incomplete excerpt again.
- 27,999 prompt tokens, 6,180 completion tokens, 34,179 total tokens reported across calls.
- Transcript is byte-identical, SHA-256 `d811f14710485ff6716f639ba407368633f309cb1b130cbcbbe2e2b9b298d0fd`. All 36 fact/uncertainty spans are exact source slices; all question fact IDs exist.

## What improved

- The v2 cross-fact reviewer-quotation failure was not observed in this pass's mechanical audit. Reviews were structurally isolated to one fact, and the previous emergency-advice/package-delivery facts now cite the actual supporting segment rather than `notiert.`.
- The letter does not invent a headache denial. It preserves the patient's `glaubt nicht` about unconsciousness, the daughter's conflicting account, the uncertain medication name `Amlodip`, possible dose, unknown thyroid medicine, possible antibiotic allergy and the clinician's unconfirmed causal assessment.
- The explicit instruction not to change medication until clarification is present.
- Today's planned lying/standing BP and ECG, tomorrow's named laboratory tests, package/list request and spoken emergency advice are present.
- The follow-up file explicitly discloses two exclusions and calls for completeness review; rejected content and repair history are retained in `review.json`.
- The v3 skill text now explains explicit report-model selection and the provider-key file, resolving the documentation mismatch seen in v2.

## Remaining failures with direct evidence

### 1. A recorded pulse was excluded after a correct source repair

F0015 states `Der Puls beträgt 78 pro Minute und ist regelmäßig.` Its original citation S6 omitted the pulse, so the first isolated review correctly rejected it. The locator repaired this to S7, which literally contains `der Puls 78 pro Minute und regelmäßig`.

The second reviewer nevertheless rejected it because a physical examination had not yet occurred. It explicitly acknowledges that the pulse value is in the quote, then argues that the lack of a physical examination makes it unsupported. That is an incorrect inference about the transcript: recorded measurements are present, while the doctor separately says a physical examination has not yet occurred. The final letter consequently omits the recorded pulse and regularity.

The repaired review payload also includes the original wrong evidence and previous rejection reason in `citation_repair`. The rejection reason refers to that original citation. This is observed evidence that rejected evidence/reasons can still influence the supposed re-review; the provenance should remain stored outside the actual verification payload.

See `result/review.json`, rejected candidate F0015; `result/calls/review-000-F0015-repaired/{request,response}.json`.

### 2. Agreed follow-up timing was lost because adjacent source context was not selected

F0023 correctly stated that a telephone review was agreed for the day after tomorrow, Thursday morning. The original citation used S8 plus irrelevant S10. S8 ends mid-sentence after `übermorgen, am`; the missing `Donnerstagvormittag, telefonisch` is at the start of S9.

The locator returned only S8. The repeated review correctly rejected the incomplete evidence, so the full agreed follow-up appointment is absent from the plan. Correct support requires S8 and S9. The final letter retains only the patient's appointment-note statement, marked unclear, and loses the day-after-tomorrow timing.

See `result/calls/locate-000-F0023/response.json` and `result/review.json`.

### 3. Missing citation support can still enter the report as `unclear`

F0028 says the patient noted the Thursday-morning telephone appointment, but its only attached evidence is S10: `notiert.`. The isolated reviewer correctly explains that this excerpt cannot establish the person, appointment or action, and returns `unclear`. The helper keeps this entire statement in the report with `[unklar – prüfen]`, and does not attempt citation relocation.

This statement exists in the whole transcript, so it is not an invented event. Its attached evidence remains inadequate. The workflow conflates two different cases: a well-supported statement that the speaker is uncertain, and a statement with insufficient attached evidence. The former can belong in an uncertain draft; the latter needs citation repair or exclusion. Correct evidence is S9 plus S10.

F0001 also remains marked supported even though its only segment S1 ends after `Nach vielleicht einer halben Minute`; its stated recovery while sitting is in the omitted S2. Other smaller boundary gaps remain: the BP unit continues from S6 into S7, and the clinician's full uncertainty statement continues from S2 into S3. Exact offsets have not eliminated incomplete evidence selection.

### 4. Follow-up questions ask an answered question and introduce unsupported context

- **Question 3 repeats an answered point.** It asks whether lying/standing BP has already been measured, although its cited F0016 says standing BP has not yet been measured and F0021 says the measurements are planned. Its reason itself acknowledges this. This directly fails the instruction not to ask questions already answered.
- That reason calls the seated BP `normal` and introduces orthostatic-hypotension assessment. The transcript records a number and a tentative BP-fluctuation hypothesis, not that normality judgment. This is additional model reasoning, not recorded fact.
- **Question 5** is a relevant allergy clarification, but its rationale says `insbesondere bei geplanter medikamentöser Therapie`; no new medication treatment is planned in the transcript. The recorded instruction is to leave medication unchanged until clarified.
- **Question 1** again proposes `Amlodipin` as the intended drug in its reason. The report itself correctly retains `Amlodip`. The unrecorded guess should not be introduced into the clarification rationale.
- **Question 4** compresses the patient's hedged account into `verneint Bewusstlosigkeit`, losing uncertainty preserved in the main letter. It mainly asks about the medication relationship the doctor already described tentatively, rather than clarifying the conflicting eyewitness details.
- Question 2 says both patient and daughter do not know the thyroid-drug name and strength. The patient's lack of knowledge is explicit; the daughter explicitly says only that she does not know the duration. This is a small speaker-attribution overreach in the rationale.

See `result/follow-up.md:9`–13. These shortcomings are in the separate suggestions, not silently inserted into the report, but they reduce the suggestions' fidelity and usefulness.

### 5. The report incorrectly says medication/background/allergies were not documented

All medication, hypertension, diabetes and possible allergy facts were assigned to Anamnese. Consequently `Vorgeschichte, Medikation und Allergien` displays **`Nicht im Transkript dokumentiert.`** despite those details appearing both in the transcript and earlier in the same report. This is a false absence claim caused by treating an empty section assignment as proof of source absence.

See `result/report.md:21`–23. A safer empty-section label should describe the rendered organization, or the facts should be assigned to the appropriate section; it must not make an unsupported claim about the transcript.

## Original expectations, reevaluated on v3

| Expectation | Result |
| --- | --- |
| Ten-day postural dizziness, non-spinning quality, roughly 30-second sitting recovery | Partial: trigger, duration and recovery retained; explicit non-spinning quality absent; recovery lacks its supporting adjacent segment. |
| Preserve competing consciousness accounts and uncertainty | Main letter passes; question rationale weakens the patient's hedge. |
| No fall, denied chest pain/dyspnea | Pass. |
| No invented headache denial | Pass. |
| Ramipril 5 mg mornings; literal uncertain new drug and possible dose | Main letter passes. Question rationale guesses the normalized drug name. |
| Half a thyroid tablet; unknown name, strength and duration | Main letter passes; question rationale overattributes lack of knowledge to daughter. |
| Possible antibiotic rash/allergy and unknown name | Main letter passes; allergy-question rationale invents planned medication therapy. |
| Hypertension, no diagnosed diabetes, fluid intake and explicit GI negatives | Present, but the background section falsely claims non-documentation. |
| Seated BP and regular pulse, without inventing completed standing measurements/examination | Partial: BP and unperformed procedures retained; pulse wrongly excluded. |
| Tentative BP/medication relationship, no confirmed cause | Main letter passes. |
| Today/tomorrow/day-after-tomorrow plan and pending results | Partial: today/tomorrow restored; explicit lack of test results omitted; full agreed day-after-tomorrow/Thursday telephone review excluded. |
| Medication packages/list, unchanged medication, spoken emergency advice | Pass in main content with correct supporting segments for instructions/advice. |
| Useful separate questions that do not repeat answered points | Fail for Q3 and unsupported rationales described above. |
| Exact transcript/spans and disclosure of exclusions | Mechanical pass; two exclusions disclosed. Semantic evidence gaps remain. |
| Understandable reviewable draft and clinician-review warning | Structure and warning pass; false empty-section claim and missing facts require correction. |

## Actionable changes supported by this run

1. Distinguish source uncertainty from insufficient evidence. Relocate or exclude an unsupported citation even when the reviewer chooses `unclear`; do not use the same acceptance path for both meanings.
2. Preserve full sentences/speaker turns across segment boundaries, or verify/include adjacent context when relocation still stops mid-sentence. The Thursday appointment is a concrete regression fixture.
3. Re-verify repaired evidence without passing the previous wrong evidence and rejection rationale to the reviewer. Retain that provenance in artifacts separately. The recorded-pulse false negative is a concrete regression fixture.
4. Review questions against the complete fact set for already-answered points, unrecorded judgments/plans, guessed medication names and speaker attribution. Schema and valid fact IDs cannot establish these properties.
5. Fix section assignment or avoid claiming `not documented` based solely on an empty rendered section.
6. Check completeness against source statements even when every extraction field validates. The explicit non-spinning symptom quality, pulse, appointment timing and unavailable results are observable losses in this case.

## Artifacts

Main deliverables: `result/report.md`, `result/follow-up.md`, `result/transcript.txt`.

Evidence: `result/document.json`, `result/review.json`, all 34 request/response/state folders in `result/calls/`, `result/run.json`, `receipt.json`, and `artifact-integrity.json`.

The mechanical audit's empty missing-review-quotation list is not a semantic pass: it only checks exact quotations present in reviewer rationales. F0001's unsupported continuation is paraphrased, and F0028 is accepted as unclear; both require the manual review recorded here.

Workspace: `/tmp/clinical-documentation-forward-v3-AnhvcV`. The measured v3 artifacts are retained without edits or post-hoc replacement.
