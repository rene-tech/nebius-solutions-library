#!/usr/bin/env python3
"""Transcript-alignment audit, not clinical adjudication or a safety score.

The secondary WER removes only listed hesitation tokens from BOTH texts. It is
a post-hoc sensitivity analysis, reported alongside (never instead of) unchanged
primary WER. Negation, yes/no answers, numbers and medication words remain.
"""

import argparse
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "nemotron-speech-20260916"))
from score_medical import alignment, words

HESITATIONS = {"um", "uh", "erm", "er", "hmm", "hm", "mm", "mhm", "uhh", "ah"}
TERMS = {"diarrhea", "blood", "vomiting", "fever", "feverish", "inhaler", "inhalers",
         "dioralyte", "paracetamol", "steroid", "steroids", "antihistamines", "loratadine",
         "emollients", "allergies", "eczema", "asthma", "tablets"}
COUNTS = {"one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten",
          "fourteen", "once", "twice"}
NEGATIONS = {"no", "not", "never", "haven't", "don't", "hasn't", "doesn't", "wasn't", "didn't"}


def hesitation_normalized(text):
    return " ".join(token for token in words(text) if token not in HESITATIONS)


def audit(row):
    expected = words(row["reference"])
    indexed = {}
    for error in row["quality"]["errors"]:
        if error["type"] != "insertion":
            indexed[error["reference_index"]] = error
    selected = []
    for index, token in enumerate(expected):
        category = "medical-term" if token in TERMS else "number" if token in COUNTS or token.isdigit() else "negation" if token in NEGATIONS else None
        if category:
            selected.append({"reference_index": index, "category": category, "reference": token,
                             "context": " ".join(expected[max(0, index - 6):index + 7]),
                             "alignment": indexed.get(index, {"type": "correct", "recognized": token})})
    secondary = alignment(hesitation_normalized(row["reference"]), hesitation_normalized(row.get("text", "")))
    return {"model": row["model"], "case": row["case"], "primary_wer": row["quality"]["wer"],
            "hesitation_normalized_wer": secondary["wer"],
            "hesitation_normalized_reference_words": secondary["reference_words"],
            "hesitation_normalized_errors": sum(secondary[k] for k in ("substitutions", "deletions", "insertions")),
            "review_items": selected,
            "limitation": "Alignment flags require context/listening review; a missing conversational no is not automatically a reversed clinical fact."}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("results", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    rows = [json.loads(path.read_text()) for path in sorted(args.results.glob("*-en-*-r1.json"))]
    report = {"hesitation_tokens_removed_from_both_sides": sorted(HESITATIONS),
              "selection": "All first-attempt English consultations; no dose, negation or medical term correction",
              "rows": [audit(row) for row in rows if "quality" in row]}
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps([{k: row[k] for k in ("model", "case", "primary_wer", "hesitation_normalized_wer")} for row in report["rows"]], indent=2))


if __name__ == "__main__":
    main()
