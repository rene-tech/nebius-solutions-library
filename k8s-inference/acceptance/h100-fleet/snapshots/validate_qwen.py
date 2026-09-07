#!/usr/bin/env python3
"""Two distinct deterministic requests against a Pod-local current Qwen server."""

from datetime import datetime, timezone
import argparse
import hashlib
import json
import time
import urllib.error
import urllib.request


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--fresh-inputs",
        action="store_true",
        help="questions never used to warm the donor",
    )
    args = parser.parse_args()
    started = time.monotonic()
    deadline = time.monotonic() + 300
    while True:
        try:
            with urllib.request.urlopen(
                "http://127.0.0.1:8000/health", timeout=10
            ) as response:
                assert response.status == 200
            break
        except (urllib.error.URLError, TimeoutError):
            if time.monotonic() > deadline:
                raise
            time.sleep(1)
    records = []
    questions = (
        ("What is 7 plus 5? Reply with only the integer.", "12"),
        ("What is 23 plus 19? Reply with only the integer.", "42"),
    )
    if args.fresh_inputs:
        questions = (
            ("What is 31 plus 17? Reply with only the integer.", "48"),
            ("What is 8 multiplied by 9? Reply with only the integer.", "72"),
        )
    for question, expected in questions:
        payload = {
            "model": "qwen3-8b",
            "messages": [{"role": "user", "content": question}],
            "temperature": 0,
            "max_tokens": 32,
            "chat_template_kwargs": {"enable_thinking": False},
        }
        request = urllib.request.Request(
            "http://127.0.0.1:8000/v1/chat/completions",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        before = time.monotonic()
        with urllib.request.urlopen(request, timeout=180) as response:
            result = json.load(response)
        answer = result["choices"][0]["message"]["content"].strip()
        records.append(
            {
                "input_sha256": hashlib.sha256(question.encode()).hexdigest(),
                "expected": expected,
                "answer": answer,
                "passed": answer == expected,
                "usage": result["usage"],
                "seconds": time.monotonic() - before,
            }
        )
    passed = all(record["passed"] for record in records)
    print(
        json.dumps(
            {
                "at": datetime.now(timezone.utc).isoformat(),
                "passed": passed,
                "requests": records,
                "total_seconds": time.monotonic() - started,
            }
        ),
        flush=True,
    )
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
