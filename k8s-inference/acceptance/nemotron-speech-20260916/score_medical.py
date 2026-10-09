"""Score retained medical-probe JSONL against attributed English TextGrids.

English WER is approximate for a mixed conversation: overlapping speaker turns
are ordered by interval onset, not by a human word-level interleaving. The
normalizer lowercases, removes annotation tags and ignores punctuation; it does
not remove hesitations or correct recognized words. No German reference exists
in this bundle, so German accuracy is explicitly unscored.
"""

import argparse
import hashlib
import json
import re
import unicodedata
from array import array
from pathlib import Path


def words(text):
    text = re.sub(r"<[^>]*>", " ", text)
    return re.findall(r"[^\W_]+(?:['’][^\W_]+)*", unicodedata.normalize("NFKC", text).lower())


def reference(directory, case):
    number = {"en-01": "01", "en-02": "02"}[case]
    turns, files = [], []
    for speaker in ("doctor", "patient"):
        path = directory / f"references/en/day1_consultation{number}_{speaker}.TextGrid"
        raw = path.read_text()
        files.append({"path": str(path.relative_to(directory)), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
        for match in re.finditer(
            r'intervals \[\d+\]:\s*xmin = ([\d.]+)\s*xmax = ([\d.]+)\s*text = "((?:""|[^"])*)"', raw,
        ):
            text = match[3].replace('""', '"').strip()
            if text:
                turns.append((float(match[1]), float(match[2]), speaker, text))
    if not turns:
        raise ValueError("no reference transcript intervals were parsed")
    turns.sort()
    overlap_count = sum(1 for a, b in zip(turns, turns[1:]) if a[1] > b[0])
    return " ".join(item[3] for item in turns), {"files": files, "overlapping_adjacent_turns": overlap_count}


def alignment(reference_text, hypothesis):
    source, target = words(reference_text), words(hypothesis)
    if not source:
        raise ValueError("cannot score an empty reference")
    matrix = [array("I", range(len(target) + 1))]
    for i, expected in enumerate(source, 1):
        row, previous = array("I", [i]), matrix[-1]
        for j, actual in enumerate(target, 1):
            row.append(min(previous[j] + 1, row[j - 1] + 1, previous[j - 1] + (expected != actual)))
        matrix.append(row)
    i, j = len(source), len(target)
    counts = {"substitutions": 0, "deletions": 0, "insertions": 0, "correct": 0}
    errors = []
    while i or j:
        if i and j and matrix[i][j] == matrix[i - 1][j - 1] + (source[i - 1] != target[j - 1]):
            if source[i - 1] == target[j - 1]:
                counts["correct"] += 1
            else:
                counts["substitutions"] += 1
                errors.append({"type": "substitution", "reference_index": i - 1,
                               "reference": source[i - 1], "recognized": target[j - 1]})
            i, j = i - 1, j - 1
        elif i and matrix[i][j] == matrix[i - 1][j] + 1:
            counts["deletions"] += 1
            errors.append({"type": "deletion", "reference_index": i - 1, "reference": source[i - 1]})
            i -= 1
        else:
            counts["insertions"] += 1
            errors.append({"type": "insertion", "reference_index": i, "recognized": target[j - 1]})
            j -= 1
    return {"reference_words": len(source), "hypothesis_words": len(target),
            "wer": matrix[-1][-1] / len(source), **counts, "errors": list(reversed(errors))}


def report(assets, logs):
    runs = []
    for path in logs:
        rows, identity = [], None
        for line in path.read_text().splitlines():
            if not line.startswith('{"event":'):
                continue
            row = json.loads(line)
            if row["event"] == "medical_probe_start":
                identity = row
            elif row["event"] == "medical_measurement":
                rows.append(row)
        for row in rows:
            score = None
            if row["case"].startswith("en-"):
                transcript, provenance = reference(assets, row["case"])
                score = {**alignment(transcript, row["text"]), **provenance,
                         "interpretation": "approximate mixed-speaker WER; onset-ordered overlapping turns"}
            row["quality"] = score
            row["quality_limitation"] = ("German has no verified human reference; accuracy is not scored"
                                         if score is None else "Not a clinical validation or speaker attribution score")
            row["run_identity"] = identity
            row["evidence_file"] = path.name
            file_mode = next((other for other in rows if other["case"] == row["case"] and
                              other["mode"] == "complete-file"), None)
            if row["mode"] == "paced-stream" and file_mode:
                row["file_stream_same_normalized_text"] = words(row["text"]) == words(file_mode["text"])
            runs.append(row)
    return {"normalization": "NFKC/lowercase; remove XML annotation tags; word tokens including apostrophes; "
                             "no spelling, number or hesitation correction",
            "german_reference_available": False, "customer_acceptance": False, "measurements": runs}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--assets", type=Path, required=True)
    parser.add_argument("--logs", type=Path, nargs="+", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = report(args.assets, args.logs)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    for row in result["measurements"]:
        print(json.dumps({"case": row["case"], "model": row["run_identity"]["model"], "mode": row["mode"],
                          "audio_seconds": row["audio_seconds"], "processing_seconds": row["processing_seconds"],
                          "first_partial_seconds": row.get("first_partial_seconds"),
                          "wer": row["quality"]["wer"] if row["quality"] else None}, ensure_ascii=False))


if __name__ == "__main__":
    main()
