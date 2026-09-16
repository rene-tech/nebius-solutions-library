"""Summarize actual completion receipts; preserve unverified judge findings."""

import argparse
import collections
import json
from pathlib import Path


def review_schema_ok(parsed, expected_ids):
    reports = parsed.get("reports")
    return (isinstance(reports, list)
            and {row.get("input_id") for row in reports} == expected_ids
            and all(isinstance(row.get("findings"), list) for row in reports))


def summarize(root):
    manifest = json.loads((root / "manifest.json").read_text())
    generation, review = [], []
    totals = collections.Counter()
    for item in manifest["inputs"]:
        path = root / "generation" / f"{item['id']}.json"
        if not path.exists():
            generation.append({"id": item["id"], "status": "missing"})
            continue
        receipt = json.loads(path.read_text())
        raw = receipt.get("raw_response", {})
        totals.update({"generation_" + key: value for key, value in raw.get("usage", {}).items()
                       if isinstance(value, int)})
        report = receipt.get("parsed", {})
        quotes = [quote for fact in report.get("facts", []) for quote in fact.get("evidence_quotes", [])]
        generation.append({"id": item["id"], "status": receipt["status"], "case": item["case"],
                           "source": item["source"], "kind": item["kind"], "sample_group": item.get("sample_group"),
                           "document_kind": report.get("document_kind"), "wall_seconds": receipt["wall_seconds"],
                           "facts": len(report.get("facts", [])), "evidence_quotes": len(quotes),
                           "nonliteral_evidence_quotes": [q for q in quotes if q not in item["text"]],
                           "uncertainties": report.get("uncertainties", [])})
    for case, context in manifest["cases"].items():
        path = root / "review" / f"{case}.json"
        if not path.exists():
            continue
        receipt = json.loads(path.read_text())
        expected_ids = {item["id"] for item in manifest["inputs"] if item["case"] == case}
        schema_valid = review_schema_ok(receipt.get("parsed", {}), expected_ids)
        raw = receipt.get("raw_response", {})
        totals.update({"review_" + key: value for key, value in raw.get("usage", {}).items()
                       if isinstance(value, int)})
        reference = context.get("human_reference")
        if reference is None:
            reference = "\n".join(item["text"] for item in manifest["inputs"] if item["case"] == case)
        reports = []
        for assessed in receipt.get("parsed", {}).get("reports", []):
            source = root / "generation" / f"{assessed['input_id']}.json"
            report_text = json.loads(source.read_text())["parsed"]["report_markdown"]
            findings = []
            for finding in assessed.get("findings", []):
                finding = dict(finding)
                finding["reference_quote_literal"] = finding.get("reference_quote", "") in reference
                finding["report_quote_literal"] = finding.get("report_quote", "") in report_text
                findings.append(finding)
            reports.append({**assessed, "findings": findings})
        review.append({"case": case, "status": receipt["status"], "schema_valid": schema_valid,
                       "human_reference": context.get("human_reference") is not None,
                       "reports": reports, "source_conflicts": receipt.get("parsed", {}).get("source_conflicts", [])})
    return {"interpretation": "Automated suggestions, not adjudicated error counts or clinical accuracy",
            "generation": generation, "reviews": review, "usage": dict(totals)}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    args = parser.parse_args()
    result = summarize(args.directory)
    (args.directory / "summary.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"reports": len(result["generation"]), "ok": sum(r["status"] == "ok" for r in result["generation"]),
                      "review_cases": len(result["reviews"]), "usage": result["usage"]}))
