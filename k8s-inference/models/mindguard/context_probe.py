#!/usr/bin/env python3
"""Measure native context boundaries with synthetic history, never clinical accuracy."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sys
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT.parents[1] / "components/control-plane/src"))

from fs2_serve.mindguard import MindGuardMessage, assess_mindguard  # noqa: E402


async def probe(model: str, endpoint: str) -> dict:
    sentence = "We discussed a routine day, writing in a journal and going for a quiet walk. "

    def messages(repetitions: int) -> list[dict]:
        return [{"role": "user", "content": "I want to reflect on my daily routine."},
                {"role": "assistant", "content": sentence * max(repetitions, 1)},
                {"role": "user", "content": "I feel safe today. I want to keep writing in my journal."}]

    async with httpx.AsyncClient(timeout=120) as client:
        models = (await client.get(endpoint.rstrip("/") + "/models")).json()["data"]
        served = next(item for item in models if item["id"] == model)
        if served["max_model_len"] != 32768:
            raise ValueError("context probe requires the native 32768-token profile")
        tokenize_url = endpoint.removesuffix("/v1") + "/tokenize"

        async def count(repetitions: int) -> int:
            response = await client.post(tokenize_url, json={"model": model, "messages": messages(repetitions),
                                                            "add_generation_prompt": True})
            response.raise_for_status()
            return response.json()["count"]

        base = await count(1)
        step = (await count(101) - base) // 100
        rows = []
        for target in [8192, 16384, 32740, 34000]:
            repetitions = max(1, 1 + (target - base) // step)
            payload = messages(repetitions)
            token_count = await count(repetitions)
            assessment = await assess_mindguard(
                [MindGuardMessage.model_validate(item) for item in payload],
                model_id=model, endpoint=endpoint, client=client,
            )
            expected = "completed" if token_count + 15 <= 32768 else "error"
            rows.append({"target_tokens": target, "actual_prompt_tokens": token_count,
                         "input_sha256": hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest(),
                         "expected_status": expected, "passed": assessment.status == expected,
                         "assessment": assessment.model_dump()})
        return {"model_id": model, "served_model": served, "probe_kind": "synthetic_context_boundary",
                "clinical_benchmark": False, "rows": rows, "passed": all(row["passed"] for row in rows)}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", choices=["mindguard-4b", "mindguard-8b"], required=True)
    parser.add_argument("--endpoint", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = asyncio.run(probe(args.model, args.endpoint))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps({"model_id": args.model, "passed": result["passed"],
                      "boundaries": [{"tokens": row["actual_prompt_tokens"], "status": row["assessment"]["status"],
                                      "latency_ms": row["assessment"]["latency_ms"]} for row in result["rows"]]}, indent=2))
    if not result["passed"]:
        raise SystemExit(2)
