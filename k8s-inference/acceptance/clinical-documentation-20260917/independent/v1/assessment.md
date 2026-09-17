# Independent clinical-documentation forward test — original implementation

Outcome: **end-to-end failure on the single authorized attempt**. The workflow retained the exact transcript and failed explicitly; it produced no Arztbrief, no follow-up questions file, and no structured final document. The failure is reproducible from saved artifacts without another model call.

## Scope and provenance

- One independently written, explicitly synthetic German consultation, 24 lines, three labeled speakers (doctor, patient, daughter), uncertain medication details, conflicting accounts, an unanswered question, existing medication and an explicit plan.
- Expectations were recorded before reading model output in `expected-before-output.md`.
- Executed the bundled CLI with `--transcript`, `--language de`, `--report-model qwen3-8b` and the existing public platform. No audio, deployment, resource or policy changes; no repository edits.
- One temporary ordinary Rene tenant/principal key, only qwen3-8b, requested scopes, max concurrency 1, one-hour expiry. Exactly one extraction operation; no retry or new identity.
- Script hashes at launch: `clinical_report.py` = `4b8cbf6120873840fb45923936c68205691934b121d9dd9d6ae1464a8f683fb7`; `document.py` = `a09e3a68c5bbf60fe5280b3c3e166c2cf5fef1d4c361461e62ff31318c8c2c3c`.
- Parent changed source files during this run. This process had already loaded the original implementation. Original request and prompt are saved; the receipt separately records before/after hashes. This result does not evaluate the parent's subsequent schema/segment-ID revision.

## Observed result

- CLI exit 1 after 222.94 seconds, including model activation. Extraction operation `6ed88952-1ef3-4c49-95b0-8c3ccbe44ee6` succeeded; response `finish_reason` was `stop`.
- Model returned a syntactically valid JSON object classified as `consultation`, with 23 candidate facts and 5 uncertainties; 1,287 prompt tokens and 2,327 completion tokens were reported.
- All 23 facts lacked the required boolean `uncertain` field. The saved system prompt explicitly required that field. Every candidate was rejected with `invalid fact fields`.
- CLI accurately reported `no supported clinical facts; transcript retained without a report`. No contextual review call and no question-generation call ran.
- The original transcript bytes are unchanged; SHA-256 is `d811f14710485ff6716f639ba407368633f309cb1b130cbcbbe2e2b9b298d0fd`. All five saved uncertainty source spans are exact. Output files are mode 0600 within the isolated temporary workspace.

## Fidelity observations about unaccepted candidates only

These observations concern raw extraction candidates. None entered a report, and the contextual fact verifier was never reached.

1. **Invented denial of headache.** Candidate F0007 states `Ärztin fragt nach neuen Kopfschmerzen, die Patientin bestreitet.` Its sole quote is `Gab es auch neue Kopfschmerzen?` The next source line is the daughter's interruption about medication; no headache answer exists. The prompt explicitly prohibited interpreting unanswered questions as negative answers. Schema rejection prevented this candidate from reaching a report, but semantic review was not exercised.
2. **Nonliteral quote.** Candidate F0015 joins the doctor's fluid-intake question and the patient's reply while removing the newline and `Patientin:` label. Its quoted string does not occur in the transcript. All other candidate quote strings occur literally. The missing uncertainty fields masked this separate citation error in the rejection reasons.
3. **Important omissions.** The patient's approximate ten-day symptom duration occurs in a quote but is missing from the candidate statement. The doctor's explicit instruction to make no medication changes until clarification is absent from the candidate set. The plan candidate omits `today` for the lying/standing measurement and ECG. This extraction therefore would still need a completeness review even after schema compliance.
4. **Weakened uncertainty in wording.** Candidate F0004 rewrites `Ich glaube nicht, dass ich bewusstlos war` as `Patientin bestreitet, bewusstlos gewesen zu sein`; this loses the patient's hedge. Separate daughter and doctor candidates do preserve the conflicting account and lack of a definite conclusion.
5. **Useful candidate behavior.** The extraction kept the literal unclear medication name `Amlodip` and possible dose rather than correcting it, preserved the unknown thyroid-tablet name/strength, the unconfirmed prior antibiotic allergy, seated BP/pulse values, the fact that standing BP and examination had not yet occurred, the doctor's tentative assessment, pending tests and spoken emergency advice.
6. **Section usability.** Background medication, hypertension, diabetes history and possible allergy were all labeled `history`, so the intended separate background section would be empty if those candidates were merely made schema-compliant. Five uncertainty entries also repeat thyroid-medication ambiguity. No final letter or suggested-question usability assessment is possible because neither was generated.

## Actionable issues for the parent

- Enforce required extraction fields through the model's structured response schema; do not silently interpret absent `uncertain` as false. The parent has already announced this revision.
- Keep the unanswered-headache fixture as a semantic regression case. Schema compliance alone cannot address it; the verifier must reject the invented denial while retaining actual negative answers.
- Use deterministic source references or otherwise require exact quotations. This run independently exposed a joined-speaker quotation failure.
- Report schema failures more specifically: 23 candidates were rejected because `uncertain` was missing. The current `invalid fact fields` and `no supported clinical facts` messages are safe but obscure the actionable cause.
- Recheck duration, medication instructions, plan timing and background-section placement in the revised workflow. Do not count extraction completion alone as a complete report.

## Cleanup

The acceptance-pattern cleanup initially attempted `POST /admin/api/v1/keys/{id}:revoke` and returned 404; that original result remains in `receipt.json`. After the parent identified the correct endpoint, queued cleanup immediately executed `DELETE /admin/api/v1/keys/{id}` and received HTTP 200 at 2026-09-17T06:39:17.880299+00:00. **The temporary key is revoked.** `revocation.json` is the authoritative cleanup record. No credentials were printed or written to artifacts.

## Artifacts

- `transcript-source.txt`: independent synthetic input.
- `expected-before-output.md`: prereview expectations.
- `result/transcript.txt`: unchanged workflow output transcript.
- `result/review.json`: all 23 rejected candidates and five supported uncertainty spans.
- `result/calls/extract-000/request.json`, `response.json`, `state.json`: exact request, response and operation provenance.
- `result/run.json`: explicit incomplete run state.
- `receipt.json`: one-attempt CLI measurement and original cleanup attempt.
- `revocation.json`: successful key revocation.
- `artifact-integrity.json`: transcript equality, missing deliverables, field/citation checks and model usage.

Workspace: `/tmp/clinical-documentation-forward-9bTAMx`. This is a documentation fidelity test, not clinical validation.
