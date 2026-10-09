#!/usr/bin/env python3
"""Verify a completed, unfiltered 49-request medical comparison and its receipts."""

import argparse
import gzip
import hashlib
import json
from collections import Counter, defaultdict
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument("--historical-german", type=Path, required=True)
    args = parser.parse_args()
    directory = args.directory
    run = json.loads((directory / "results-r1/run.json").read_text())
    assert run["key_revoked"] is True
    assert run["cohorts"] == [6, 6, 37]
    assert run["finished_at"] > run["started_at"]
    rows = [json.loads(p.read_text()) for p in sorted((directory / "results-r1").glob("*-r*.json"))]
    assert len(rows) == 49
    expected = {
        (model, case, repetition)
        for model in run["models"] for case in ("en-01", "en-02") for repetition in (1, 2, 3)
    } | {
        ("nemotron-speech-multilingual-0-6b", f"de-{index:04}", 1)
        for index in {i * 1090 // 29 for i in range(30)} | {193}
    }
    assert {(r["model"], r["case"], r["repetition"]) for r in rows} == expected
    assert len({r["operation_id"] for r in rows}) == 49
    cases = {c["case"]: c for c in run["cases"]}
    workers = defaultdict(Counter)
    for row in rows:
        assert row["status"] != "started" and "quality" in row
        assert row["reference"] == cases[row["case"]]["reference"]
        assert row["source_sha256"] == cases[row["case"]]["source_sha256"]
        assert row["full_duration"] is True
        stem = f"{row['model']}-{row['case']}-r{row['repetition']}"
        with gzip.open(directory / "results-r1" / f"{stem}.result.json.gz", "rt") as handle:
            result = json.load(handle)
        assert hashlib.sha256(json.dumps(result, sort_keys=True).encode()).hexdigest() == row["raw_result_sha256"]
        assert result["text"] == row["text"]
        assert row["operation"]["id"] == row["operation_id"]
        assert row["operation"]["tenant_id"] == "rene"
        # Preserve the complete runtime mapping without assuming a provider-specific schema.
        workers[row["model"]][json.dumps(row["operation"]["runtime"], sort_keys=True)] += 1

    german = {int(r["case"].split("-")[1]): r for r in rows if r["language"] == "de"}
    historical = {}
    with args.historical_german.open() as handle:
        for line in handle:
            item = json.loads(line)
            if item.get("repetition") == 0 and item.get("case") in german:
                historical[item["case"]] = item["result"]["text"]
    assert historical.keys() == german.keys()
    details = json.loads((directory / "medical-details.json").read_text())
    secondary = defaultdict(lambda: {"errors": 0, "reference_words": 0})
    for row in details["rows"]:
        secondary[row["model"]]["errors"] += row["hesitation_normalized_errors"]
        secondary[row["model"]]["reference_words"] += row["hesitation_normalized_reference_words"]
    for counts in secondary.values():
        counts["wer"] = counts["errors"] / counts["reference_words"]
    report = {
        "verification": "complete: all expected requests present, full decoded durations, hashes and operation receipts agree; temporary key revoked",
        "started_at": run["started_at"], "finished_at": run["finished_at"],
        "requests": len(rows), "status_counts": dict(Counter(r["status"] for r in rows)),
        "transport_operation_status_counts": dict(Counter(r["operation"]["status"] for r in rows)),
        "german_matching_historical_texts": sum(r["text"] == historical[i] for i, r in german.items()),
        "german_differing_cases": [i for i, r in german.items() if r["text"] != historical[i]],
        "secondary_hesitation_normalized_english": dict(secondary),
        "worker_receipts": {model: [{"runtime": json.loads(runtime), "requests": count}
                                   for runtime, count in counts.items()] for model, counts in workers.items()},
    }
    (directory / "verification.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({key: value for key, value in report.items() if key != "worker_receipts"}, indent=2))


if __name__ == "__main__":
    main()
