"""Apply the final review-only/empty-section rendering fix to retained evidence.

No inference is performed. Originals and their historical timing stay unchanged.
This is a deterministic regression projection, not a new end-to-end live run.
"""

import argparse
import copy
import json
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[2] / "integrations/librechat/skills/clinical-documentation/scripts"
sys.path.insert(0, str(SCRIPTS))
from clinical_report import save
from document import VERSION, digest, render, render_review


def project(source, destination):
    original = json.loads((source / "document.json").read_text())
    result = copy.deepcopy(original)
    uncertain_ids = set()
    facts = []
    for fact in result["facts"]:
        if fact["review"]["verdict"] == "supported":
            facts.append(fact)
        else:
            uncertain_ids.add(fact["id"])
            result["rejected"].append({"candidate": fact, "verdict": fact["review"]["verdict"],
                                       "reason": fact["review"]["reason"]})
    result["facts"] = facts
    # A suggestion referencing a withheld fact belongs with that review, not the accepted facts.
    result["questions"] = [q for q in result["questions"] if not uncertain_ids.intersection(q["fact_ids"])]
    result["schema"] = VERSION
    result["projection"] = {"source_document_sha256": digest(original), "source": str(source),
                            "live_inference": False,
                            "change": "unclear source support withheld for human review; neutral empty-section text; readable review queue"}
    report, followup = render(result, result["language"])
    save(destination / "document.json", result)
    save(destination / "report.md", report)
    save(destination / "follow-up.md", followup)
    save(destination / "review.md", render_review(result, result["language"]))
    save(destination / "transcript.txt", (source / "transcript.txt").read_text())
    assert all(f["review"]["verdict"] == "supported" for f in result["facts"])
    assert len(result["facts"]) + len(result["rejected"]) == len(original["facts"]) + len(original["rejected"])
    return {"case": source.name, "source_sha256": digest(original), "facts": len(result["facts"]),
            "review_candidates": len(result["rejected"]), "newly_withheld": sorted(uncertain_ids),
            "live_inference": False}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    results = [project(path.parent, args.output / path.parent.name) for path in sorted(args.source.glob("*/document.json"))]
    save(args.output / "projection.json", {"results": results, "live_inference": False})
    print(json.dumps({"cases": len(results), "facts_preserved_in_report_or_review": True, "live_inference": False}))
