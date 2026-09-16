#!/usr/bin/env python3
"""Publish a transparent, agent-adjudicated medical-content review with literal evidence.

This is a descriptive post-hoc checklist, NOT a validated clinical metric, a
blind clinician assessment, or an automated clinical judge. The explicit grades
below come from reading the entire reference and all three first transcripts.
The script extracts/verifies evidence and computes counts; it does not infer
clinical correctness from regex matches. Repeats do not increase sample size.
"""

import argparse
import hashlib
import json
import re
from collections import Counter, defaultdict
from pathlib import Path

MODELS = (
    "nemotron-speech-en-0-6b", "nemotron-speech-multilingual-0-6b",
    "parakeet-realtime-eou-120m-v1",
)
GRADES = {
    "P": "preserved: stated medical content is recoverable directly without repairing a clinical term or quantity",
    "D": "degraded: phonetic/wording/confirmation damage requires review or repair; not automatically a different diagnosis",
    "M": "missing_or_changed: a specific required fact is not reliably present; do not reconstruct it by guessing",
}

# ID, category, reference fact (paraphrase, not quotation), evidence anchor,
# explicit grades in MODELS order, adjudication note. Composite clauses are
# deliberately disclosed; these are 44 checklist items, not 44 independent cases.
FACTS = [
    ("GI01", "history", "Diarrhea for three days", r"last three days", "PPP", "Duration stated by all three."),
    ("GI02", "quantity", "Six or seven bowel movements per day", r"six.{0,12}seven times", "PPP", "The 6–7 range is preserved."),
    ("GI03", "negation", "No blood in stool", r"no.? no blood", "PPP", "Explicit denial retained."),
    ("GI04", "anatomy", "Pain in the lower abdomen, on the left side", r"lower abdomen", "PPP", "Location and laterality retained in adjacent dialogue."),
    ("GI05", "history", "Cramping pain comes and goes", r"muscular cramp", "PPP", "Cramp and intermittent course retained."),
    ("GI06", "negation", "Felt hot at onset but no current fever", r"haven't had a fever", "PPP", "Current-versus-past distinction retained."),
    ("GI07", "history", "Vomited at onset; vomiting has stopped", r"stopped vomiting", "PPP", "Resolution is not changed into continuing vomiting."),
    ("GI08", "history", "Vomit described as normal food colour", r"normal food colo[u]?r", "PPP", "Do not score the conflicting note's bilious description as gold."),
    ("GI09", "negation", "Dialogue establishes no blood in vomit", r"blood in your vomit", "PPP", "All retain the negative question and response; the English output retains the clearest explicit no-blood answer. Not scored as reversal."),
    ("GI10", "history", "Can retain oral fluids despite loss of appetite", r"hold down fluids|halt down fluids", "PDP", "Multilingual halt down is a damaged verb; remaining context suggests the intended ability, but requires repair."),
    ("GI11", "history", "One child vomited but did not have diarrhea", r"child was vomiting", "PPP", "Do not broaden this to the entire family from the reference note."),
    ("GI12", "history", "Asthma managed with an inhaler", r"other than.{0,12}asthma|other than.{0,12}athsma", "PPP", "Spoken condition/inhaler association retained."),
    ("GI13", "negation", "No other medications besides inhalers", r"other medications", "PPP", "Explicit negative medication history retained."),
    ("GI14", "diagnosis", "Clinician suggests gastroenteritis, not a proven diagnosis", r"gastroenteritis|gastropentaritis", "PPD", "Parakeet corrupts the disease name; tentative wording remains. No diagnostic-validity judgment is made."),
    ("GI15", "plan", "Clinician does not recommend antibiotics", r"need anything like antibiotics", "PPP", "Treatment negation preserved; this is a statement about the dialogue, not advice."),
    ("GI16", "medication", "Dioralyte mentioned as a pharmacy product", r"Dioralyte|dire light|dirolite", "DDD", "All corrupt the product spelling/segmentation; do not silently repair the transcript."),
    ("GI17", "medication", "Paracetamol named", r"paracetamol|parasetamol", "PPD", "Parakeet parasetamol is recognizable but noncanonical, so degraded rather than a different drug."),
    ("GI18", "quantity", "Two tablets per dose stated in the dialogue", r"up to four times a day", "PDM", "English: two tablets; multilingual: two tables; Parakeet: tube it's, losing reliable quantity/form. Never infer the dose from usual practice."),
    ("GI19", "quantity", "Frequency limit up to four times per day", r"up to four times a day", "PPP", "Frequency retained even when the adjacent tablet quantity is damaged."),
    ("GI20", "plan", "Rest/time off work for two to three days", r"two to three days", "PPP", "Later explicit time off and rest resolves awkward earlier wording."),
    ("GI21", "plan", "Review in three to four days if not improving", r"three to four days", "PPP", "The conditional follow-up is preserved; the note gives a different range."),
    ("GI22", "plan", "Possible future stool sample if symptoms persist", r"sample of your", "DDD", "All render stool as store in this clause; future conditional testing is retained but specimen name needs repair."),
    ("EC01", "history", "Corrected symptom duration is four days", r"last four days", "PPP", "The later four-day recap remains even where an earlier correction is lost."),
    ("EC02", "history", "Red, itchy, sore and cracked skin", r"red skin|quite sore|skin is a bit cracked", "PPP", "Full transcript retains all descriptors; saw/sore at one occurrence alone is not treated as whole-fact loss."),
    ("EC03", "anatomy", "Patient specifies chest, hands and inside elbows", r"inside the elbows|inside the airbows|inside the airballs", "PDD", "Inner-elbow anatomy becomes airbows/airballs. Do not resolve the separate speaker disagreement about chest/back/legs using the clinician note."),
    ("EC04", "negation", "No bleeding/discharge from the skin", r"bleeding or discharge", "PPP", "The initial explicit denial survives all three; one later repetition is not required to count it."),
    ("EC05", "negation", "No facial involvement", r"on my face", "PPP", "Explicit face-specific denial retained."),
    ("EC06", "negation", "No fever with this rash", r"temperature or fevers", "PDP", "Multilingual merges the answer into Nos are itching; likely no, but explicit denial is degraded, not counted as positive fever."),
    ("EC07", "negation", "Patient denies cough/breathing difficulties", r"no breathing difficulties", "PDP", "Multilingual keeps the clinician's negative question but drops the separate patient no before the urine question. Confirmation is less clear, not a demonstrated polarity reversal."),
    ("EC08", "history", "Past asthma, described by patient as no longer active", r"asthma in the past", "PPP", "Past-versus-current wording preserved; no inference about actual disease resolution."),
    ("EC09", "negation", "No regular current medication", r"regular medications", "PPP", "Explicit no retained."),
    ("EC10", "negation", "Patient denies allergies", r"allergies.{0,6}at all|allergies at allergies", "PPP", "The repeated allergy question and final denial survive."),
    ("EC11", "medication", "OTC steroid cream used last night without benefit", r"pharmacy last night", "PDD", "English first statement says steroid; multilingual says sterile and Parakeet spirit. Later steroid wording provides context but the conflicting history needs review."),
    ("EC12", "history", "Antihistamines already tried without benefit", r"didn't really help", "PPP", "Whole-dialogue context retains prior use and lack of benefit despite damaged repetitions."),
    ("EC13", "diagnosis", "Clinician suspects an eczema flare", r"flare.up of your eczema", "PPP", "Suspected assessment and correct eczema label survive."),
    ("EC14", "plan", "Plan to prescribe a stronger steroid", r"stronger prescription", "PPP", "Future prescription plan preserved, not an already taken treatment."),
    ("EC15", "medication", "Emollients/moisturizing skin treatment in the plan", r"emollients|ammoience|ammolians|ammolions|amoleins", "PDD", "English has a correct later emollients occurrence, despite ammoience initially. Neither other model has a clean term; moisturizing purpose remains."),
    ("EC16", "quantity", "Initial skin-treatment trial seven to ten days", r"first seven to ten days", "PPP", "Range retained; do not import invented drug dose/frequency from the note."),
    ("EC17", "medication", "Loratadine example named", r"loratadine|loratidine|latidine|la ratadine", "DDD", "latidine / loratidine / la ratadine require correction. Phonetic proximity is not independently validated drug identification."),
    ("EC18", "medication", "Piriton example named", r"Piriton|puritan|piritin", "DDD", "All require a brand-name correction."),
    ("EC19", "medication", "Fexofenadine proposed", r"Fexofenadine|fetcophenidine|fexapenide|fx ephenidine", "DDD", "All three corrupt the drug name; no model gets a clean pass."),
    ("EC20", "plan", "Keep a diary of possible triggers", r"diary of any triggers", "PPP", "Diary and triggers remain despite multilingual kiffing instead of keeping."),
    ("EC21", "plan", "Return next week if not improving", r"see me next week", "PPP", "Conditional timing retained; do not replace with the note's 10–14 days."),
    ("EC22", "quantity", "Patient gives age thirty-one", r"thirty.one", "PPP", "Age retained; no age inference from other demographic context."),
]

GERMAN_EXAMPLES = [
    ("de-0338", "missing", "Birth year 1963 is omitted; the day and month remain."),
    ("de-0526", "degraded", "Tachykardie becomes Tarykadie and Oligurie becomes Aligorie; these terms require correction. Hematocrit 53 percent is retained as words."),
    ("de-1090", "preserved", "Blood pressure 130/90, described as slightly elevated, and planned blood tests are retained. Spelled-out numbers inflate raw WER without changing their values."),
    ("de-0939", "unsupported_content", "Output adds an allergy phrase and segment-related wording absent from the reference, besides corrupting the bed-positioning instruction. Not evidence of a patient allergy."),
    ("de-0193", "empty", "No transcript; all reference content missing. API completion is not a transcription-quality pass."),
]


def normalized(text):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]*>", "", text)).strip()


def evidence(text, anchor):
    text = normalized(text)
    found = []
    for match in list(re.finditer(anchor, text, flags=re.I))[:5]:
        start, end = max(0, match.start() - 190), min(len(text), match.end() + 210)
        found.append({"start": start, "end": end, "quote": text[start:end]})
    assert found, f"Missing evidence anchor: {anchor}"
    assert all(text[item["start"]:item["end"]] == item["quote"] for item in found)
    return found


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    args = parser.parse_args()
    directory = args.directory
    transcripts = {}
    for model in MODELS:
        for case in ("en-01", "en-02"):
            transcripts[model, case] = json.loads((directory / "results-r1" / f"{model}-{case}-r1.json").read_text())
    review = []
    totals = {model: Counter() for model in MODELS}
    by_category = {model: defaultdict(Counter) for model in MODELS}
    for identifier, category, fact, anchor, grades, note in FACTS:
        case = "en-01" if identifier.startswith("GI") else "en-02"
        ref = transcripts[MODELS[0], case]["reference"]
        item = {"id": identifier, "case": case, "category": category, "reference_fact_paraphrase": fact,
                "reference_evidence": evidence(ref, anchor), "adjudication": note, "models": {}}
        for model, grade in zip(MODELS, grades, strict=True):
            row = transcripts[model, case]
            assert row["reference"] == ref and grade in GRADES
            item["models"][model] = {"grade": grade, "evidence": evidence(row["text"], anchor),
                                      "operation_id": row["operation_id"],
                                      "transcript_sha256": hashlib.sha256(row["text"].encode()).hexdigest()}
            totals[model][grade] += 1
            by_category[model][category][grade] += 1
        review.append(item)
    output = {
        "reviewer": "Codex agent; not clinician-adjudicated or blinded",
        "method": "Manual full-text comparison; script extracts literal evidence and counts explicit judgments, it does not make the judgments",
        "scope": "44 post-hoc checklist items across two English simulated consultations, first attempts; not independent clinical samples",
        "grades": GRADES,
        "limitations": ["Reference transcripts include uncertain/overlapping speech; audio has not been independently re-adjudicated by a clinician",
                        "Clinician notes contain unspoken details and conflicting facts and are not primary gold",
                        "No severity weights or clinical-accuracy percentage; item granularity and phonetic judgments are subjective",
                        "No downstream report regeneration or new inference calls in this content review",
                        "German has only one applicable model in this comparison, not a head-to-head medical ranking"],
        "totals": {model: {grade: counts[grade] for grade in GRADES} for model, counts in totals.items()},
        "by_category": {model: {category: dict(counts) for category, counts in groups.items()} for model, groups in by_category.items()},
        "items": review,
    }
    output["german_illustrative_findings_not_a_separate_score"] = []
    for case, finding, explanation in GERMAN_EXAMPLES:
        row = json.loads((directory / "results-r1" / f"{MODELS[1]}-{case}-r1.json").read_text())
        output["german_illustrative_findings_not_a_separate_score"].append({
            "case": case, "finding": finding, "explanation": explanation,
            "reference": row["reference"], "text": row["text"],
            "operation_id": row["operation_id"], "wer": row["quality"]["wer"],
        })
    (directory / "clinical-fidelity.json").write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({key: output[key] for key in ("scope", "totals", "by_category")}, indent=2))


if __name__ == "__main__":
    main()
