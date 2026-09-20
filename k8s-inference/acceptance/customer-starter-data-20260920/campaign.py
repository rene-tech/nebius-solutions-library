#!/usr/bin/env python3
"""Bounded real customer-key recipe execution; a model failure pauses its group.

No retry with a new idempotency key, no model configuration changes, and no
admin key is used. A resumed output directory polls its original operation.
"""

import argparse
import asyncio
import importlib.util
import json
import traceback
from datetime import UTC, datetime
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[2] / "starter-data/run_example.py"
spec = importlib.util.spec_from_file_location("starter_runner", SOURCE)
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)


def error_summary(error):
    if isinstance(error, BaseExceptionGroup):
        return [item for child in error.exceptions for item in error_summary(child)]
    result = {
        "type": type(error).__name__,
        "trace": [
            frame.name + ":" + str(frame.lineno)
            for frame in traceback.extract_tb(error.__traceback__)
        ],
    }
    if hasattr(error, "response"):
        result["http_status"] = error.response.status_code
    if (
        isinstance(error, (ValueError, RuntimeError))
        and str(error).replace("_", "").isalnum()
    ):
        result["code"] = str(error)[:128]
    return [result]


async def main(args):
    access = json.loads(args.key_file.read_bytes())
    assert access["tenant_id"] == "fs2-starter-data-acceptance-20260920"
    assert access["principal_id"] == "seed-canary"
    manifest = json.loads((args.pack / "manifest.json").read_bytes())
    groups = {}
    for case in manifest["cases"]:
        for recipe in json.loads((args.pack / case["recipes"]).read_bytes())["recipes"]:
            if (not args.models or recipe["model_id"] in args.models) and recipe[
                "model_id"
            ] not in args.exclude_models:
                groups.setdefault(recipe["model_id"], []).append(case["id"])
    args.output.mkdir(parents=True, exist_ok=True, mode=0o700)
    semaphore, results = asyncio.Semaphore(args.workers), []

    async def group(model, cases):
        async with semaphore:
            for case in cases:
                output = args.output / case / model
                print(
                    json.dumps(
                        {
                            "at": datetime.now(UTC).isoformat(),
                            "event": "start",
                            "model": model,
                            "case": case,
                        }
                    ),
                    flush=True,
                )
                try:
                    record = await runner.run(
                        args.pack,
                        case,
                        model,
                        output,
                        endpoint=access["origin"] + "/mcp",
                        key=access["secret"],
                        observe_seconds=args.observe_seconds,
                    )
                    result = {
                        "case_id": case,
                        "model_id": model,
                        "state": record["state"],
                        "operation_id": record.get("operation_id"),
                        "semantic_validation": record.get("semantic_validation"),
                        "completed_at": record.get("completed_at"),
                        "observation_seconds": record.get("observation_seconds"),
                    }
                except Exception as exc:
                    result = {
                        "case_id": case,
                        "model_id": model,
                        "state": "client-error",
                        "errors": error_summary(exc),
                    }
                    # Inputs are public fixtures, but exception text may include
                    # signed transport details. Keep console evidence bounded.
                results.append(result)
                runner.save(
                    args.output / "campaign-summary.json",
                    {"key_id": access["key_id"], "results": results},
                )
                print(json.dumps(result), flush=True)
                if result["state"] != "succeeded":
                    break

    await asyncio.gather(*(group(model, cases) for model, cases in groups.items()))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pack", type=Path, required=True)
    parser.add_argument("--key-file", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--models", nargs="*")
    parser.add_argument("--exclude-models", nargs="*", default=[])
    parser.add_argument("--workers", type=int, choices=(1, 2, 3), default=3)
    parser.add_argument("--observe-seconds", type=int, default=1800)
    asyncio.run(main(parser.parse_args()))
