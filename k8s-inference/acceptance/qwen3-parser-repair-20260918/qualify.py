"""Bounded direct-runtime Qwen parser qualification; not public HTTP/MCP acceptance."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import time
from datetime import UTC, datetime
from pathlib import Path

import httpx


def combine_stream(text):
    content, reasoning, calls, finish, usage = [], [], {}, None, None
    done = False
    for line in text.splitlines():
        if not line.startswith("data: "):
            continue
        if line[6:] == "[DONE]":
            done = True
            continue
        chunk = json.loads(line[6:])
        if chunk.get("error"):
            raise ValueError("stream_error")
        usage = chunk.get("usage") or usage
        for choice in chunk.get("choices", []):
            delta = choice.get("delta", {})
            content.append(delta.get("content") or "")
            reasoning.append(delta.get("reasoning") or delta.get("reasoning_content") or "")
            finish = choice.get("finish_reason") or finish
            for call in delta.get("tool_calls", []):
                target = calls.setdefault(call["index"], {"function": {"name": "", "arguments": ""}})
                function = call.get("function") or {}
                for key in ("name", "arguments"):
                    target["function"][key] += function.get(key) or ""
    if not done:
        raise ValueError("stream_missing_done")
    return {
        "choices": [
            {
                "finish_reason": finish,
                "message": {
                    "content": "".join(content),
                    "reasoning": "".join(reasoning),
                    "tool_calls": [calls[i] for i in sorted(calls)],
                },
            }
        ],
        "usage": usage,
    }


def validate(result, expected):
    choice = result["choices"][0]
    if choice.get("finish_reason") not in ("stop", "tool_calls"):
        raise ValueError("incomplete_completion")
    message = choice["message"]
    if expected.get("kind", expected.get("evaluator")) == "chat_tool_call":
        calls = message.get("tool_calls") or []
        if len(calls) != 1 or calls[0]["function"]["name"] != expected["tool_name"]:
            raise ValueError("missing_or_wrong_tool")
        answer = json.loads(calls[0]["function"]["arguments"])
    else:
        answer = json.loads(message.get("content") or "")
    if not isinstance(answer, dict) or any(answer.get(k) != v for k, v in expected["reference_answer"].items()):
        raise ValueError("reference_answer_mismatch")
    if "<think>" in (message.get("content") or "") or "</think>" in (message.get("content") or ""):
        raise ValueError("reasoning_tags_in_answer")
    return {
        "passed": True,
        "finish_reason": choice["finish_reason"],
        "reasoning_characters": len(message.get("reasoning") or message.get("reasoning_content") or ""),
        "tool_calls": len(message.get("tool_calls") or []),
        "usage": result.get("usage"),
    }


def extra_cases():
    expected = {"sample_id": "SAMPLE-17", "count": 7}
    schema = {
        "type": "object",
        "properties": {"sample_id": {"type": "string"}, "count": {"type": "integer"}},
        "required": ["sample_id", "count"],
        "additionalProperties": False,
    }
    tool = {
        "type": "function",
        "function": {"name": "save_sample", "description": "Save the sample record.", "parameters": schema},
    }
    base = {
        "messages": [{"role": "user", "content": 'Return exactly JSON {"sample_id":"SAMPLE-17","count":7}. /no_think'}],
        "temperature": 0,
        "max_completion_tokens": 2048,
        "stream": False,
    }
    cases = []
    for mode in ("auto", "required", "named", "none", "json_object", "json_schema"):
        for stream in (False, True):
            request = copy.deepcopy(base)
            request["stream"] = stream
            want = {"kind": "chat_json", "reference_answer": expected}
            if mode in ("auto", "required", "named", "none"):
                request.update(
                    tools=[tool],
                    tool_choice=mode if mode != "named" else {"type": "function", "function": {"name": "save_sample"}},
                )
                if mode != "none":
                    request["messages"][0]["content"] = (
                        "Call save_sample with sample_id SAMPLE-17 and count 7. Do not answer in prose. /no_think"
                    )
                    want.update(kind="chat_tool_call", tool_name="save_sample")
            else:
                request["response_format"] = {"type": mode}
                if mode == "json_schema":
                    request["response_format"]["json_schema"] = {"name": "sample", "strict": True, "schema": schema}
            if stream:
                request["stream_options"] = {"include_usage": True}
            cases.append({"case_id": f"parser-{mode}-stream-{stream}", "arguments": request, "expected": want})
    for stream in (False, True):
        request = copy.deepcopy(base)
        request["messages"] = [
            {
                "role": "user",
                "content": "Calculate 17 times 13 plus 5. Return exactly a JSON object with integer key answer. /think",
            }
        ]
        request.update(stream=stream, response_format={"type": "json_object"})
        if stream:
            request["stream_options"] = {"include_usage": True}
        cases.append(
            {
                "case_id": f"parser-thinking-stream-{stream}",
                "arguments": request,
                "expected": {"kind": "chat_json", "reference_answer": {"answer": 226}},
            }
        )
    return cases


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--cases", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    manifest = json.loads(args.cases.read_bytes())
    cases = [c for c in manifest["cases"] if c["model_id"] == "qwen3-8b"]
    if len(cases) != 60:
        raise ValueError("expected_frozen_sixty_qwen_cases")
    cases += extra_cases()
    os.umask(0o077)
    args.output.mkdir(parents=True, exist_ok=False, mode=0o700)
    summary = []
    with httpx.Client(base_url=args.base_url, timeout=120, trust_env=False) as client:
        for cohort in (1, 2):
            for case in cases:
                request = copy.deepcopy(case["arguments"])
                for key in ("idempotency_key", "wait_seconds"):
                    request.pop(key, None)
                request["model"] = "qwen3-8b"
                started, clock = datetime.now(UTC).isoformat(), time.monotonic()
                response = client.post("/v1/chat/completions", json=request)
                row = {
                    "case_id": case["case_id"],
                    "cohort": cohort,
                    "at": started,
                    "elapsed_seconds": time.monotonic() - clock,
                    "http_status": response.status_code,
                    "request_sha256": hashlib.sha256(json.dumps(request, sort_keys=True).encode()).hexdigest(),
                }
                try:
                    response.raise_for_status()
                    result = combine_stream(response.text) if request.get("stream") else response.json()
                    row.update(validate(result, case["expected"]))
                except (ValueError, KeyError, IndexError, httpx.HTTPError) as error:
                    row.update(passed=False, error_type=type(error).__name__, error=str(error)[:200])
                receipt = {"request": request, "response": response.text, "expected": case["expected"], "receipt": row}
                (args.output / f"{cohort}-{case['case_id']}.json").write_text(json.dumps(receipt, indent=2) + "\n")
                summary.append(row)
                (args.output / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
                print(json.dumps(row), flush=True)
    if not all(r["passed"] for r in summary):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
