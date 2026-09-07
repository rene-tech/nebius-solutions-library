#!/usr/bin/env python3
"""Run unchanged accepted structure requests; external observers measure startup.

This is a source-controlled composition of the existing public scientific
acceptance runner, not a replacement model runner or latency interpretation.
Credentials are read privately from the deployment output bundle.
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
import os
from pathlib import Path
import sys


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repository-root", type=Path, required=True)
    parser.add_argument("--outputs", type=Path, required=True)
    parser.add_argument("--receipts", type=Path, required=True)
    parser.add_argument("--models", nargs="+", required=True)
    parser.add_argument("--repetitions", type=int, default=3)
    parser.add_argument("--max-parallel", type=int, default=3)
    parser.add_argument("--run-id", required=True)
    args = parser.parse_args()
    allowed = {
        "esmfold2",
        "esmfold2-fast",
        "protenix-v2",
        "alphafold3",
        "openfold3-openbind",
    }
    if (
        not set(args.models) <= allowed
        or not 1 <= args.max_parallel <= 3
        or not 1 <= args.repetitions <= 3
    ):
        parser.error("choose structure profiles, 1..3 workers, and 1..3 repetitions")
    os.umask(0o077)
    sys.path.insert(0, str(args.repository_root / "acceptance/scientific-fleet"))
    import run_acceptance as public
    import run_fleet_acceptance as fleet
    import run_scenario_acceptance as scenarios

    bundle = json.loads(args.outputs.read_bytes())
    endpoint = bundle["endpoints"]["inference_base_url"].removesuffix("/v1")
    token = bundle["credentials"]["scientific_access_token"]
    fragments = {
        item.model_id: item.path for item in fleet.discover_inputs(args.repository_root)
    }
    args.receipts.mkdir(parents=True, exist_ok=True)

    def run_model(model: str) -> list[dict]:
        rows = []
        for repetition in range(1, args.repetitions + 1):
            case = {"id": f"{model}-r{repetition:02}", "model_id": model}
            config = public.RunConfig(
                endpoint=endpoint,
                repository_root=args.repository_root,
                activation_fragment=fragments[model],
                receipt_path=args.receipts / f"{case['id']}.json",
                run_id=f"{args.run_id}.{case['id']}",
                timeout_seconds=1800,
            )
            row = scenarios.run_scenario(config, case, token)
            print(
                json.dumps(
                    {
                        key: row.get(key)
                        for key in (
                            "id",
                            "model_id",
                            "operation_id",
                            "outcome",
                            "wall_seconds",
                            "error_code",
                        )
                    }
                ),
                flush=True,
            )
            rows.append(row)
        return rows

    rows = []
    with ThreadPoolExecutor(max_workers=args.max_parallel) as executor:
        for future in as_completed(
            [executor.submit(run_model, model) for model in args.models]
        ):
            rows.extend(future.result())
    public._write_receipt(
        args.receipts / "public-acceptance-summary.json",
        {
            "run_id": args.run_id,
            "rows": rows,
            "clock_note": "Request completion durations are not model-ready startup measurements.",
        },
        overwrite=False,
    )
    return int(any(row.get("outcome") != "passed" for row in rows))


if __name__ == "__main__":
    raise SystemExit(main())
