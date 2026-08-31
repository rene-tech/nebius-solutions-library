#!/usr/bin/env python3
"""Run two deterministic OpenAI-compatible semantic checks and write a receipt."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path


MODEL = "Qwen/Qwen3-8B"
REVISION = "b968826d9c46dd6066d109eabc6255188de91218"
CASES = (
    {
        "id": "sky-color",
        "prompt": (
            "Disable thinking. Reply with exactly one uppercase English word for "
            "the color of a clear daytime sky. /no_think"
        ),
        "expected": "BLUE",
    },
    {
        "id": "integer-addition",
        "prompt": (
            "Disable thinking. Compute 19 + 23 and reply with only the decimal "
            "integer. /no_think"
        ),
        "expected": "42",
    },
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def request_json(url: str, payload: dict[str, object] | None = None) -> tuple[dict, float]:
    body = None if payload is None else json.dumps(payload, separators=(",", ":")).encode()
    request = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json"} if body is not None else {},
        method="POST" if body is not None else "GET",
    )
    started = time.monotonic()
    with urllib.request.urlopen(request, timeout=120) as response:
        result = json.loads(response.read())
    return result, time.monotonic() - started


def semantic_match(case: dict[str, str], content: str) -> bool:
    normalized = content.strip().strip(".!").upper()
    if case["id"] == "sky-color":
        return normalized == "BLUE"
    return normalized == "42" or bool(re.fullmatch(r"42\.?", content.strip()))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--attempt", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    models, models_latency = request_json(f"{args.base_url}/v1/models")
    served_ids = [item.get("id") for item in models.get("data", [])]
    if MODEL not in served_ids:
        raise RuntimeError(f"exact served model missing: {served_ids}")

    cases = []
    for case in CASES:
        payload = {
            "model": MODEL,
            "messages": [{"role": "user", "content": case["prompt"]}],
            "temperature": 0,
            "max_tokens": 8,
            "chat_template_kwargs": {"enable_thinking": False},
        }
        response, latency = request_json(f"{args.base_url}/v1/chat/completions", payload)
        content = response["choices"][0]["message"]["content"]
        passed = semantic_match(case, content)
        cases.append(
            {
                "id": case["id"],
                "request_sha256": hashlib.sha256(
                    json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
                ).hexdigest(),
                "expected": case["expected"],
                "content": content,
                "response_model": response.get("model"),
                "finish_reason": response["choices"][0].get("finish_reason"),
                "usage": response.get("usage"),
                "latency_seconds": latency,
                "passed": passed,
            }
        )
    if cases[0]["content"] == cases[1]["content"]:
        raise RuntimeError("semantic responses are not distinct")
    if not all(case["passed"] for case in cases):
        raise RuntimeError(f"semantic check failed: {cases}")

    receipt = {
        "schema_version": 1,
        "attempt": args.attempt,
        "completed_at": utc_now(),
        "model": MODEL,
        "revision": REVISION,
        "models_latency_seconds": models_latency,
        "served_model_ids": served_ids,
        "cases": cases,
        "passed": True,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(receipt, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
