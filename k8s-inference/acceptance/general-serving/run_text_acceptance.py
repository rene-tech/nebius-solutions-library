"""Repeat exact text fixtures over the public, non-streaming serving API.

No replica, cache, or capacity policy is changed. These are observed workflow
latencies, not an isolated decode-throughput or cold-start qualification.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import time
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import httpx


def validate_result(result: dict, case: dict) -> dict:
    content = result["choices"][0]["message"]["content"]
    oracle = case["oracle"]
    if oracle["type"] != "exact-content" or content.strip() != oracle["expected"]:
        raise ValueError("text output did not match the fixture oracle")
    tokens = result["usage"]["completion_tokens"]
    if isinstance(tokens, bool) or not isinstance(tokens, int) or tokens <= 0:
        raise ValueError("runtime did not report a positive completion-token count")
    return {
        "output_sha256": hashlib.sha256(content.encode()).hexdigest(),
        "completion_tokens": tokens,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--fixture", type=Path, required=True)
    parser.add_argument("--receipt", type=Path, required=True)
    parser.add_argument("--repetitions", type=int, default=3)
    args = parser.parse_args()
    if args.repetitions < 1:
        parser.error("--repetitions must be positive")
    if args.receipt.exists():
        parser.error("use a new receipt path; preserve previous evidence")
    bundle = json.loads(args.bundle.read_text())
    token = bundle["credentials"]["inference_access_token"]
    origin = bundle["endpoints"]["admin_portal_url"].split("/admin")[0]
    raw_fixture = args.fixture.read_bytes()
    fixture = json.loads(raw_fixture)
    evidence = {
        "schema": "fs2-serve.nebius.ai/text-public-acceptance/v1",
        "started_at": datetime.now(UTC).isoformat(),
        "fixture_sha256": hashlib.sha256(raw_fixture).hexdigest(),
        "ttft_seconds": None,
        "ttft_reason": "Public gateway returns complete responses, not token streams.",
        "requests": [],
    }

    def save() -> None:
        encoded = json.dumps(evidence, indent=2, sort_keys=True) + "\n"
        if token in encoded:
            raise ValueError("credential detected in receipt")
        descriptor = os.open(args.receipt, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "w") as handle:
            handle.write(encoded)

    with httpx.Client(
        base_url=origin,
        timeout=60,
        trust_env=False,
        headers={"authorization": "Bearer " + token},
    ) as client:
        for repetition in range(args.repetitions):
            for case in fixture["requests"]:
                record = {"case": case["id"], "repetition": repetition + 1}
                evidence["requests"].append(record)
                started = time.monotonic()
                try:
                    response = client.post(
                        "/v1/chat/completions",
                        json=case["request"],
                        headers={
                            "Idempotency-Key": "text-ready-" + uuid4().hex,
                            "x-fs2-wait-seconds": "0",
                        },
                    )
                    response.raise_for_status()
                    operation = response.json()
                    operation_id = response.headers["x-fs2-operation-id"]
                    record["operation_id"] = operation_id
                    save()
                    while operation.get("status") not in {
                        "succeeded",
                        "failed",
                        "cancelled",
                        "expired",
                    }:
                        if time.monotonic() - started > 600:
                            raise TimeoutError("text operation deadline exceeded")
                        response = client.get(f"/v1/operations/{operation_id}")
                        response.raise_for_status()
                        operation = response.json()
                        if operation["status"] not in {
                            "succeeded",
                            "failed",
                            "cancelled",
                            "expired",
                        }:
                            time.sleep(0.5)
                    record["operation"] = {
                        key: operation.get(key)
                        for key in (
                            "status",
                            "model_id",
                            "model_revision",
                            "accepted_at",
                            "ready_at",
                            "started_at",
                            "completed_at",
                            "cold_start_seconds",
                            "runtime",
                            "attempt",
                        )
                    }
                    if operation["status"] != "succeeded":
                        raise ValueError("text operation did not succeed")
                    response = client.get(f"/v1/operations/{operation_id}/result")
                    response.raise_for_status()
                    record.update(validate_result(response.json(), case))
                    duration = (
                        datetime.fromisoformat(operation["completed_at"])
                        - datetime.fromisoformat(operation["accepted_at"])
                    ).total_seconds()
                    record["operation_end_to_end_seconds"] = duration
                    record["end_to_end_output_tokens_per_second"] = (
                        record["completion_tokens"] / duration
                    )
                    record["client_seconds"] = time.monotonic() - started
                    record["passed"] = True
                except Exception as error:
                    record["passed"] = False
                    record["failure_type"] = type(error).__name__
                    if isinstance(error, httpx.HTTPStatusError):
                        record["failure_http_status"] = error.response.status_code
                    save()
                    return 1
                save()
    evidence["result"] = "PASS"
    save()
    print(
        f"PASS: {len(evidence['requests'])} exact text outputs; receipt {args.receipt}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
