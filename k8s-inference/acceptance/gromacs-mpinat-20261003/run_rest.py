"""MPINAT cohorts through the public REST API; reuse QA ownership/cleanup guards."""
import argparse
import asyncio
import importlib.util
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).parents[1] / "gromacs-concurrency-20261003"))
from verify_concurrency import Run, load, save
from prepare import parameters


class Campaign(Run):
    async def prepare(self, http, cohort, index):
        case = self.args.cases[index - 1]
        run_id = f"cohort-{cohort}/{case}"
        out = self.args.output / run_id
        idem = f"{self.args.campaign_id}-{case}"
        out.mkdir(parents=True, exist_ok=True)
        receipt = load(out / "receipt.json", {})
        self.jobs[run_id] = {"out": out, "receipt": receipt, "idem": idem}
        if (out / "request.json").exists():
            return
        fixture = self.args.fixture / case
        provenance = load(fixture / "provenance.json")
        content = self.helper.FileSource(fixture / "input.tar.gz")
        if content.sha256 != provenance["bundle_sha256"]:
            raise ValueError("Fixture changed after provenance capture")
        artifact = await self.helper.upload(http, "gromacs", content, "application/x-tar", "gzip", idem + "-source")
        manifest = {"schema": "fs2-serve.nebius.ai/scientific-artifact-manifest/v1", "manifest_id": idem,
                    "entries": [{"name": "gromacs-inputs", "semantic_type": "gromacs-input-bundle/v1", "artifact": artifact}]}
        ref = await self.helper.upload(http, "gromacs", self.helper.canonical(manifest),
            "application/vnd.fs2.scientific-manifest+json", "none", idem + "-manifest")
        params = parameters(provenance)
        params["output_prefix"] = f"runs/{self.args.campaign_id}/{case}"
        request = {"schema": "fs2-serve.nebius.ai/scientific-run-request/v1", "operation": "run-workflow",
                   "service_class": "customer-batch", "input_manifest": ref, "parameters": params,
                   "client_context": {"display_name": f"Internal QA MPINAT {case}", "correlation_id": idem}}
        save(out / "input-manifest.json", manifest)
        save(out / "request.json", request)
        save(out / "provenance.json", provenance)
        receipt.update(state="prepared", input_sha256=content.sha256, case=case, steps=10000, repetitions=3)
        save(out / "receipt.json", receipt)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for field in ("client-root", "qa-env", "fixture", "output"):
        parser.add_argument("--" + field, type=Path, required=True)
    parser.add_argument("--campaign-id", required=True)
    parser.add_argument("--cases", required=True, help="Comma-separated fixture IDs, at most sixteen")
    parser.add_argument("--origin", default="https://89.169.99.188")
    parser.add_argument("--context", default="nebius-mk8s-k8s-inference-h100-e00j5z9te7x5dd9g6a")
    parser.add_argument("--timeout", type=int, default=10800)
    args = parser.parse_args()
    args.cases = args.cases.split(",")
    if not 1 <= len(args.cases) <= 16 or len(set(args.cases)) != len(args.cases):
        parser.error("Choose one to sixteen distinct cases")
    import re
    if not all(re.fullmatch(r"[a-z0-9][a-z0-9-]{0,100}", x) for x in [args.campaign_id, *args.cases]):
        parser.error("Identifiers must be path-safe")
    args.cohorts, args.requests = 1, len(args.cases)
    os.umask(0o077)
    args.output.mkdir(parents=True, exist_ok=True)
    sys.path.insert(0, str(args.client_root))
    spec = importlib.util.spec_from_file_location("scientific_acceptance", args.client_root / "scripts/scientific-batch-acceptance.py")
    helper = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(helper)
    asyncio.run(Campaign(args, helper).execute())


if __name__ == "__main__":
    main()
