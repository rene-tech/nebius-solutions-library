"""Run two existing seeded MD workflows through customer REST, without new keys.

Reuse the shipped client's upload and hash-verifying artifact implementation.
Receipts plus stable idempotency keys make interruption/resumption safe.
Credentials and raw evidence stay outside the repository.
"""
import argparse
import asyncio
import importlib.util
import json
import os
import sys
import time
from pathlib import Path

import httpx2


async def main(args):
    os.umask(0o077)
    sys.path.insert(0, str(args.client_root))
    spec = importlib.util.spec_from_file_location(
        "scientific_acceptance", args.client_root / "scripts/scientific-batch-acceptance.py",
    )
    helper = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(helper)
    values = dict(line.split("=", 1) for line in args.qa_env.read_text().splitlines() if "=" in line)
    key = values["SCIENTIFIC_MODELS_API_KEY"]
    if not key.startswith("fs2_pat_56130b22ae09"):
        raise ValueError("Use the existing system/qa key, never the Lynx customer key")
    async with httpx2.AsyncClient(
        base_url=args.origin, headers={"Authorization": "Bearer " + key}, timeout=90, trust_env=False,
    ) as http:
        models = helper.check(await http.get("/v1/scientific-models")).json()
        helper.save(args.output / "scientific-catalog.json", models)
        for cohort in (1, 2):
            out = args.output / f"cohort-{cohort}"
            out.mkdir(parents=True, exist_ok=True)
            receipt_path = out / "receipt.json"
            receipt = helper.load_receipt(receipt_path) or {}
            if receipt.get("state") == "verified":
                continue
            idem = f"route-expiry-20261003-gromacs-{cohort}"
            started = time.monotonic()
            if "operation_id" not in receipt:
                source = helper.FileSource(args.fixture / "input.tar.gz")
                artifact = await helper.upload(http, "gromacs", source, "application/x-tar", "gzip", idem + "-source")
                manifest = {
                    "schema": "fs2-serve.nebius.ai/scientific-artifact-manifest/v1",
                    "manifest_id": idem,
                    "entries": [{"name": "gromacs-inputs", "semantic_type": "gromacs-input-bundle/v1",
                                 "artifact": artifact}],
                }
                manifest_ref = await helper.upload(
                    http, "gromacs", helper.canonical(manifest),
                    "application/vnd.fs2.scientific-manifest+json", "none", idem + "-manifest",
                )
                params = json.loads((args.fixture / "parameters.json").read_text())
                params["output_prefix"] = f"runs/qa-route-expiry-20261003/cohort-{cohort}"
                request = {
                    "schema": "fs2-serve.nebius.ai/scientific-run-request/v1",
                    "operation": "run-workflow", "service_class": "customer-batch",
                    "input_manifest": manifest_ref, "parameters": params,
                    "client_context": {
                        "display_name": f"Route expiry repair MD cohort {cohort}", "correlation_id": idem,
                    },
                }
                helper.save(out / "request.json", request)
                helper.save(out / "input-manifest.json", manifest)
                response = await http.post("/v1/models/gromacs:submit", json=request,
                                           headers={"Idempotency-Key": idem})
                if response.status_code != 202:
                    helper.save(out / "submission-error.json", response.json())
                accepted = helper.check(response).json()
                receipt.update(operation_id=accepted["operation"]["id"], state="admitted")
                helper.save(receipt_path, receipt)
                replay = helper.check(await http.post("/v1/models/gromacs:submit", json=request,
                                                     headers={"Idempotency-Key": idem})).json()
                assert replay["operation"]["id"] == receipt["operation_id"]
                assert replay["operation"]["reused"] is True
                receipt["idempotency_verified"] = True
                helper.save(receipt_path, receipt)
            op = receipt["operation_id"]
            last = None
            deadline = time.monotonic() + 1800
            while time.monotonic() < deadline:
                status = helper.check(await http.get(f"/v1/operations/{op}")).json()
                helper.save(out / "status.json", status)
                state = status["operation"]["status"]
                if state != last:
                    print(json.dumps({"cohort": cohort, "operation_id": op, "state": state}), flush=True)
                    last = state
                if state in helper.TERMINAL:
                    raise RuntimeError(f"Cohort {cohort} failed: {state}; retained status at {out}")
                if state == "succeeded" and status.get("batch", {}).get("result_published"):
                    result = helper.check(await http.get(f"/v1/operations/{op}/result")).json()
                    helper.save(out / "result.json", result)
                    receipt.update(await helper.collect_outputs(http, result, out), state="verified")
                    receipt["observed_seconds"] = round(time.monotonic() - started, 3)
                    helper.save(receipt_path, receipt)
                    print(json.dumps({"cohort": cohort, "operation_id": op, "state": "verified",
                                      "artifacts": len(receipt["verified_artifacts"]),
                                      "seconds": receipt["observed_seconds"]}), flush=True)
                    break
                await asyncio.sleep(5)
            else:
                raise TimeoutError("Observation window ended; resume the same receipt, not a new job")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--client-root", required=True, type=Path)
    parser.add_argument("--qa-env", required=True, type=Path)
    parser.add_argument("--fixture", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--origin", default="https://89.169.99.188")
    asyncio.run(main(parser.parse_args()))
