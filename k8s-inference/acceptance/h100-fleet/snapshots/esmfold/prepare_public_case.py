#!/usr/bin/env python3
"""Retain a real varied public ESM workflow and its controller-issued Job.

This runs the existing customer acceptance scenario unchanged. Credentials and
Pod/Job specifications stay in a private output directory; only the validated
result summary is printed. No production configuration is changed.
"""

import argparse
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import subprocess
import sys
import time


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("outputs", "kubeconfig", "directory"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--model", choices=("esmfold2", "esmfold2-fast"), required=True)
    parser.add_argument("--run-id", required=True)
    args = parser.parse_args()
    os.umask(0o077)
    args.directory.mkdir(parents=True, exist_ok=False)
    solution = Path(__file__).resolve().parents[4]
    sys.path.insert(0, str(solution / "acceptance/scientific-fleet"))
    import run_acceptance as public
    import run_fleet_acceptance as fleet
    import run_scenario_acceptance as scenarios

    bundle = json.loads(args.outputs.read_bytes())
    scenario = next(item for item in json.loads(
        (solution / "acceptance/scientific-fleet/scenarios/customer-readiness.json").read_bytes()
    ) if item["model_id"] == args.model)
    config = public.RunConfig(
        endpoint=bundle["endpoints"]["inference_base_url"].removesuffix("/v1"),
        repository_root=solution,
        activation_fragment=next(item.path for item in fleet.discover_inputs(solution)
                                 if item.model_id == args.model),
        receipt_path=args.directory / "public-result.json", run_id=args.run_id,
        timeout_seconds=1800,
    )
    retained = {}
    kube = ["kubectl", "--kubeconfig", str(args.kubeconfig), "--context", "k8s-inference-h100",
            "-n", "fs2-models"]
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(scenarios.run_scenario, config, scenario,
                                 bundle["credentials"]["scientific_access_token"])
        while not future.done():
            submitted = args.directory / "public-result.submitted.json"
            if submitted.exists():
                operation = json.loads(submitted.read_bytes())["operation_id"]
                observed = subprocess.run([*kube, "get", "jobs", "-l",
                    "fs2.nebius.ai/operation-id=" + operation, "-o", "json"],
                    capture_output=True, text=True, check=True)
                for job in json.loads(observed.stdout)["items"]:
                    retained[job["metadata"]["name"]] = job
                (args.directory / "jobs-private.json").write_text(json.dumps({"items": list(retained.values())}))
            time.sleep(2)
        result = future.result()
    print(json.dumps({key: result.get(key) for key in ("id", "model_id", "operation_id", "outcome", "wall_seconds", "error_code")}))
    return int(result.get("outcome") != "passed")


if __name__ == "__main__":
    raise SystemExit(main())
