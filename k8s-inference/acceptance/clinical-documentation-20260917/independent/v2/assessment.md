# Independent forward test, revised v2 profile

Execution succeeded and the main letter is substantially faithful to the synthetic source. **Semantic citation support failed:** the reviewer marked all 29 facts supported, including facts whose sole source excerpt is `notiert.`. This pass is not evidence that the evidence-linking or contextual review is reliable. It is not independent clinical validation.

## Run and deliverables

- One newly authorized attempt against `https://api.tokenfactory.nebius.com/v1`, exact model `Qwen/Qwen3-235B-A22B-Instruct-2507`; no retry. Provider model was confirmed in the catalog first.
- Original synthetic transcript and preregistered expectations were reused unchanged from `/tmp/clinical-documentation-forward-9bTAMx/`.
- CLI completed in **93.70 seconds**: 29 facts, five marked uncertain, six separate uncertainties, five suggested questions, zero rejected facts.
- All six expected files exist: report, follow-up questions, transcript, document, review, run metadata. Input and retained transcript are byte-identical. All **37** fact/uncertainty character spans point to the exact saved substring. All question fact IDs exist.
- Three calls completed with `finish_reason=stop` and strict JSON schema: extraction 47.82s, review 33.34s, questions 11.32s. Usage totals: 16,765 prompt tokens, 4,226 completion tokens, 20,991 total tokens. This is observed request usage, not a quality score.
- Source hashes were unchanged throughout this run: `clinical_report.py` `6cd03f408cd251f7a4173491be3242636873793bb9ff8a4f10c3047415382a93`; `document.py` `5f7fd6c7c7c4514efc604bc86e2e0cf4a6111c3f8f2e20ab94b37e3704779d54`.
- Fresh ordinary scoped Rene key was revoked in `finally` with **DELETE HTTP 200**. Provider credentials stayed in the existing protected file; no secret was printed or copied into artifacts. No repository, resource, policy or deployment edits.

## Main finding: incorrect citations approved by review

The extraction selected real source IDs, and their offsets are mechanically exact. It frequently selected the wrong segment or failed to include an adjacent segment spanning the same statement. The reviewer received all facts in one request and used evidence attached to other facts to justify these wrong citations, despite the explicit instruction against doing so.

Strongest examples:

| Fact | Report statement | Only attached evidence | Reviewer verdict |
| --- | --- | --- | --- |
| F0027 | Daughter will bring medication packages in the afternoon | `notiert.` | supported |
| F0028 | Call emergency services for recurrent loss of consciousness, chest pain or severe dyspnea | `notiert.` | supported |
| F0029 | Patient noted Thursday-morning telephone appointment | `notiert.` | supported |
| F0009 | Longstanding treatment for hypertension | Segment about antibiotic rash, drinking, vomiting/diarrhea and seated BP | supported |
| F0012 | About one litre of fluid daily | Segment about pulse, unperformed examination and tentative medication relationship | supported |

The statements occur in the full transcript; the defect is their unsupported attached citation and the false reviewer confirmation. There is no claim that these five statements were invented from scratch.

The mechanical audit found **14 of 29 reviewer reasons quoting text that occurs elsewhere in the transcript but is absent from that fact's own attached evidence**: F0009, F0010, F0012, F0013, F0014, F0018, F0019, F0023, F0024, F0025, F0026, F0027, F0028, F0029. Leading/trailing ellipses in reviewer quotations were ignored for this comparison. Additional incomplete citations (F0001, F0005, F0008) have paraphrased reviewer reasons rather than verbatim quotations, so are not included in that 14-fact mechanical count.

The report's source appendix makes the defect directly reviewable at `result/report.md:81`–83. This is a failure of semantic attribution despite correct character offsets and a successful process exit.

## Preregistered expectations: content assessment

| Expectation | Observed result |
| --- | --- |
| Ten-day postural dizziness, non-spinning quality, about 30 seconds' recovery sitting | Partial: duration, trigger and recovery preserved; explicit non-spinning quality omitted from the letter statement. |
| Conflicting daughter/patient loss-of-consciousness accounts | Pass: both accounts and the clinician's unresolved assessment retained; the patient's hedge `glaubt nicht` is preserved. |
| No fall; denied chest pain and dyspnea | Pass. |
| Do not fabricate a negative answer to headache question | Pass: no headache denial is present. |
| Ramipril 5 mg mornings; uncertain literal `Amlodip`, possible 5 mg nightly | Pass in letter content; Ramipril's attached segment does not contain its name/duration. |
| Half a thyroid tablet, unknown name/strength/duration | Pass in content; the old-list statement has a grammatical error and wrong evidence segment. |
| Possible antibiotic rash/allergy and unknown antibiotic | Pass: uncertainty preserved. |
| Hypertension, no diagnosed diabetes, approximate daily intake, no recent vomiting/diarrhea | Pass in content; several attached citations are wrong. |
| Seated BP 118/72, pulse 78 regular, no completed standing BP/examination | Pass in content. |
| Tentative BP/medication relationship without confirmed cause | Pass in content. |
| Planned measurements/ECG today, labs tomorrow, phone review day after tomorrow/Thursday | Partial: planned status, missing results, tomorrow and Thursday remain; `today` for measurements/ECG and `day after tomorrow` are omitted. |
| Packages/list, no medication changes, spoken emergency advice | Pass in content; medication instructions and emergency advice have wrong citations. |
| Useful separate questions prioritizing ambiguous meds, contradiction, allergy | Partial: all five are relevant and linked, with no tests or treatment prescribed. One rationale speculates that `Amlodip` may mean `Amlodipin`; the fifth asks the clinician to evaluate the discrepancy instead of asking a concrete factual clarification. |
| Unchanged transcript, exact character slices, exclusion disclosure | Mechanical pass: transcript exact; all spans exact; nothing excluded. This does not rescue the semantic citation failure. |
| Understandable draft, separate review/questions, clinician-review warning | Pass for delivered structure and warning. Medication facts are under Anamnese rather than the dedicated medication section, and the source appendix repeats long 360-character excerpts. |

## Other observed limitations

- The first question's reason introduces an unrecorded possible standardized drug name (`Amlodipin`). It is explicitly hedged and outside the letter, so this is not an unmarked medication substitution in the report, but it undermines the intended literal-name clarification workflow and may anchor the reviewer.
- The question about duration of thyroid treatment has `basis=not_documented`, although the source explicitly states that the duration is unknown. This is a minor classification inconsistency.
- The letter is a structured bullet draft, not a finished correspondence letter. F0008 contains `seit wann die Patientin sie eingenommen wird`, requiring language cleanup.
- The inspected SKILL.md still described a default qwen3-8b profile and an example without `--report-model`, whereas the revised helper required explicit model selection. The supplied test command resolved this mismatch; a first-time user following only the displayed example would need updated instructions.

## Suggested fixes supported by this run

1. Review each fact with only its own attached evidence, so evidence for another fact cannot silently justify a wrong citation. Do not equate valid source IDs with support.
2. Improve source segmentation or permit verified citation relocation when sentences cross segment boundaries. Preserve original citations and reviewer decisions if repaired.
3. Keep this exact case as a regression for the prior invented-headache failure, medication uncertainty, plan timing, and the `notiert.` citation failure.
4. Preserve explicit non-spinning symptom quality and plan timing; prevent guessed medication names in question rationales; improve background-section assignment and F0008 grammar.

Parent has since announced v3 with isolated per-fact review and one citation-relocation attempt. The artifacts here evaluate v2 only; they have not been overwritten or reinterpreted as a v3 result.

## Artifacts

- `result/report.md`: completed report with visible wrong source appendix.
- `result/follow-up.md`: uncertainties and five questions.
- `result/transcript.txt`: exact input transcript.
- `result/document.json`: complete facts, evidence and approving review decisions.
- `result/calls/`: all three strict-schema requests, responses, timestamps and usage.
- `receipt.json`: launch/end hashes, exact credential-free command, latency and successful revocation.
- `artifact-integrity.json`: mechanical checks and 14 demonstrable reviewer-quote attribution failures.

Workspace: `/tmp/clinical-documentation-forward-v2-6gRQ4q`.
