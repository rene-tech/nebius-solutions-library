"""MPINAT cohorts through the public REST API; reuse QA ownership/cleanup guards."""
import argparse
import asyncio
import importlib.util
import os
from pathlib import Path
import sys
import time
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).parents[1] / "gromacs-concurrency-20261003"))
from verify_concurrency import Run, load, save, append, now, emit, TERMINAL
from prepare import parameters
from native_timings import verify_native_timings


class Campaign(Run):
    def prepared_requests(self):
        return len(self.args.cases)

    async def collect(self, http, run_id, slots):
        await super().collect(http, run_id, slots)
        job = self.jobs[run_id]
        if job["receipt"].get("state") != "verified":
            return
        timings = verify_native_timings(load(job["out"] / "request.json"), job["receipt"])
        save(job["out"] / "native-timings.json", timings)
        job["receipt"]["benchmark_complete"] = timings["benchmark_complete"]
        save(job["out"] / "receipt.json", job["receipt"])
        if not timings["benchmark_complete"]:
            raise ValueError("Artifacts verified, but requested native benchmark timings are incomplete")

    async def trace_request(self, request):
        span = {"span_id": str(uuid4()), "method": request.method,
                "host": request.url.host, "path": request.url.path,
                "started_at": now(), "request_bytes": request.headers.get("content-length")}
        request.extensions["benchmark_span"] = (span, time.monotonic())
        append(self.args.output / "http-spans.jsonl", {"event": "start", **span})

    async def trace_response(self, response):
        span, started = response.request.extensions["benchmark_span"]
        append(self.args.output / "http-spans.jsonl", {"event": "response_headers", **span,
            "finished_at": now(), "seconds_to_headers": time.monotonic() - started,
            "status_code": response.status_code,
            "response_bytes": response.headers.get("content-length"),
            "body_transfer_seconds": None})

    async def prepare(self, http, cohort, index):
        if not getattr(self, "tracing_installed", False):
            http.event_hooks["request"].append(self.trace_request)
            http.event_hooks["response"].append(self.trace_response)
            self.tracing_installed = True
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

    async def observe(self, http, run_id):
        await super().observe(http, run_id)
        job = self.jobs[run_id]
        operation = job["receipt"].get("operation_id")
        if not operation:
            return
        events = load(job["out"] / "events.json", {"data": []})
        after = max((e["sequence"] for e in events["data"]), default=0)
        for _ in range(100):
            response = await http.get(f"/v1/operations/{operation}/events",
                                      params={"after_sequence": after, "limit": 200})
            response.raise_for_status()
            rows = response.json()["data"]
            events["data"].extend(rows)
            if rows:
                following = max(e["sequence"] for e in rows)
                if following <= after:
                    raise ValueError("Event pagination stopped advancing")
                after = following
            if len(rows) < 200:
                break
        else:
            raise ValueError("Event history exceeds pagination bound")
        save(job["out"] / "events.json", events)

    async def cohort(self, http, cohort):
        """Refill bounded slots as cases finish; large systems need not block smaller cases."""
        names = [name for name in self.jobs if name.startswith(f"cohort-{cohort}/")]
        deadline = time.monotonic() + self.args.timeout
        terminal = TERMINAL | {"verified", "collected_failure", "submission_failed"}
        previous = None
        while time.monotonic() < deadline:
            active = [n for n in names if self.jobs[n]["receipt"].get("operation_id")
                      and self.jobs[n]["receipt"]["state"] not in terminal]
            prepared = [n for n in names if self.jobs[n]["receipt"]["state"] == "prepared"]
            entering = prepared[:max(0, self.args.requests - len(active))]
            results = await asyncio.gather(*(self.submit(http, n) for n in entering), return_exceptions=True)
            for name, result in zip(entering, results):
                if isinstance(result, Exception):
                    append(self.jobs[name]["out"] / "submission-errors.jsonl",
                           {"at": now(), "error_type": type(result).__name__})
            # Terminal operation state may precede artifact publication. Keep
            # polling status/events until the cohort's delivery condition holds.
            pending = [n for n in names if self.jobs[n]["receipt"].get("operation_id")]
            results = await asyncio.gather(*(self.observe(http, n) for n in pending), return_exceptions=True)
            for name, result in zip(pending, results):
                if isinstance(result, Exception):
                    append(self.jobs[name]["out"] / "observation-errors.jsonl",
                           {"at": now(), "error_type": type(result).__name__})
            from collections import Counter
            counts = dict(Counter(self.jobs[n]["receipt"]["state"] for n in names))
            if counts != previous:
                emit(cohort=cohort, states=counts)
                previous = counts
            if all(self.jobs[n]["receipt"]["state"] in terminal and
                   (self.jobs[n]["receipt"]["state"] != "succeeded" or self.jobs[n]["receipt"].get("result_published"))
                   for n in names):
                break
            await asyncio.sleep(5)
        await self.drain(http, names)
        slots = asyncio.Semaphore(2)
        results = await asyncio.gather(*(self.collect(http, n, slots) for n in names), return_exceptions=True)
        for name, result in zip(names, results):
            if isinstance(result, Exception):
                self.jobs[name]["receipt"]["collection_error_type"] = type(result).__name__
                save(self.jobs[name]["out"] / "receipt.json", self.jobs[name]["receipt"])
        return all(self.jobs[n]["receipt"]["state"] == "verified"
                   and self.jobs[n]["receipt"].get("benchmark_complete") is True for n in names)

    async def collect_existing(self):
        """Read-only recovery: no submissions, key-policy changes or GPU work."""
        import httpx2
        key = dict(line.split("=", 1) for line in self.args.qa_env.read_text().splitlines()
                   if "=" in line)["SCIENTIFIC_MODELS_API_KEY"]
        if not key.startswith("fs2_pat_56130b22ae09"):
            raise ValueError("Only existing system/qa")
        for case in self.args.cases:
            root = self.args.output / "cohort-1" / case
            if not (root / "request.json").exists() or not load(root / "receipt.json", {}).get("operation_id"):
                raise ValueError("Collect-only requires an existing request and operation for every case")
        async with httpx2.AsyncClient(base_url=self.args.origin, headers={"Authorization": "Bearer " + key},
                                     timeout=90, trust_env=False) as http:
            for index in range(1, len(self.args.cases) + 1):
                await self.prepare(http, 1, index)
            await asyncio.gather(*(self.observe(http, name) for name in self.jobs))
            if any(j["receipt"]["state"] not in TERMINAL | {"verified", "collected_failure"} for j in self.jobs.values()):
                raise ValueError("Collect-only does not cancel or wait for active operations")
            slots = asyncio.Semaphore(2)
            results = await asyncio.gather(*(self.collect(http, n, slots) for n in self.jobs), return_exceptions=True)
            for (name, job), result in zip(self.jobs.items(), results):
                if isinstance(result, Exception):
                    job["receipt"]["collection_error_type"] = type(result).__name__
                    save(job["out"] / "receipt.json", job["receipt"])
            save(self.args.output / "collection-summary.json", {"at": now(), "read_only_recovery": True,
                 "requests": {name: {"operation_id": j["receipt"]["operation_id"],
                     "state": j["receipt"]["state"], "result_published": j["receipt"].get("result_published"),
                     "collection_error_type": j["receipt"].get("collection_error_type")} for name, j in self.jobs.items()}})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for field in ("client-root", "qa-env", "fixture", "output"):
        parser.add_argument("--" + field, type=Path, required=True)
    parser.add_argument("--campaign-id", required=True)
    parser.add_argument("--cases", required=True, help="Comma-separated fixture IDs, at most 24")
    parser.add_argument("--concurrency", type=int, default=16, choices=range(1, 17))
    parser.add_argument("--origin", default="https://89.169.99.188")
    parser.add_argument("--context", default="nebius-mk8s-k8s-inference-h100-e00j5z9te7x5dd9g6a")
    parser.add_argument("--timeout", type=int, default=10800)
    parser.add_argument("--collect-only", action="store_true", help="Read existing terminal results; never submit or change policy")
    args = parser.parse_args()
    args.cases = args.cases.split(",")
    if not 1 <= len(args.cases) <= 24 or len(set(args.cases)) != len(args.cases):
        parser.error("Choose one to 24 distinct cases")
    import re
    if not all(re.fullmatch(r"[a-z0-9][a-z0-9-]{0,100}", x) for x in [args.campaign_id, *args.cases]):
        parser.error("Identifiers must be path-safe")
    args.cohorts, args.requests = 1, min(args.concurrency, len(args.cases))
    os.umask(0o077)
    args.output.mkdir(parents=True, exist_ok=True)
    sys.path.insert(0, str(args.client_root))
    spec = importlib.util.spec_from_file_location("scientific_acceptance", args.client_root / "scripts/scientific-batch-acceptance.py")
    helper = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(helper)
    campaign = Campaign(args, helper)
    asyncio.run(campaign.collect_existing() if args.collect_only else campaign.execute())


if __name__ == "__main__":
    main()
