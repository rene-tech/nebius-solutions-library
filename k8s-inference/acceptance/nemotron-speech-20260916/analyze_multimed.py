"""Summarize all retained German test cases without mixing in repeated samples."""

import argparse
import json
import statistics
from collections import Counter
from pathlib import Path

from score_medical import words


def distance(a, b):
    previous = list(range(len(b) + 1))
    for i, x in enumerate(a, 1):
        row = [i]
        for j, y in enumerate(b, 1):
            row.append(min(previous[j] + 1, row[j-1] + 1, previous[j-1] + (x != y)))
        previous = row
    return previous[-1]


def analyze(rows):
    full = [row for row in rows if row["repetition"] == 0]
    indexed = {row["case"]: row for row in full}
    scored = [row for row in full if row["quality"] is not None]
    chars, errors = 0, 0
    substitutions = Counter()
    for row in scored:
        reference = "".join(words(row["reference"]))
        hypothesis = "".join(words(row.get("result", {}).get("text", "")))
        chars += len(reference)
        errors += distance(reference, hypothesis)
        substitutions.update((error["reference"], error["recognized"]) for error in row["quality"]["errors"]
                             if error["type"] == "substitution")
    audio_seconds = sum(row.get("result", {}).get("audio_seconds", 0) for row in full)
    processing_seconds = sum(row.get("result", {}).get("processing_seconds", 0) for row in full)
    words_count = sum(row["quality"]["reference_words"] for row in scored)
    word_errors = sum(sum(row["quality"][key] for key in ("substitutions", "deletions", "insertions")) for row in scored)
    repetitions = [row for row in rows if row["repetition"] > 0]
    totals = [sum(row["wall_seconds"] for row in repetitions if row["repetition"] == i) for i in range(1, 4)]
    return {
        "full_split_requests": len(full), "unique_audio_sha256": len({row["input_sha256"] for row in full}),
        "http_failures": sum(row["http_status"] != 200 for row in full),
        "scored_reference_rows": len(scored), "reference_words": words_count,
        "word_errors": word_errors, "wer": word_errors/words_count,
        "character_normalization": "same word normalization, then remove whitespace; no compound/number correction",
        "reference_characters": chars, "character_errors": errors, "cer": errors/chars,
        "all_decoded_audio_seconds": audio_seconds, "all_processing_seconds": processing_seconds,
        "all_http_seconds": sum(row["wall_seconds"] for row in full),
        "processing_real_time_factor": processing_seconds/audio_seconds,
        "processing_times_faster_than_realtime": audio_seconds/processing_seconds,
        "blank_outputs": [{"case": row["case"], "reference": row["reference"], "audio_seconds": row["result"]["audio_seconds"]}
                          for row in full if not row.get("result", {}).get("text", "").strip()],
        "top_substitutions": [{"reference": a, "recognized": b, "count": count} for (a, b), count in substitutions.most_common(35)],
        "additional_warm_requests": len(repetitions),
        "repeated_transcripts_identical_to_full_pass": all(row["result"]["text"] == indexed[row["case"]]["result"]["text"] for row in repetitions),
        "repeated_30_clip_http_totals_seconds": totals,
        "repeated_total_mean_seconds": statistics.mean(totals),
        "repeated_total_sample_stdev_seconds": statistics.stdev(totals),
        "limitations": ["Private warmed worker, not public request/cold-start SLA", "Human-labeled according to dataset authors; no independent relabeling",
                        "No number equivalence, annotation-word removal, or spelling correction; not directly comparable to differently normalized published WER",
                        "Not clinical validation; one reference contains only an ellipsis and is unscorable"],
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = analyze([json.loads(line) for line in args.input.read_text().splitlines()])
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({key: value for key, value in result.items() if key not in {"top_substitutions"}}, ensure_ascii=False))
