# Medical-content fidelity: which ASR is technically most faithful?

**English Nemotron is the best-supported choice for the tested English medical
recordings.** It preserves more of the selected medical facts clearly, retains
the complete spoken tablet quantity, and has the lowest word-error rate. This
does not qualify any of the models for unchecked clinical documentation.

## What was checked

The same complete first-attempt transcripts from the 49-request experiment were
reviewed against the human transcripts. There were **44 selected checklist items
across two simulated English consultations**, covering symptoms/history,
anatomical location, negation, medications, quantities, assessments and plans.
The complete transcripts were read, not just matched keywords. Repeated correct
mentions were considered; the absence of a repeated conversational `no` was not
automatically called a clinical reversal. Claims are about fidelity to the
spoken dialogue, not whether the dialogue's treatment plan is medically right.

The checklist is post-hoc, agent-adjudicated and not blinded or independently
clinician-scored. Some items combine related clauses. Counts depend on this
explicit granularity and are **not a medical-accuracy percentage, population
error rate or severity-weighted score**. All classifications, literal normalized
source/output excerpts, transcript hashes and operation IDs are retained in
[clinical-fidelity.json](clinical-fidelity.json). Extraction verifies the quotes;
it does not prove the subjective classifications correct.

| ASR | Preserved clearly | Degraded / needs review or correction | Required fact missing or not reliably recoverable |
| --- | ---: | ---: | ---: |
| Nemotron English 0.6B | **39** | 5 | 0 |
| Nemotron multilingual 0.6B | 32 | 12 | 0 |
| Parakeet Realtime EOU 120M | 33 | 10 | **1** |

“Degraded” includes phonetic corruption that a reviewer might understand; it does
not mean every such item would cause harm. For example, `parasetamol` is not
counted as a completely different medication, but it is not a clean drug-name
transcription either. Conversely, do not reconstruct a missing tablet quantity
from what a typical prescription would say. The one-item difference between
multilingual and Parakeet's clear counts does not establish a meaningful clinical
ranking between them; Parakeet's quantity loss is the more concerning observed
information loss in this particular pair of recordings.

## Decisive examples

These quotations compare text; they are **not medication instructions**.

| Reference content | English Nemotron | Multilingual Nemotron | Parakeet |
| --- | --- | --- | --- |
| paracetamol … two tablets up to four times a day | `paracetamol two tablets up to four times a day` | `paracetamol uh two tables up to four times a day` | `parasetamol tube it's up to four times a day` |
| gastroenteritis | Correct | Correct | `gastropentaritis` |
| inside the elbows | Correct | `inside the airbows` | `inside the airballs` |
| loratadine or Piriton | `latidine or puritan` | `loratidine or piritin` | `la ratadine or piritin` |
| fexofenadine | `fetcophenidine` | `fexapenide` | `fx ephenidine` |
| emollients | First occurrence damaged, later `emollients` correct | `ammolians` / `ammolions` | `amoleins` |

Both Nemotron models reproduce their transcripts identically across all three
runs per consultation. The critical Parakeet tablet-quantity, gastroenteritis
and eczema medication-name errors also persist in all three repeats, despite
small changes elsewhere. These are not isolated best/worst-run selections.

Most core history and timing survives: three-day diarrhea, six-to-seven stools
per day, left-sided cramping, resolved initial vomiting, four-day skin symptoms,
past asthma, absence of regular medication/allergies in the eczema case, and
conditional follow-up intervals. Multilingual damages two eczema denial/
confirmation passages; the evidence does **not** establish positive fever or
breathing difficulty. All three need correction of some product/drug names;
none is a clean medication-dictation solution on this evidence.

## German: useful outputs, specific fidelity failures, no head-to-head winner

Only multilingual Nemotron supports German in this comparison. The 31 reference
clips again have 18.31% WER, but medically relevant examples are more informative:

- Row338: birth year **1963 is omitted**; day and month remain.
- Row526: **Oligurie → Aligorie**, **Tachykardie → Tarykadie**. The hematocrit
  value **53% is retained** as spoken-number words.
- Row1090: **130/90 is correctly retained**, along with the description and
  planned blood tests. Raw WER is 38.46% partly because digits become words;
  this is not a blood-pressure-value error.
- Row939: an **allergy phrase not present in the reference** appears among
  heavily corrupted bed-positioning instructions. Do not turn it into a patient
  allergy in a downstream record.
- Row193: **empty transcript**, not a successful recognition despite operation
  status `succeeded`.

These examples are included with full original reference/output text in the
JSON review. They are not five additional clinical encounters or a separate
quantitative German fact-accuracy score.

## Why the clinician notes were not used blindly as gold

The source notes and the actual human dialogue disagree in material places:

- GI note says bilious vomiting; the dialogue describes normal food colour.
- GI note broadens family illness beyond the child described in the dialogue.
- Eczema note introduces **Betnovate BD** and **Cetraben QDS**; those brands and
  doses were not spoken. They must not be invented to make an ASR output match.
- Eczema follow-up is next week if no better in the dialogue; the note says
  10–14 days. The dialogue also contains a patient/clinician disagreement about
  skin distribution, which must not be silently resolved by the recognizer.

Human transcripts were therefore the primary reference. Their uncertainty and
overlap annotations are still limitations; this is not a new clinician listening
review of the source audio.

## Practical recommendation

1. For **English recorded consultations**, use English Nemotron as the preferred
   candidate for the next clinician-reviewed evaluation. It wins both textual
   and medical-content fidelity here and is faster in the deployed setup.
2. For **German**, multilingual Nemotron is the only applicable tested candidate,
   not a demonstrated winner over other German medical recognizers. The named
   omissions/corruptions require review; do not infer readiness from English.
3. Do not switch medical transcription to Parakeet on a quality claim. It may
   have a separate interactive end-of-utterance benefit, which this full-file
   evaluation has not measured.
4. A clinician-reviewed draft workflow is the appropriate next evaluation,
   not unattended final records. A downstream LLM can add or lose facts even
   when ASR is correct; the earlier report-generation experiment already found
   that, and no new downstream reports were generated in this comparison.

Reviewing and correcting generated notes before they enter patient records is
also consistent with [NHS England's ambient-scribing guidance](https://digital.nhs.uk/data-and-information/information-governance/guidance/using-ai-enabled-ambient-scribing-products-in-health-and-care-settings/guidance-for-health-and-care-professionals),
checked 2026-09-16. This is a recommendation and a limitation of the evidence,
not a newly implemented product guardrail, access rule or deployment change.
