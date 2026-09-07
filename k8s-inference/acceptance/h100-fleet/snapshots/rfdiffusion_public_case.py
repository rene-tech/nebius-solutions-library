#!/usr/bin/env python3
"""Submit one normal 96-residue RF request and retain its real controller Job."""

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
    for key in ("outputs", "kubeconfig", "directory"):
        parser.add_argument("--" + key, type=Path, required=True)
    args = parser.parse_args()
    os.umask(0o077)
    args.directory.mkdir(parents=True, exist_ok=False)
    root = Path(__file__).resolve().parents[3]
    sys.path.insert(0, str(root / "acceptance/scientific-fleet"))
    import run_acceptance as public
    import run_fleet_acceptance as fleet
    import run_scenario_acceptance as scenarios

    bundle = json.loads(args.outputs.read_bytes())
    scenario = {"id": "rfdiffusion-snapshot-96-residues", "model_id": "rfdiffusion",
                "service_class": "customer-batch", "parameters": {"num_designs": 1, "contigs": ["96-96"], "seed": 8200},
                "expected_shards": {"inference": 1}}
    config = public.RunConfig(endpoint=bundle["endpoints"]["inference_base_url"].removesuffix("/v1"),
        repository_root=root, activation_fragment=next(item.path for item in fleet.discover_inputs(root)
            if item.model_id == "rfdiffusion"), receipt_path=args.directory / "public-result.json",
        run_id="rfdiffusion-snapshot-96-r20260907", timeout_seconds=1800)
    kube = ["kubectl", "--kubeconfig", str(args.kubeconfig), "--context", "k8s-inference-h100", "-n", "fs2-models"]
    retained = {}
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(scenarios.run_scenario, config, scenario, bundle["credentials"]["scientific_access_token"])
        while not future.done():
            submitted = args.directory / "public-result.submitted.json"
            if submitted.exists():
                operation = json.loads(submitted.read_bytes())["operation_id"]
                observed = subprocess.check_output([*kube, "get", "jobs", "-l", "fs2.nebius.ai/operation-id=" + operation,
                                                    "-o", "json"])
                for job in json.loads(observed)["items"]:
                    retained[job["metadata"]["name"]] = job
                (args.directory / "jobs-private.json").write_text(json.dumps({"items": list(retained.values())}))
            time.sleep(2)
        result = future.result()
    print(json.dumps({key: result.get(key) for key in ("id", "model_id", "operation_id", "outcome", "wall_seconds", "error_code")}))
    return int(result.get("outcome") != "passed")


if __name__ == "__main__":
    raise SystemExit(main())
