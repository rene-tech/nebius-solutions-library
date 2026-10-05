"""One exact-input REST/raw-MCP benchmark on the existing two-slot QA policy.

Reuses the prior durable campaign, artifact verifier and lifecycle observations.
No admin client, quota edits, pool overrides or customer credential access.
"""

import argparse
import asyncio
import importlib.util
import os
from pathlib import Path
import re
import sys
import time

from recipes import TPR_SHA256, sha

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "gromacs-mpinat-20261003"))
from run_mpi import MPICampaign
from verify_concurrency import load, now, save


def fixture_identity(folder, model, interface):
    fixture = load(folder / "fixture.json")
    if (fixture["tpr_sha256"] != TPR_SHA256
            or sha(folder / "input.tar.gz") != fixture["input_sha256"]
            or sha(folder / "request.json") != fixture["request_sha256"]):
        raise ValueError("Exact-input fixture identity changed")
    params = load(folder / "request.json")
    mpi = "mpi-workflow" in params["schema"]
    if model != ("gromacs-mpi" if mpi else "gromacs"):
        raise ValueError("Fixture does not match the selected App")
    return {"model_id": model, "interface": interface,
            "fixture_sha256": sha(folder / "fixture.json"),
            "input_sha256": fixture["input_sha256"], "request_sha256": fixture["request_sha256"],
            "original_tpr_sha256": TPR_SHA256, "steps": fixture["steps"],
            "repetitions": fixture["repetitions"]}


class LynxCampaign(MPICampaign):
    async def prepare(self, http, cohort, index):
        if not getattr(self, "tracing_installed", False):
            http.event_hooks["request"].append(self.trace_request)
            http.event_hooks["response"].append(self.trace_response)
            self.tracing_installed = True
        case = self.args.cases[0]
        out = self.args.output / f"cohort-{cohort}" / case
        out.mkdir(parents=True, exist_ok=True)
        receipt = load(out / "receipt.json", {})
        idem = self.args.campaign_id + "-" + case
        self.jobs[f"cohort-{cohort}/{case}"] = {"out": out, "receipt": receipt, "idem": idem}
        identity = fixture_identity(self.args.fixture, self.args.model, self.args.interface)
        provenance = {"id": case, "tpr_sha256": TPR_SHA256,
                      "bundle_sha256": identity["input_sha256"],
                      "bundle_bytes": (self.args.fixture / "input.tar.gz").stat().st_size,
                      "private_source": True, "fixture_sha256": identity["fixture_sha256"]}
        if not (out / "provenance.json").exists():
            save(out / "provenance.json", provenance)
        elif load(out / "provenance.json") != provenance:
            raise ValueError("Retained exact-input provenance differs from this fixture")
        if (out / "request.json").exists():
            if receipt.get("benchmark_identity") != identity:
                raise ValueError("Do not reuse an idempotency boundary for a different benchmark")
            return
        content = self.helper.FileSource(self.args.fixture / "input.tar.gz")
        artifact = await self.helper.upload(http, self.args.model, content,
                                            "application/x-tar", "gzip", idem + "-source")
        manifest = {"schema": "fs2-serve.nebius.ai/scientific-artifact-manifest/v1", "manifest_id": idem,
                    "entries": [{"name": "gromacs-inputs", "semantic_type": "gromacs-input-bundle/v1", "artifact": artifact}]}
        pointer = await self.helper.upload(http, self.args.model, self.helper.canonical(manifest),
                                          "application/vnd.fs2.scientific-manifest+json", "none", idem + "-manifest")
        params = load(self.args.fixture / "request.json")
        params["output_prefix"] = f"runs/{self.args.campaign_id}/{case}"
        body = {"schema": "fs2-serve.nebius.ai/scientific-run-request/v1", "operation": "run-workflow",
                "service_class": "customer-batch", "input_manifest": pointer, "parameters": params,
                "client_context": {"display_name": "Internal QA exact-input MD performance", "correlation_id": idem}}
        save(out / "input-manifest.json", manifest)
        save(out / "request.json", body)
        save(out / "fixture.json", load(self.args.fixture / "fixture.json"))
        receipt.update(state="prepared", benchmark_identity=identity,
                       input_sha256=content.sha256, steps=identity["steps"], repetitions=identity["repetitions"])
        save(out / "receipt.json", receipt)

    async def submit(self, http, run_id):
        await self.guard_existing_policy(http)
        job = self.jobs[run_id]
        out, receipt = job["out"], job["receipt"]
        if receipt.get("operation_id"):
            return
        request = load(out / "request.json")
        receipt["submit_started_at"] = now()
        save(out / "receipt.json", receipt)
        started = time.monotonic()

        async def admit():
            if self.args.interface == "mcp":
                tool = "submit_gromacs_mpi_workflow" if self.args.model == "gromacs-mpi" else "submit_gromacs_workflow"
                return await self.mcp_call(http, tool, {**request, "idempotency_key": job["idem"]})
            response = await http.post(f"/v1/models/{self.args.model}:submit", json=request,
                                       headers={"Idempotency-Key": job["idem"]})
            receipt["submit_http_status"] = response.status_code
            if response.status_code != 202:
                save(out / "submission-error.json", response.json())
                receipt["state"] = "submission_failed"
                save(out / "receipt.json", receipt)
                response.raise_for_status()
            return response.json()

        data = await admit()
        save(out / "submission.json", data)
        operation = data.get("operation", data)
        if operation["model_id"] != self.args.model:
            raise ValueError("Admission returned the wrong App")
        receipt.update(operation_id=operation["id"], state=operation["status"], submit_seconds=time.monotonic() - started)
        save(out / "receipt.json", receipt)
        replay = await admit()
        save(out / "replay.json", replay)
        old = replay.get("operation", replay)
        receipt["idempotency_verified"] = old.get("id") == operation["id"] and old.get("reused") is True
        save(out / "receipt.json", receipt)
        if not receipt["idempotency_verified"]:
            raise ValueError("Same-key replay did not bind the same operation")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("client-root", "qa-env", "fixture", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--model", choices=("gromacs", "gromacs-mpi"), required=True)
    parser.add_argument("--interface", choices=("rest", "mcp"), required=True)
    parser.add_argument("--campaign-id", required=True)
    parser.add_argument("--case", required=True)
    parser.add_argument("--peer-operation", action="append", default=[])
    parser.add_argument("--origin", default="https://89.169.99.188")
    parser.add_argument("--context", default="nebius-mk8s-k8s-inference-h100-e00j5z9te7x5dd9g6a")
    parser.add_argument("--timeout", type=int, default=7200)
    parser.add_argument("--collect-only", action="store_true")
    args = parser.parse_args()
    if not all(re.fullmatch(r"[a-z0-9][a-z0-9-]{0,90}", text) for text in (args.campaign_id, args.case)):
        parser.error("Use path-safe distinct campaign/case names")
    if not args.campaign_id.startswith("fs2-lynx-performance-20261005-"):
        parser.error("Output prefixes must belong to this task")
    if len(args.peer_operation) > 1 or any(not re.fullmatch(r"[a-f0-9-]{36}", op) for op in args.peer_operation):
        parser.error("At most one exact coordinated peer UUID")
    args.cases, args.cohorts, args.requests, args.existing_policy = [args.case], 1, 1, True
    fixture_identity(args.fixture, args.model, args.interface)
    os.umask(0o077)
    args.output.mkdir(parents=True, exist_ok=True)
    spec = importlib.util.spec_from_file_location("scientific_acceptance", args.client_root / "scripts/scientific-batch-acceptance.py")
    helper = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(helper)
    runner = LynxCampaign(args, helper)
    result = asyncio.run(runner.collect_existing() if args.collect_only else runner.execute_existing_policy())
    return 1 if result is False else 0


if __name__ == "__main__":
    raise SystemExit(main())
