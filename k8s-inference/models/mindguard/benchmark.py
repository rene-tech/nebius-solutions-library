#!/usr/bin/env python3
"""Reproducible classifier measurements; reports contain no input transcripts.

Sources: pinned Sword testset, pinned model-card example, or supplied MindEval JSONL.
Local representative cases are functional probes, not clinical accuracy evidence.
"""

from __future__ import annotations

import argparse
import ast
import asyncio
import hashlib
import json
import math
import os
import re
import statistics
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT.parents[1] / "components/control-plane/src"))

from fs2_serve.mindguard import MindGuardMessage, assess_mindguard  # noqa: E402


def load_cases(source: str, model_id: str, path: Path | None = None) -> list[dict[str, Any]]:
    lock = json.loads((ROOT / "public-models.lock.json").read_text())
    if source in {"representative", "mindeval"}:
        source_path = path if source == "mindeval" else ROOT / "representative.jsonl"
        if source_path is None:
            raise ValueError("--input is required for MindEval JSONL")
        cases = [json.loads(line) for line in source_path.read_text().splitlines() if line.strip()]
        if source == "mindeval":
            prefixes = []
            for conversation in cases:
                for index, message in enumerate(conversation["messages"]):
                    if message["role"] != "user":
                        continue
                    expected = conversation.get("expected_by_user_index", {}).get(str(index), {})
                    prefixes.append({"id": f"{conversation['id']}-user-{index}",
                                     "messages": conversation["messages"][:index + 1],
                                     "expected_safety": expected.get("safety"),
                                     "expected_categories": expected.get("categories")})
            cases = prefixes
    elif source == "model-card":
        from huggingface_hub import hf_hub_download

        model = lock["models"][model_id]
        card = Path(hf_hub_download(model["repo_id"], "README.md", revision=model["revision"])).read_text()
        match = re.search(r"conversation\s*=\s*(\[.*?\n\s*\])", card, re.DOTALL)
        if not match:
            raise ValueError("pinned model-card example could not be located")
        cases = [{"id": "sword-model-card-example", "messages": ast.literal_eval(match.group(1)),
                  "expected_safety": "unsafe", "expected_categories": ["S2"]}]
    else:
        from huggingface_hub import hf_hub_download
        import pyarrow.parquet as pq

        dataset = lock["testset"]
        filename = hf_hub_download(dataset["repo_id"], dataset["filename"],
                                   repo_type="dataset", revision=dataset["revision"])
        rows = pq.read_table(filename).to_pylist()
        if len(rows) != dataset["expected_rows"]:
            raise ValueError("pinned Sword testset row count changed")
        labels = {"safe": ("safe", []), "self_harm": ("unsafe", ["S1"]), "harm_others": ("unsafe", ["S2"])}
        cases = []
        for index, row in enumerate(rows):
            safety, categories = labels[row["label"]]
            cases.append({"id": f"sword-testset-{index:04d}",
                          "messages": [*row["prompt"], {"role": "user", "content": row["user_message"]}],
                          "expected_safety": safety, "expected_categories": categories})
    if not cases:
        raise ValueError("no benchmark cases")
    for case in cases:
        if not isinstance(case.get("id"), str) or not case["id"]:
            raise ValueError("each case requires a nonempty id")
        case["messages"] = [MindGuardMessage.model_validate(message).model_dump() for message in case["messages"]]
        if not case["messages"] or case["messages"][-1]["role"] != "user":
            raise ValueError("each benchmark case must end at the evaluated user turn")
        if case.get("expected_safety") not in {None, "safe", "unsafe"}:
            raise ValueError("expected_safety must be safe, unsafe or omitted")
    return cases


def summarize(rows: list[dict[str, Any]], elapsed: float) -> dict[str, Any]:
    measured = [row for row in rows if row["phase"] == "warm"]
    completed = [row for row in measured if row["assessment"]["status"] == "completed"]
    latencies = sorted(row["assessment"]["latency_ms"] for row in completed)
    labeled = [row for row in completed if row["expected_safety"] is not None]
    category_labeled = [row for row in labeled if row["expected_categories"] is not None]
    safe = [row for row in labeled if row["expected_safety"] == "safe"]
    unsafe = [row for row in labeled if row["expected_safety"] == "unsafe"]
    false_positives = sum(row["assessment"]["safety"] == "unsafe" for row in safe)
    false_negatives = sum(row["assessment"]["safety"] == "safe" for row in unsafe)
    return {
        "warm_requests": len(measured), "completed": len(completed), "failed": len(measured) - len(completed),
        "completion_rate": len(completed) / len(measured) if measured else None,
        "labeled_completed": len(labeled),
        "accuracy_among_completed": sum(row["assessment"]["safety"] == row["expected_safety"]
                                        for row in labeled) / len(labeled) if labeled else None,
        "category_exact_match_among_completed": sum(
            sorted(row["assessment"]["categories"]) == sorted(row["expected_categories"])
            for row in category_labeled) / len(category_labeled) if category_labeled else None,
        "false_positives": false_positives, "true_negatives": len(safe) - false_positives,
        "false_negatives": false_negatives, "true_positives": len(unsafe) - false_negatives,
        "false_positive_rate_among_completed": false_positives / len(safe) if safe else None,
        "false_negative_rate_among_completed": false_negatives / len(unsafe) if unsafe else None,
        "latency_ms_mean": statistics.mean(latencies) if latencies else None,
        "latency_ms_p50": statistics.median(latencies) if latencies else None,
        "latency_ms_p95": latencies[math.ceil(.95 * len(latencies)) - 1] if latencies else None,
        "elapsed_warm_seconds": elapsed,
        "completed_requests_per_second": len(completed) / elapsed if elapsed else None,
        "auroc": None, "auroc_note": "Discrete model labels do not provide a calibrated continuous risk score.",
    }


async def run(args: argparse.Namespace) -> dict[str, Any]:
    cases = load_cases(args.source, args.model, args.input)
    if len(cases) * args.repeat < 10:
        raise ValueError("at least 10 measured requests are required; increase --repeat")
    lock = json.loads((ROOT / "public-models.lock.json").read_text())
    case_digest = hashlib.sha256(json.dumps(cases, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    rows: list[dict[str, Any]] = []
    semaphore = asyncio.Semaphore(args.concurrency)
    async with httpx.AsyncClient() as client:
        identity_response = await client.get(args.endpoint.rstrip("/") + "/models", timeout=30)
        identity_response.raise_for_status()
        runtime_identity = next(item for item in identity_response.json()["data"] if item["id"] == args.model)
        if runtime_identity.get("max_model_len") != lock["max_model_len"]:
            raise ValueError("runtime context limit differs from the pinned benchmark profile")
        if not str(runtime_identity.get("root", "")).endswith(lock["models"][args.model]["revision"]):
            raise ValueError("runtime model root does not identify the pinned snapshot")

        async def measure(case: dict[str, Any], phase: str, repetition: int) -> dict[str, Any]:
            async with semaphore:
                result = await assess_mindguard(
                    [MindGuardMessage.model_validate(message) for message in case["messages"]],
                    model_id=args.model, endpoint=args.endpoint, client=client,
                    api_key=os.environ.get("MINDGUARD_BENCHMARK_API_KEY"),
                )
            return {"case_id": case["id"], "phase": phase, "repetition": repetition,
                    "input_sha256": hashlib.sha256(json.dumps(case["messages"], sort_keys=True).encode()).hexdigest(),
                    "expected_safety": case.get("expected_safety"),
                    "expected_categories": case.get("expected_categories"),
                    "assessment": result.model_dump()}

        # First request is measured separately; pod/image/cache startup is separate evidence.
        rows.append(await measure(cases[0], "first_request", 0))
        for index in range(args.warmup):
            rows.append(await measure(cases[index % len(cases)], "warmup", index))
        started = time.perf_counter()
        rows.extend(await asyncio.gather(*[
            measure(case, "warm", repetition) for repetition in range(args.repeat) for case in cases
        ]))
        elapsed = time.perf_counter() - started
    return {"schema_version": 1, "created_at": datetime.now(UTC).isoformat(), "model_id": args.model,
            "model_revision": lock["models"][args.model]["revision"], "runtime_image": lock["runtime_image"],
            "runtime_identity": runtime_identity,
            "runtime_profile": {"dtype": lock["dtype"], "max_model_len": lock["max_model_len"],
                                "temperature": 0, "max_output_tokens": 15, "seed": 0},
            "source": args.source, "dataset_revision": lock["testset"]["revision"] if args.source == "testset" else None,
            "clinical_benchmark": args.source == "testset", "case_sha256": case_digest,
            "case_count": len(cases), "concurrency": args.concurrency, "repeat": args.repeat,
            "hardware_evidence": json.loads(args.hardware_evidence.read_text()) if args.hardware_evidence else None,
            "metrics": summarize(rows, elapsed), "requests": rows}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", choices=["mindguard-4b", "mindguard-8b"], required=True)
    parser.add_argument("--endpoint", required=True, help="operator-controlled runtime base URL ending /v1")
    parser.add_argument("--source", choices=["representative", "model-card", "testset", "mindeval"], required=True)
    parser.add_argument("--input", type=Path)
    parser.add_argument("--hardware-evidence", type=Path)
    parser.add_argument("--concurrency", type=int, choices=range(1, 33), default=1)
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--warmup", type=int, default=3)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.repeat < 1 or args.warmup < 0:
        parser.error("repeat must be positive and warmup nonnegative")
    report = asyncio.run(run(args))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report["metrics"], indent=2))
    if report["metrics"]["failed"]:
        raise SystemExit(2)


if __name__ == "__main__":
    main()
