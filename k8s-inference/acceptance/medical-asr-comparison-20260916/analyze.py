#!/usr/bin/env python3
"""Aggregate every receipt, retaining failures and not inflating corpus size by repeats."""

import argparse
import json
import statistics
from collections import defaultdict
from pathlib import Path


def aggregate(rows):
    complete = [r for r in rows if "quality" in r]
    keys = ("reference_words", "substitutions", "deletions", "insertions", "reference_characters", "character_errors")
    counts = {key: sum(r["quality"][key] for r in complete) for key in keys}
    return {
        "requests": len(rows), "scored": len(complete),
        "succeeded": sum(r["status"] == "succeeded" for r in rows),
        "failed_or_empty": sum(r["status"] not in ("succeeded", "started") for r in rows),
        "pending": sum(r["status"] == "started" for r in rows),
        **counts,
        "wer": sum(counts[k] for k in ("substitutions", "deletions", "insertions")) / counts["reference_words"] if counts["reference_words"] else None,
        "cer": counts["character_errors"] / counts["reference_characters"] if counts["reference_characters"] else None,
        "median_wall_seconds": statistics.median(r["wall_seconds"] for r in complete) if complete else None,
        "min_wall_seconds": min((r["wall_seconds"] for r in complete), default=None),
        "max_wall_seconds": max((r["wall_seconds"] for r in complete), default=None),
        "summed_wall_seconds": sum(r["wall_seconds"] for r in complete),
        "audio_seconds": sum(r["audio_seconds"] for r in rows),
        "complete_duration_count": sum(r.get("full_duration", False) for r in rows),
        "unique_transcripts": len({r.get("text", "") for r in complete}),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("results", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    rows = [json.loads(p.read_text()) for p in sorted(args.results.glob("*-r*.json"))]
    groups = defaultdict(list)
    for row in rows:
        groups[(row["model"], row["case"])].append(row)
    by_case = [{"model": model, "case": case, **aggregate(items)} for (model, case), items in groups.items()]
    by_model = []
    for model in sorted({r["model"] for r in rows}):
        for language in ("en", "de"):
            # Quality corpus counts each unique recording ONCE, using first attempt.
            selected = [r for r in rows if r["model"] == model and r["language"] == language and r["repetition"] == 1]
            if selected:
                by_model.append({"model": model, "language": language, **aggregate(selected)})
    report = {"quality_policy": "Unique clips, first attempt; failures/empty transcripts scored as deletions. Repeats reported per case, not extra quality samples.",
              "by_model": by_model, "by_case": by_case}
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
