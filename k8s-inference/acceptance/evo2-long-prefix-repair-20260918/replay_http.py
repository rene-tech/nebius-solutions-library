"""Replay the retained benign plant cases against an isolated candidate.

No retries: every original response or transport failure is retained. This
measures direct model HTTP execution, not customer queue or cold-start latency.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
import statistics
import time
import urllib.error
import urllib.request
import uuid


def write(path, data):
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")


def evaluate(case, data):
    sequence = data["sequence"]
    timings = data["elapsed_ms_per_token"]
    count = case["arguments"]["num_tokens"]
    truth = case["expected"]["reference_continuation"]
    return {
        "semantic_pass": (len(sequence) == count and not (set(sequence) - set("ACGTN"))
            and len(timings) == count and all(math.isfinite(value) and value >= 0 for value in timings)
            and data["logits"] is None and data["sampled_probs"] is None),
        "generated_bases": len(sequence),
        "gc_fraction": sum(base in "GC" for base in sequence) / max(1, len(sequence)),
        "reference_gc_fraction": sum(base in "GC" for base in truth) / len(truth),
        "positional_reference_identity": sum(a == b for a, b in zip(sequence, truth)) / len(truth),
        "first_token_ms": timings[0] if timings else None,
        "mean_remaining_token_ms": statistics.mean(timings[1:]) if len(timings) > 1 else None,
        "elapsed_ms": data["elapsed_ms"],
        "scientific_scope": "Public plant genome continuation; identity is descriptive, not functional validity or paper reproduction.",
    }


def summary(rows):
    by_shape = {}
    for row in rows:
        shape = f"{row['prefix_length']}+{row['num_tokens']}"
        by_shape.setdefault(shape, []).append(row)
    return {
        "requests": len(rows), "passed": sum(row["passed"] for row in rows),
        "failed": sum(not row["passed"] for row in rows),
        "by_shape": {shape: {
            "requests": len(items), "passed": sum(row["passed"] for row in items),
            "wall_seconds_median": statistics.median(row["wall_seconds"] for row in items),
            "wall_seconds_min": min(row["wall_seconds"] for row in items),
            "wall_seconds_max": max(row["wall_seconds"] for row in items),
        } for shape, items in by_shape.items()},
        "scope": "Isolated exact-image model HTTP replay; no automatic retries, no end-to-end queue claim.",
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", type=Path, required=True)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--repetitions", type=int, default=3)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    manifest = args.cases.read_bytes()
    cases = [case for case in json.loads(manifest)["cases"] if case["model_id"] == "evo2-40b"]
    shape_counts = {length: sum(len(case["arguments"]["sequence"]) == length for case in cases)
                    for length in (256, 1024, 4096, 8192)}
    if len(cases) != 32 or set(shape_counts.values()) != {8} or args.repetitions < 1:
        raise ValueError("expected all 32 retained Evo2 cases, eight per shape, and positive repetitions")
    # Start with the failing shape, then keep shorter requests as recovery checks.
    cases.sort(key=lambda case: (-len(case["arguments"]["sequence"]), case["case_id"]))
    write(args.output / "experiment.json", {
        "manifest_sha256": hashlib.sha256(manifest).hexdigest(), "cases": len(cases),
        "repetitions": args.repetitions, "base_url": args.base_url,
        "started_unix_seconds": time.time(), "warmup": "Prior explicit paired state/model comparison; no hidden HTTP warmup.",
    })
    rows = []
    for repetition in range(1, args.repetitions + 1):
        for case in cases:
            request_id = str(uuid.uuid4())
            request = urllib.request.Request(
                args.base_url.rstrip("/") + "/biology/arc/evo2/generate",
                data=json.dumps(case["arguments"]).encode(),
                headers={"Content-Type": "application/json", "X-Request-ID": request_id},
            )
            started = time.perf_counter()
            row = {"case_id": case["case_id"], "repetition": repetition,
                "request_id": request_id, "prefix_length": len(case["arguments"]["sequence"]),
                "num_tokens": case["arguments"]["num_tokens"], "seed": case["arguments"]["random_seed"],
                "passed": False}
            try:
                try:
                    response = urllib.request.urlopen(request, timeout=900)
                except urllib.error.HTTPError as error:
                    response = error
                with response:
                    row["status"] = response.status
                    raw = response.read()
                row["response"] = json.loads(raw)
                if row["status"] == 200:
                    row["evaluation"] = evaluate(case, row["response"])
                    row["passed"] = row["evaluation"]["semantic_pass"]
            except Exception as error:
                row["failure"] = {"type": type(error).__name__, "detail": str(error)}
            row["wall_seconds"] = time.perf_counter() - started
            write(args.output / f"r{repetition}-{case['case_id']}.json", row)
            rows.append(row)
            write(args.output / "summary.json", summary(rows))
            print(json.dumps({key: row[key] for key in ("case_id", "repetition", "passed", "wall_seconds")}), flush=True)
    raise SystemExit(0 if all(row["passed"] for row in rows) else 1)


if __name__ == "__main__":
    main()
