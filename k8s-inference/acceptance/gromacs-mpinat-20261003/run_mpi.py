"""Strong-scaling cohorts through the published GROMACS MPI REST/MCP surface.

Run one shape at a time so comparisons do not hide competing benchmark jobs.
The inherited supervisor verifies QA ownership, records allocation/health and
restores only the QA admission limit. It never evicts Apps or changes pools.
MCP submissions/status/results are retained alongside independent REST reads;
this is MCP transport evidence, not a substitute for the actual agent/skill run.
"""
import argparse
import asyncio
import importlib.util
import os
from pathlib import Path
import re
import sys
import time

from run_rest import Campaign
from prepare import parameters
from verify_concurrency import QA_KEY_ID, active_operations, load, save, now, emit


PROTOCOLS = ("auto", "fixed-cpu-pme", "fixed-gpu-pme")


def shape_parameters(case, nodes, gpus_per_node, steps=10000, repetitions=3, protocol="auto"):
    if (type(nodes) is not int or not 1 <= nodes <= 8
            or type(gpus_per_node) is not int or gpus_per_node not in (1, 2, 4, 8)
            or nodes * gpus_per_node > 16):
        raise ValueError("Use 1..8 nodes, 1/2/4/8 GPUs per node, at most 16 GPUs")
    if type(steps) is not int or steps < 10000 or repetitions < 3:
        raise ValueError("Qualification requires at least 10,000 steps and three timing repeats")
    if protocol not in PROTOCOLS:
        raise ValueError("Unknown matched-input PME protocol")
    value = parameters(case, steps, repetitions, mpi_nodes=nodes, gpus_per_node=gpus_per_node)
    if protocol != "auto":
        for command in value["jobs"][0]["steps"]:
            if command["command"] != "mdrun":
                continue
            arguments = command["args"]
            if "-update" not in arguments:
                arguments += ["-update", "cpu"]
            # This distinct comparison fixes the original TPR PME grid/cutoff;
            # do not conflate its timing with the normal auto-tuned protocol.
            # This runtime has no distributed GPU FFT backend: GPU PME uses
            # exactly one PME rank, not an unqualified multi-PME decomposition.
            arguments += ["-bonded", "cpu", "-notunepme", "-pme",
                          "gpu" if protocol == "fixed-gpu-pme" else "cpu"]
            arguments += ["-npme", "1" if protocol == "fixed-gpu-pme" and nodes * gpus_per_node > 1 else "0"]
            if protocol == "fixed-gpu-pme":
                arguments += ["-pmefft", "gpu"]
    return value


class MPICampaign(Campaign):
    async def guard_existing_policy(self, http):
        """Borrow one of QA's existing two slots beside an explicitly owned peer.

        This path never reads admin credentials or changes admission policy.
        Peer IDs must be supplied by their supervisor; arbitrary active work
        is not permission to borrow its identity or cancel it.
        """
        own = {job["receipt"].get("operation_id") for job in self.jobs.values()} - {None}
        peers = set(self.args.peer_operation)
        active = {row["id"] for row in await active_operations(http)}
        if len(peers) > 1 or active - own - peers or len(active & own) > 1 or len(active) > 2:
            raise ValueError("QA work exceeds one explicit peer and one owned MPI operation")
        save(self.args.output / "existing-policy-observation.json",
             {"at": now(), "peer_operation_ids": sorted(peers), "active_ids": sorted(active),
              "policy_written": False, "maximum_owned_operations": 1})

    async def execute_existing_policy(self):
        import httpx2
        values = dict(line.split("=", 1) for line in self.args.qa_env.read_text().splitlines() if "=" in line)
        key = values["SCIENTIFIC_MODELS_API_KEY"]
        if not key.startswith("fs2_pat_" + QA_KEY_ID.replace("-", "")[:12]):
            raise ValueError("Only the existing system/qa identity may be used")
        if self.args.requests != 1 or len(self.args.cases) != 1 or self.args.cohorts != 1:
            raise ValueError("Existing-policy qualification runs one MPI case at a time")
        async with httpx2.AsyncClient(base_url=self.args.origin, headers={"Authorization": "Bearer " + key},
                                     timeout=90, trust_env=False) as http:
            # Preparation adopts only the exact same campaign's retained ID.
            # It stages bytes but does not submit a GPU operation.
            await self.prepare(http, 1, 1)
            await self.guard_existing_policy(http)
            watchers = [asyncio.create_task(self.health_loop(http)), asyncio.create_task(self.cluster_loop())]
            try:
                verified = await self.cohort(http, 1)
                save(self.args.output / "summary.json", {"at": now(), "tenant": "system", "principal": "qa",
                     "key_id": QA_KEY_ID, "cohorts_verified": [verified], "policy_written": False,
                     "peer_operation_ids": self.args.peer_operation,
                     "api_probe_count": len(self.health),
                     "api_probe_failures": sum(row.get("http_status") != 200 for row in self.health)})
                return verified
            finally:
                try:
                    await self.drain(http, list(self.jobs))
                finally:
                    self.done.set()
                    for watcher in watchers:
                        watcher.cancel()
                    await asyncio.gather(*watchers, return_exceptions=True)

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
        identity = {"model_id": "gromacs-mpi", "nodes": self.args.nodes,
                    "gpus_per_node": self.args.gpus_per_node,
                    "total_gpus": self.args.nodes * self.args.gpus_per_node,
                    "interface": self.args.interface, "steps": self.args.steps,
                    "repetitions": self.args.repetitions,
                    "protocol": self.args.protocol}
        if (out / "request.json").exists():
            if receipt.get("benchmark_identity") != identity:
                raise ValueError("Existing receipt has a different shape/path; use a new campaign")
            return
        fixture = self.args.fixture / case
        provenance = load(fixture / "provenance.json")
        content = self.helper.FileSource(fixture / "input.tar.gz")
        if content.sha256 != provenance["bundle_sha256"]:
            raise ValueError("Fixture changed after provenance capture")
        artifact = await self.helper.upload(http, "gromacs-mpi", content,
            "application/x-tar", "gzip", idem + "-source")
        manifest = {"schema": "fs2-serve.nebius.ai/scientific-artifact-manifest/v1",
                    "manifest_id": idem, "entries": [{"name": "gromacs-inputs",
                    "semantic_type": "gromacs-input-bundle/v1", "artifact": artifact}]}
        ref = await self.helper.upload(http, "gromacs-mpi", self.helper.canonical(manifest),
            "application/vnd.fs2.scientific-manifest+json", "none", idem + "-manifest")
        params = shape_parameters(provenance, self.args.nodes, self.args.gpus_per_node,
                                  self.args.steps, self.args.repetitions, self.args.protocol)
        params["output_prefix"] = f"runs/{self.args.campaign_id}/{case}"
        request = {"schema": "fs2-serve.nebius.ai/scientific-run-request/v1",
                   "operation": "run-workflow", "service_class": "customer-batch",
                   "input_manifest": ref, "parameters": params,
                   "client_context": {"display_name": f"Internal QA MPI {case} {self.args.nodes}x{self.args.gpus_per_node}",
                                      "correlation_id": idem}}
        save(out / "input-manifest.json", manifest)
        save(out / "request.json", request)
        save(out / "provenance.json", provenance)
        receipt.update(state="prepared", input_sha256=content.sha256, case=case,
                       benchmark_identity=identity, steps=self.args.steps,
                       repetitions=self.args.repetitions)
        save(out / "receipt.json", receipt)

    async def mcp_call(self, http, tool, arguments):
        import httpx2
        started = time.monotonic()
        # The same non-expiring QA identity is used on both transports. No
        # credential or signed artifact URL is copied to the report.
        async with httpx2.AsyncClient(headers={"Authorization": http.headers["Authorization"]},
                                     timeout=90, trust_env=False) as transport:
            async with self.helper.Client(self.helper.streamable_http_client(
                    self.args.origin.rstrip("/") + "/mcp", http_client=transport),
                    read_timeout_seconds=90) as connection:
                result = await self.helper.call(connection, tool, arguments)
        from verify_concurrency import append
        append(self.args.output / "mcp-spans.jsonl", {"at": now(), "tool": tool,
               "seconds_including_handshake": time.monotonic() - started})
        return result

    async def submit(self, http, run_id):
        if getattr(self.args, "existing_policy", False):
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
                return await self.mcp_call(http, "submit_gromacs_mpi_workflow",
                                           {**request, "idempotency_key": job["idem"]})
            response = await http.post("/v1/models/gromacs-mpi:submit", json=request,
                                       headers={"Idempotency-Key": job["idem"]})
            receipt["submit_http_status"] = response.status_code
            body = response.json()
            if response.status_code != 202:
                save(out / "submission.json", body)
                receipt["state"] = "submission_failed"
                save(out / "receipt.json", receipt)
                raise ValueError("GROMACS MPI admission rejected; retained response")
            return body
        data = await admit()
        save(out / "submission.json", data)
        operation = data.get("operation", data)
        if operation.get("model_id") != "gromacs-mpi":
            raise ValueError("Submission returned an unexpected App")
        receipt.update(operation_id=operation["id"], state=operation["status"],
                       submit_seconds=time.monotonic() - started)
        save(out / "receipt.json", receipt)
        replay = await admit()
        save(out / "replay.json", replay)
        previous = replay.get("operation", replay)
        receipt["idempotency_verified"] = (previous.get("id") == operation["id"]
                                           and previous.get("reused") is True)
        save(out / "receipt.json", receipt)
        if not receipt["idempotency_verified"]:
            raise ValueError("MPI submission did not preserve idempotency")

    async def observe(self, http, run_id):
        # Independent REST history provides lifecycle events needed for costing.
        await super().observe(http, run_id)
        job = self.jobs[run_id]
        operation = job["receipt"].get("operation_id")
        if self.args.interface == "mcp" and operation:
            status = await self.mcp_call(http, "get_scientific_status", {"operation_id": operation})
            if status.get("operation", status).get("id") != operation:
                raise ValueError("MCP status returned a different operation")
            save(job["out"] / "mcp-status.json", status)

    async def collect(self, http, run_id, slots):
        job = self.jobs[run_id]
        receipt = job["receipt"]
        if (self.args.interface == "mcp" and receipt.get("result_published")
                and receipt["state"] not in {"verified", "collected_failure"}):
            result = await self.mcp_call(http, "get_scientific_result",
                                         {"operation_id": receipt["operation_id"]})
            if result.get("operation_id") != receipt["operation_id"]:
                raise ValueError("MCP result returned a different operation")
            save(job["out"] / "mcp-result.json", result)
        await super().collect(http, run_id, slots)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    for field in ("client-root", "qa-env", "fixture", "output"):
        p.add_argument("--" + field, type=Path, required=True)
    p.add_argument("--campaign-id", required=True)
    p.add_argument("--cases", required=True)
    p.add_argument("--nodes", type=int, required=True)
    p.add_argument("--gpus-per-node", type=int, required=True)
    p.add_argument("--interface", choices=("rest", "mcp"), required=True)
    p.add_argument("--protocol", choices=PROTOCOLS, default="auto")
    p.add_argument("--steps", type=int, default=10000)
    p.add_argument("--repetitions", type=int, default=3)
    p.add_argument("--origin", default="https://89.169.99.188")
    p.add_argument("--context", default="nebius-mk8s-k8s-inference-h100-e00j5z9te7x5dd9g6a")
    p.add_argument("--timeout", type=int, default=21600)
    p.add_argument("--collect-only", action="store_true")
    p.add_argument("--existing-policy", action="store_true", help="Use one existing QA slot; no admin or policy writes")
    p.add_argument("--peer-operation", action="append", default=[],
                   help="One exact operation owned by a coordinated peer; never cancel or adopt it")
    a = p.parse_args()
    a.cases = a.cases.split(",")
    if (not 1 <= len(a.cases) <= 24 or len(set(a.cases)) != len(a.cases)
            or not all(re.fullmatch(r"[a-z0-9][a-z0-9-]{0,100}", x)
                       for x in [a.campaign_id, *a.cases])):
        p.error("Select unique path-safe case and campaign IDs")
    shape_parameters({"id": a.cases[0]}, a.nodes, a.gpus_per_node, a.steps, a.repetitions, a.protocol)
    a.cohorts, a.requests = 1, 1
    if a.peer_operation and not a.existing_policy:
        p.error("--peer-operation requires --existing-policy")
    if len(a.peer_operation) > 1 or any(not re.fullmatch(r"[a-f0-9-]{36}", value) for value in a.peer_operation):
        p.error("Specify at most one exact peer operation UUID")
    if a.existing_policy and len(a.cases) != 1:
        p.error("--existing-policy runs exactly one case")
    os.umask(0o077)
    a.output.mkdir(parents=True, exist_ok=True)
    sys.path.insert(0, str(a.client_root))
    spec = importlib.util.spec_from_file_location("scientific_acceptance", a.client_root / "scripts/scientific-batch-acceptance.py")
    helper = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(helper)
    campaign = MPICampaign(a, helper)
    emit(interface=a.interface, shape=f"{a.nodes}x{a.gpus_per_node}",
         agent_qualification_claimed=False)
    if a.collect_only:
        asyncio.run(campaign.collect_existing())
    elif a.existing_policy:
        # Share the existing matrix lock; the actual-agent supervisor is
        # separately paused at its batch boundary while this lane borrows.
        import fcntl
        with (a.qa_env.resolve().parent / "gromacs-matrix-system-qa.lock").open("a") as lease:
            fcntl.flock(lease.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            if not asyncio.run(campaign.execute_existing_policy()):
                raise SystemExit(2)
    else:
        asyncio.run(campaign.execute())


if __name__ == "__main__":
    main()
