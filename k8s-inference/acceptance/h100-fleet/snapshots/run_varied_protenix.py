#!/usr/bin/env python3
"""Run one real varied public Protenix case and retain controller Job inputs.

Only private fixture copies change; the production route, recipe and original
public acceptance/artifact validation are reused without model mutations.
"""

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import threading


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for key in ("outputs", "input", "directory", "kubeconfig"):
        parser.add_argument("--" + key, type=Path, required=True)
    args = parser.parse_args()
    os.umask(0o077)
    args.directory.mkdir(parents=True, exist_ok=False)
    solution = Path(__file__).resolve().parents[3]
    sys.path.insert(0, str(solution / "acceptance/scientific-fleet"))
    from run_acceptance import PublicApiClient, RunConfig, run_acceptance

    activation = solution / "models/structure/batch-adapters/protenix-v2/activation"
    fragment = json.loads((activation / "public-acceptance.json").read_bytes())
    request = json.loads((activation / "public-request.json").read_bytes())
    manifest = json.loads((activation / "public-input-manifest.json").read_bytes())
    payload = args.input.read_bytes()
    pointer = manifest["entries"][0]["artifact"]
    pointer.update(sha256=hashlib.sha256(payload).hexdigest(), size_bytes=len(payload))
    manifest_bytes = (
        json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode()
    request["input_manifest"].update(
        sha256=hashlib.sha256(manifest_bytes).hexdigest(),
        size_bytes=len(manifest_bytes),
    )
    request["parameters"]["model_seeds"] = [101, 102]
    request["client_context"]["display_name"] = (
        "Snapshot qualification independent ubiquitin input"
    )
    fragment["public_fixtures"]["request"] = "request.json"
    for item in fragment["public_fixtures"]["supporting_inputs"]:
        item["path"] = (
            "manifest.json"
            if item["role"] == "request-input-manifest"
            else "input.json"
        )
    (args.directory / "request.json").write_text(json.dumps(request))
    (args.directory / "manifest.json").write_bytes(manifest_bytes)
    (args.directory / "input.json").write_bytes(payload)
    (args.directory / "fragment.json").write_text(json.dumps(fragment))
    stop = threading.Event()

    def observe():
        jobs = {}
        while not stop.is_set():
            observed = subprocess.run(
                [
                    "kubectl",
                    "--kubeconfig",
                    str(args.kubeconfig),
                    "--context",
                    "k8s-inference-h100",
                    "-n",
                    "fs2-models",
                    "get",
                    "jobs",
                    "-l",
                    "fs2.nebius.ai/model-id=protenix-v2",
                    "-o",
                    "json",
                ],
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            if observed.returncode == 0:
                for job in json.loads(observed.stdout)["items"]:
                    jobs[job["metadata"]["uid"]] = job
                (args.directory / "jobs-private.json").write_text(
                    json.dumps({"items": list(jobs.values())})
                )
            stop.wait(2)

    thread = threading.Thread(target=observe, daemon=True)
    thread.start()
    bundle = json.loads(args.outputs.read_bytes())
    origin = bundle["endpoints"]["inference_base_url"].removesuffix("/v1")
    try:
        receipt = run_acceptance(
            RunConfig(
                endpoint=origin,
                repository_root=args.directory,
                activation_fragment=args.directory / "fragment.json",
                receipt_path=args.directory / "public-receipt.json",
                run_id=args.directory.name,
                timeout_seconds=1800,
                poll_seconds=2,
            ),
            PublicApiClient(origin, bundle["credentials"]["scientific_access_token"]),
        )
        (args.directory / "public-receipt.json").write_text(
            json.dumps(receipt, indent=2)
        )
        print(json.dumps(receipt["terminal_state"]))
    finally:
        stop.set()
        thread.join(timeout=35)


if __name__ == "__main__":
    main()
