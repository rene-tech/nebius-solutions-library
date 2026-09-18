"""Offline acceptance gate; never infer a pass from an incomplete replay."""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import json
from pathlib import Path
import statistics


def load(path):
    return json.loads(path.read_text())


def distribution(values):
    values = sorted(values)
    return {"count": len(values), "min": min(values), "median": statistics.median(values),
            "max": max(values), "p95_nearest_rank": values[max(0, -(-95 * len(values) // 100) - 1)]}


def summarize(replay, evidence, baseline, expected_state_comparisons):
    rows = [load(path) for path in sorted(replay.glob("r*-evo2-*.json"))]
    experiment = load(replay / "experiment.json")
    counts = Counter(row["case_id"] for row in rows)
    repetitions = experiment["repetitions"]
    complete = (len(counts) == 32 and repetitions >= 3 and len(rows) == 32 * repetitions
                and set(counts.values()) == {repetitions})
    paired = load(evidence / "paired-model-proof.json")
    paired_ok = len(paired) == 9 and all(
        row["same_seeded_outputs"] and row["tiled_vs_baseline"]["bitwise_equal"] for row in paired)
    state_proof = load(evidence / "cuda-state-proof.json")
    states_ok = len(state_proof) == expected_state_comparisons and all(row["bitwise_equal"] for row in state_proof)
    by_shape, sequences = defaultdict(list), defaultdict(set)
    for row in rows:
        by_shape[(row["prefix_length"], row["num_tokens"])].append(row)
        if row["passed"]:
            sequences[row["case_id"]].add(row["response"]["sequence"])
    comparisons = []
    for row in rows:
        old_paths = list(baseline.glob(f"scientist-*/{row['case_id']}/result.json"))
        if old_paths and row["passed"]:
            if len(old_paths) != 1:
                raise ValueError("ambiguous original reference receipt")
            original = load(old_paths[0])
            comparisons.append({"case_id": row["case_id"], "repetition": row["repetition"],
                                "same_original_sequence": row["response"]["sequence"] == original["sequence"]})
    memory = [load(path) for path in sorted(evidence.glob("http-memory-*.json"))]
    memory_by_shape = defaultdict(list)
    for row in memory:
        memory_by_shape[(row["prefix_length"], row["num_tokens"])].append(row)
    shape_metrics = {}
    for shape, items in sorted(by_shape.items()):
        successful = [row for row in items if row["passed"]]
        shape_metrics[f"{shape[0]}+{shape[1]}"] = {
            "requests": len(items), "passed": len(successful),
            "http_wall_seconds": distribution([row["wall_seconds"] for row in items]),
            "first_token_ms": distribution([row["evaluation"]["first_token_ms"] for row in successful]) if successful else None,
            "positional_identity": distribution([row["evaluation"]["positional_reference_identity"] for row in successful]) if successful else None,
            "memory_records": len(memory_by_shape[shape]),
            "peak_allocated_bytes_per_gpu": {
                str(device): max((entry["memory"][device]["peak_allocated_bytes"] for entry in memory_by_shape[shape]), default=None)
                for device in (0, 1)
            },
        }
    baseline_ok = len(comparisons) == 24 * repetitions and all(row["same_original_sequence"] for row in comparisons)
    repeat_ok = len(sequences) == 32 and all(len(values) == 1 for values in sequences.values())
    return {
        "isolated_runtime_qualified": complete and paired_ok and states_ok and baseline_ok and repeat_ok
            and all(row["passed"] for row in rows) and len(memory) == len(rows),
        "customer_path_qualified": False,
        "scope": "Exact-image isolated 2-H100 runtime only; parent owns deployment/public authenticated MCP acceptance.",
        "complete_replay": complete, "requests": len(rows), "passed": sum(row["passed"] for row in rows),
        "repetitions": repetitions, "paired_full_model_bitwise_equal": paired_ok,
        "paired_cuda_state_bitwise_equal": states_ok, "identical_seeded_repeats": repeat_ok,
        "same_sequences_as_original_completed_requests": baseline_ok,
        "original_sequence_comparisons": comparisons,
        "shape_metrics": shape_metrics,
        "failures": [{key: row.get(key) for key in ("case_id", "repetition", "status", "failure")} for row in rows if not row["passed"]],
        "identity": load(evidence / "runtime-identity.json"),
        "scientific_limitations": "Plant sequence continuation only. Training overlap unknown; reference identity is not functional validity, variant-effect performance or paper reproduction.",
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--replay", type=Path, required=True)
    parser.add_argument("--server-evidence", type=Path, required=True)
    parser.add_argument("--baseline-cohort", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--expected-state-comparisons", type=int, choices=(6, 8), default=8)
    args = parser.parse_args()
    report = summarize(args.replay, args.server_evidence, args.baseline_cohort, args.expected_state_comparisons)
    with args.output.open("x") as stream:
        json.dump(report, stream, indent=2, sort_keys=True)
        stream.write("\n")
    print(json.dumps({key: report[key] for key in ("isolated_runtime_qualified", "requests", "passed")}))
    raise SystemExit(0 if report["isolated_runtime_qualified"] else 1)


if __name__ == "__main__":
    main()
