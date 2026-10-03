"""Expected native failure through either published output destination.

This verifies diagnostic publication only, not successful science or customer
bucket export. Use the same idempotency key to resume; never resubmit a failed
operation as new work. No key, quota, customer or cluster policy changes.
"""
import argparse
import asyncio
import importlib.util
import json
import os
from pathlib import Path
import time

import httpx2

from prepare import parameters


async def main(a):
    os.umask(0o077)
    a.output.mkdir(parents=True, exist_ok=True)
    spec = importlib.util.spec_from_file_location("acceptance", a.client_root / "scripts/scientific-batch-acceptance.py")
    h = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(h)
    key = dict(line.split("=", 1) for line in a.qa_env.read_text().splitlines() if "=" in line)["SCIENTIFIC_MODELS_API_KEY"]
    if not key.startswith("fs2_pat_56130b22ae09"):
        raise ValueError("Only existing system/qa")
    idem = "fs2-mpinat-negative-" + a.run_label + "-" + a.output_destination + "-20261003"
    async with httpx2.AsyncClient(base_url="https://89.169.99.188", headers={"Authorization": "Bearer " + key}, timeout=90, trust_env=False) as http:
        request_path = a.output / "request.json"
        if not request_path.exists():
            content = h.FileSource(a.fixture / "input.tar.gz")
            provenance = json.loads((a.fixture / "provenance.json").read_text())
            if content.sha256 != provenance["bundle_sha256"]:
                raise ValueError("Input bundle differs from provenance")
            artifact = await h.upload(http, "gromacs", content, "application/x-tar", "gzip", idem + "-source")
            manifest = {"schema": "fs2-serve.nebius.ai/scientific-artifact-manifest/v1", "manifest_id": idem,
                        "entries": [{"name": "gromacs-inputs", "semantic_type": "gromacs-input-bundle/v1", "artifact": artifact}]}
            ref = await h.upload(http, "gromacs", h.canonical(manifest), "application/vnd.fs2.scientific-manifest+json", "none", idem + "-manifest")
            params = parameters({"id": "benchmem"})
            params.update(output_destination=a.output_destination, max_wall_seconds=300,
                          output_prefix="runs/" + idem)
            params["jobs"][0]["steps"] = [params["jobs"][0]["steps"][0],
                {"id": "expected-failure", "command": "check", "args": ["-f", "deliberately-missing.xtc"]}]
            request = {"schema": "fs2-serve.nebius.ai/scientific-run-request/v1", "operation": "run-workflow",
                       "service_class": "customer-batch", "input_manifest": ref, "parameters": params,
                       "client_context": {"display_name": "Internal QA expected diagnostic failure", "correlation_id": idem}}
            h.save(request_path, request)
        frozen = json.loads(request_path.read_text())
        if (frozen["parameters"]["output_destination"] != a.output_destination
                or frozen["client_context"]["correlation_id"] != idem):
            raise ValueError("Existing request has a different identity/destination; preserve it")
        response = await http.post("/v1/models/gromacs:submit", json=json.loads(request_path.read_text()), headers={"Idempotency-Key": idem})
        response.raise_for_status()
        h.save(a.output / "submission.json", response.json())
        operation = response.json()["operation"]["id"]
        h.save(a.output / "receipt.json", {"operation_id": operation,
            "output_destination": a.output_destination, "idempotency_key": idem})
        print(json.dumps({"operation_id": operation, "output_destination": a.output_destination}), flush=True)
        deadline = time.monotonic() + 420
        while time.monotonic() < deadline:
            response = await http.get("/v1/operations/" + operation)
            response.raise_for_status()
            status = response.json()
            h.save(a.output / "status.json", status)
            if status["operation"]["status"] in {"succeeded", "failed", "cancelled", "expired", "preempted"} and status["batch"]["result_published"]:
                break
            await asyncio.sleep(5)
        else:
            raise RuntimeError("Observation deadline; retain operation ID and resume, do not resubmit")
        response = await http.get("/v1/operations/" + operation + "/result")
        response.raise_for_status()
        result = response.json()
        h.save(a.output / "result.json", result)
        if result["terminal_status"] != "failed" or result.get("semantic_validation", {}).get("status") == "passed":
            raise ValueError("Expected scientific failure, not success")
        if not result.get("output_manifest"):
            raise ValueError("Native failure diagnostics not published")
        collected = await h.collect_manifest(http, result["output_manifest"], a.output / "diagnostics", diagnostics=True)
        texts = [Path(x["path"]).read_text(errors="replace") for x in collected["verified_artifacts"]]
        if not any("deliberately-missing.xtc" in t and ("does not exist" in t or "not found" in t) for t in texts):
            raise ValueError("Original native missing-file explanation absent")
        h.save(a.output / "verification.json", {"operation_id": operation, "diagnostics_verified": True,
               "science_passed": False, "output_destination": a.output_destination,
               "customer_bucket_export_tested": False,
               "bucket_verification_note": "Use recover_bucket.py to verify retained remote checkpoints separately.",
               **collected})
        print(json.dumps({"operation_id": operation, "diagnostics_verified": True, "science_passed": False}), flush=True)


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    for name in ("qa-env", "client-root", "fixture", "output"):
        p.add_argument("--" + name, type=Path, required=True)
    p.add_argument("--output-destination", choices=("platform-artifacts", "customer-bucket"),
                   default="platform-artifacts")
    p.add_argument("--run-label", default="v2", choices=("v2", "v3", "v4"))
    asyncio.run(main(p.parse_args()))
