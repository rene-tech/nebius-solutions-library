"""Bounded public-REST GROMACS qualification using ONLY the existing system/qa.

No customer credentials, new identities, direct Job creation, cloud quota
changes, pool edits, or serving-image changes. Each cohort uses distinct seeds.
The QA admission limit is restored; GPU execution is observed independently of
logical operation state. Receipts/idempotency keys permit safe resumption.
"""

import argparse
import asyncio
import base64
import gzip
import hashlib
import importlib.util
import io
import json
import os
import re
import sys
import tarfile
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import httpx2

QA_KEY_ID = "56130b22-ae09-42fc-a0f0-48012f22fb71"
TASK_ID = "fs2-gromacs-internal-api-concurrency-r20261003"
TERMINAL = {"succeeded", "failed", "cancelled", "expired", "preempted"}
FIXTURE_SHA = "8d2d7f61ddb7d329387fc64b2b36b27511f8575cfcaffdf2bc0763ed579f008e"


def now():
    return datetime.now(timezone.utc).isoformat()


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    staged = path.with_suffix(path.suffix + ".partial")
    staged.write_text(json.dumps(value, indent=2) + "\n")
    staged.replace(path)


def load(path, default=None):
    return json.loads(path.read_text()) if path.exists() else default


def append(path, value):
    with path.open("a") as stream:
        stream.write(json.dumps(value) + "\n")


def emit(**value):
    print(json.dumps({"at": now(), **value}), flush=True)


async def active_operations(http):
    """Read every history page before borrowing the existing QA admission limit."""
    active, cursor, seen = [], None, set()
    while True:
        query = {"limit": 200}
        if cursor:
            query["cursor"] = cursor
        history = await http.get("/v1/operations", params=query)
        history.raise_for_status()
        page = history.json()
        active.extend(o for o in page["data"] if o["status"] not in TERMINAL)
        cursor = page.get("next_cursor")
        if not cursor:
            return active
        if cursor in seen or len(seen) >= 100:
            raise ValueError("Operation history pagination did not terminate")
        seen.add(cursor)


def make_input(fixture, seed, production_ps):
    original = (fixture / "input.tar.gz").read_bytes()
    if hashlib.sha256(original).hexdigest() != FIXTURE_SHA:
        raise ValueError("Fixture identity changed; inspect before submitting")
    files = {}
    with tarfile.open(fileobj=io.BytesIO(original), mode="r:gz") as archive:
        for member in archive:
            if not member.isfile() or "/" in member.name:
                raise ValueError("Expected only the reviewed flat input archive")
            files[member.name] = archive.extractfile(member).read()
    for offset, phase in enumerate(("nvt", "npt", "production")):
        name = phase + ".mdp"
        text = files[name].decode()
        for field in ("ld-seed", "gen-seed"):
            text, count = re.subn(r"(?m)^" + field + r"\s*=.*$", f"{field} = {seed + offset}", text)
            if count != 1:
                raise ValueError("Missing/ambiguous seed in reviewed fixture")
        if phase == "production":
            text, count = re.subn(r"(?m)^nsteps\s*=.*$", f"nsteps = {production_ps * 500}", text)
            assert count == 1
            # Keep useful output while avoiding 32 full-water high-frequency
            # trajectories filling the existing shared QA workspace.
            text = re.sub(r"(?m)^nstxout-compressed\s*=.*$", "nstxout-compressed = 5000", text)
        files[name] = text.encode()
    protocol = json.loads(files["starter-protocol.json"])
    protocol.update(variant="internal-api-concurrency", production_ps=production_ps,
                    production_output_interval_ps=10, nvt_seed=seed, npt_seed=seed + 1,
                    production_seed=seed + 2, convergence_claimed=False)
    protocol["other_protocol_files"] = "Canonical source provenance only; this file and native MDPs define this variant."
    files["starter-protocol.json"] = (json.dumps(protocol, indent=2) + "\n").encode()
    memory = io.BytesIO()
    with tarfile.open(fileobj=memory, mode="w") as archive:
        for name, content in sorted(files.items()):
            info = tarfile.TarInfo(name)
            info.size, info.mode, info.mtime = len(content), 0o600, 0
            archive.addfile(info, io.BytesIO(content))
    return gzip.compress(memory.getvalue(), mtime=0), protocol


class Run:
    def __init__(self, args, helper):
        self.args, self.helper = args, helper
        self.jobs, self.done = {}, asyncio.Event()
        self.health, self.cluster = [], []

    async def kubectl(self, *arguments):
        process = await asyncio.create_subprocess_exec(
            "kubectl", "--context", self.args.context, "--request-timeout=20s", *arguments,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        try:
            stdout, _ = await asyncio.wait_for(process.communicate(), timeout=30)
        except TimeoutError:
            process.kill()
            await process.wait()
            raise
        if process.returncode:
            raise RuntimeError("kubectl read/observation failed")
        return stdout

    async def kjson(self, *arguments):
        return json.loads(await self.kubectl(*arguments, "-o", "json"))

    async def key_metadata(self, admin):
        response = await admin.get("/admin/api/v1/keys", params={"tenant_id": "system"})
        response.raise_for_status()
        key = next(k for k in response.json()["data"]["items"] if k["id"] == QA_KEY_ID)
        if (key["tenant_id"], key["principal_id"]) != ("system", "qa") or key["expires_at"] is not None:
            raise ValueError("Internal non-expiring QA identity mismatch")
        return key

    async def prepare(self, http, cohort, index):
        run_id = f"cohort-{cohort}/request-{index:02}"
        out = self.args.output / run_id
        idem = f"{self.args.campaign_id}-c{cohort}-r{index:02}"
        out.mkdir(parents=True, exist_ok=True)
        receipt = load(out / "receipt.json", {})
        self.jobs[run_id] = {"out": out, "receipt": receipt, "idem": idem}
        if (out / "request.json").exists():
            return
        content, protocol = make_input(self.args.fixture, self.args.seed_base + cohort * 100 + index * 3,
                                       self.args.production_ps)
        (out / "input.tar.gz").write_bytes(content)
        save(out / "protocol.json", protocol)
        artifact = await self.helper.upload(http, "gromacs", content, "application/x-tar", "gzip", idem + "-source")
        manifest = {"schema": "fs2-serve.nebius.ai/scientific-artifact-manifest/v1", "manifest_id": idem,
                    "entries": [{"name": "gromacs-inputs", "semantic_type": "gromacs-input-bundle/v1", "artifact": artifact}]}
        ref = await self.helper.upload(http, "gromacs", self.helper.canonical(manifest),
                                       "application/vnd.fs2.scientific-manifest+json", "none", idem + "-manifest")
        params = json.loads((self.args.fixture / "parameters.json").read_text())
        params["output_prefix"] = f"runs/{self.args.campaign_id}/{run_id}"
        request = {"schema": "fs2-serve.nebius.ai/scientific-run-request/v1", "operation": "run-workflow",
                   "service_class": "customer-batch", "input_manifest": ref, "parameters": params,
                   "client_context": {"display_name": f"Internal QA GROMACS {run_id}", "correlation_id": idem}}
        save(out / "input-manifest.json", manifest)
        save(out / "request.json", request)
        receipt.update(input_sha256=hashlib.sha256(content).hexdigest(), state="prepared", **protocol)
        save(out / "receipt.json", receipt)

    async def submit(self, http, run_id):
        job = self.jobs[run_id]
        out, receipt = job["out"], job["receipt"]
        if receipt.get("operation_id"):
            return
        started = time.monotonic()
        receipt["submit_started_at"] = now()
        save(out / "receipt.json", receipt)
        response = await http.post("/v1/models/gromacs:submit", json=load(out / "request.json"),
                                   headers={"Idempotency-Key": job["idem"]})
        receipt.update(submit_http_status=response.status_code, submit_seconds=time.monotonic() - started)
        save(out / "submission.json", response.json())
        if response.status_code != 202:
            receipt["state"] = "submission_failed"
            save(out / "receipt.json", receipt)
            return
        op = response.json()["operation"]
        receipt.update(operation_id=op["id"], state=op["status"])
        save(out / "receipt.json", receipt)
        replay = await http.post("/v1/models/gromacs:submit", json=load(out / "request.json"),
                                 headers={"Idempotency-Key": job["idem"]})
        data = replay.json()
        receipt["idempotency_verified"] = (replay.status_code == 202 and data.get("operation", {}).get("id") == op["id"]
                                            and data["operation"].get("reused") is True)
        save(out / "replay.json", data)
        save(out / "receipt.json", receipt)

    async def observe(self, http, run_id):
        job = self.jobs[run_id]
        receipt, out = job["receipt"], job["out"]
        if not receipt.get("operation_id") or receipt["state"] in ("verified", "collected_failure"):
            return
        response = await http.get("/v1/operations/" + receipt["operation_id"])
        response.raise_for_status()
        status = response.json()
        save(out / "status.json", status)
        job["status"] = status
        receipt["state"] = status["operation"]["status"]
        receipt["result_published"] = status.get("batch", {}).get("result_published", False)
        receipt["workload_id"] = status.get("batch", {}).get("workload_id")
        save(out / "receipt.json", receipt)
        append(out / "observations.jsonl", {"at": now(), "status": status})

    async def collect(self, http, run_id, slots):
        job = self.jobs[run_id]
        out, receipt = job["out"], job["receipt"]
        if receipt["state"] == "verified" or not receipt.get("result_published"):
            return
        async with slots:
            response = await http.get("/v1/operations/" + receipt["operation_id"] + "/result")
            response.raise_for_status()
            result = response.json()
            save(out / "result.json", result)
            if result.get("operation_id") != receipt["operation_id"]:
                raise ValueError("Result identity mismatch")
            if receipt["state"] == "succeeded":
                receipt.update(await self.helper.collect_outputs(http, result, out), state="verified")
            elif result.get("output_manifest"):
                receipt.update(await self.helper.collect_manifest(http, result["output_manifest"], out / "diagnostics", diagnostics=True),
                               state="collected_failure")
            save(out / "receipt.json", receipt)
            emit(run_id=run_id, state=receipt["state"], artifacts=len(receipt.get("verified_artifacts", [])))

    async def health_loop(self, http):
        while not self.done.is_set():
            started = time.monotonic()
            try:
                response = await http.get("/v1/scientific-models")
                row = {"at": now(), "http_status": response.status_code, "seconds": time.monotonic() - started}
            except Exception as error:
                row = {"at": now(), "error_type": type(error).__name__, "seconds": time.monotonic() - started}
            self.health.append(row)
            append(self.args.output / "api-availability.jsonl", row)
            try:
                await asyncio.wait_for(self.done.wait(), timeout=5)
            except TimeoutError:
                pass

    async def device_sample(self, pod, container):
        try:
            text = (await self.kubectl("-n", "fs2-models", "exec", pod, "-c", container, "--", "nvidia-smi",
                "--query-compute-apps=gpu_uuid,process_name,used_gpu_memory", "--format=csv,noheader,nounits")).decode()
            return {"pod": pod, "at": now(), "compute_processes": text.strip().splitlines()}
        except Exception as error:
            return {"pod": pod, "at": now(), "error_type": type(error).__name__}

    async def cluster_loop(self):
        previous = None
        while not self.done.is_set():
            try:
                pods, nodes = await asyncio.gather(self.kjson("get", "pods", "-A"), self.kjson("get", "nodes"))
                workload_ids = {j["receipt"].get("workload_id") for j in self.jobs.values()} - {None}
                owned, allocated, devices = [], Counter(), []
                for pod in pods["items"]:
                    spec, status = pod["spec"], pod["status"]
                    if status.get("phase") not in ("Succeeded", "Failed"):
                        allocated[spec.get("nodeName", "")] += sum(int(c.get("resources", {}).get("requests", {}).get("nvidia.com/gpu", 0)) for c in spec["containers"])
                    labels = pod["metadata"].get("labels", {})
                    if labels.get("fs2.nebius.ai/workload-id") not in workload_ids:
                        continue
                    if labels.get("fs2.nebius.ai/tenant-id") != "system":
                        raise ValueError("Test workload unexpectedly outside system tenant")
                    states = {c["name"]: c["state"] for c in status.get("containerStatuses", [])}
                    gpu_containers = [c for c in spec["containers"] if int(c.get("resources", {}).get("limits", {}).get("nvidia.com/gpu", 0))]
                    executing = [c for c in gpu_containers if "running" in states.get(c["name"], {})]
                    row = {"pod": pod["metadata"]["name"], "node": spec.get("nodeName"), "phase": status.get("phase"),
                           "workload_id": labels.get("fs2.nebius.ai/workload-id"), "containers": states,
                           "running_gpu_claims": sum(int(c["resources"]["limits"]["nvidia.com/gpu"]) for c in executing)}
                    owned.append(row)
                    devices.extend(self.device_sample(row["pod"], c["name"]) for c in executing)
                summaries = []
                for node in nodes["items"]:
                    name, labels = node["metadata"]["name"], node["metadata"].get("labels", {})
                    gpu = int(node.get("status", {}).get("allocatable", {}).get("nvidia.com/gpu", 0))
                    if gpu:
                        summaries.append({"node": name, "pool": labels.get("accelerator.fs2.nebius/pool-id"), "gpu": gpu,
                            "allocated": allocated[name], "ready": any(c["type"] == "Ready" and c["status"] == "True" for c in node["status"].get("conditions", []))})
                samples = await asyncio.gather(*devices)
                observed = {line.split(",")[0].strip() for sample in samples for line in sample.get("compute_processes", []) if "gmx" in line.lower()}
                row = {"at": now(), "pods": owned, "nodes": summaries, "devices": samples,
                       "running_gpu_claims": sum(p["running_gpu_claims"] for p in owned), "observed_gromacs_devices": len(observed)}
                self.cluster.append(row)
                append(self.args.output / "cluster-observations.jsonl", row)
                counters = (row["running_gpu_claims"], len(observed), dict(Counter(p["phase"] for p in owned)))
                if counters != previous:
                    emit(running_gpu_claims=counters[0], observed_gromacs_devices=counters[1], pods=counters[2])
                    previous = counters
            except Exception as error:
                append(self.args.output / "cluster-observations.jsonl", {"at": now(), "error_type": type(error).__name__})
            try:
                await asyncio.wait_for(self.done.wait(), timeout=15)
            except TimeoutError:
                pass

    async def cohort(self, http, cohort):
        names = [name for name in self.jobs if name.startswith(f"cohort-{cohort}/")]
        responses = await asyncio.gather(*(self.submit(http, n) for n in names), return_exceptions=True)
        for name, response in zip(names, responses):
            if isinstance(response, Exception):
                self.jobs[name]["receipt"]["submit_error_type"] = type(response).__name__
                save(self.jobs[name]["out"] / "receipt.json", self.jobs[name]["receipt"])
        deadline, previous = time.monotonic() + self.args.timeout, None
        while time.monotonic() < deadline:
            outcomes = await asyncio.gather(*(self.observe(http, n) for n in names), return_exceptions=True)
            for name, outcome in zip(names, outcomes):
                if isinstance(outcome, Exception):
                    append(self.jobs[name]["out"] / "observation-errors.jsonl", {"at": now(), "error_type": type(outcome).__name__})
            counts = dict(Counter(self.jobs[n]["receipt"]["state"] for n in names))
            if counts != previous:
                emit(cohort=cohort, states=counts)
                previous = counts
            if all(self.jobs[n]["receipt"]["state"] in TERMINAL | {"verified", "submission_failed"}
                   and (self.jobs[n]["receipt"]["state"] != "succeeded" or self.jobs[n]["receipt"].get("result_published")) for n in names):
                break
            await asyncio.sleep(5)
        await self.drain(http, names)
        slots = asyncio.Semaphore(2)
        outcomes = await asyncio.gather(*(self.collect(http, n, slots) for n in names), return_exceptions=True)
        for name, outcome in zip(names, outcomes):
            if isinstance(outcome, Exception):
                self.jobs[name]["receipt"]["collection_error_type"] = type(outcome).__name__
                save(self.jobs[name]["out"] / "receipt.json", self.jobs[name]["receipt"])
        return all(self.jobs[n]["receipt"]["state"] == "verified" and self.jobs[n]["receipt"].get("idempotency_verified") for n in names)

    async def drain(self, http, names):
        for name in names:
            job, receipt = self.jobs[name], self.jobs[name]["receipt"]
            if not receipt.get("operation_id") or receipt["state"] in TERMINAL | {"verified", "collected_failure"}:
                continue
            await self.observe(http, name)
            if receipt["state"] not in TERMINAL:
                receipt["cancelled_by_test_deadline"] = True
                response = await http.delete("/v1/operations/" + receipt["operation_id"])
                response.raise_for_status()
                save(job["out"] / "cancel-response.json", response.json())
                save(job["out"] / "receipt.json", receipt)
        deadline = time.monotonic() + 240
        while time.monotonic() < deadline:
            pending = [n for n in names if self.jobs[n]["receipt"].get("operation_id") and self.jobs[n]["receipt"]["state"] not in TERMINAL | {"verified", "collected_failure"}]
            if not pending:
                return
            await asyncio.gather(*(self.observe(http, n) for n in pending))
            await asyncio.sleep(5)
        raise RuntimeError("Task-owned operations did not drain; retain IDs for closeout")

    async def execute(self):
        values = dict(line.split("=", 1) for line in self.args.qa_env.read_text().splitlines() if "=" in line)
        key = values["SCIENTIFIC_MODELS_API_KEY"]
        if not key.startswith("fs2_pat_" + QA_KEY_ID.replace("-", "")[:12]):
            raise ValueError("Only the existing system/qa key is accepted")
        secret = await self.kjson("-n", "fs2-system", "get", "secret", "fs2-serve-admin")
        admin_token = base64.b64decode(secret["data"]["token"]).decode().strip()
        async with httpx2.AsyncClient(base_url=self.args.origin, headers={"Authorization": "Bearer " + key}, timeout=90, trust_env=False) as http, \
                httpx2.AsyncClient(base_url=self.args.origin, headers={"Origin": self.args.origin, "X-Requested-With": "XMLHttpRequest"}, timeout=90, trust_env=False) as admin:
            response = await admin.post("/admin/api/v1/session", headers={"Authorization": "Bearer " + admin_token})
            response.raise_for_status()
            before = await self.key_metadata(admin)
            backup = self.args.output / "qa-policy-before.json"
            if not backup.exists():
                save(backup, before)
            original = load(backup)
            active = await active_operations(http)
            previous_ids = {r.get("operation_id") for p in self.args.output.glob("cohort-*/request-*/receipt.json") if (r := load(p))}
            if any(o["id"] not in previous_ids for o in active):
                raise ValueError("QA has unrelated/unresolved work; do not modify its policy")
            for c in range(1, self.args.cohorts + 1):
                for index in range(1, self.args.requests + 1):
                    await self.prepare(http, c, index)
                emit(cohort=c, phase="inputs_prepared", requests=self.args.requests)
            watchers = []
            try:
                response = await admin.patch("/admin/api/v1/keys/" + QA_KEY_ID, json={"max_concurrency": self.args.requests})
                response.raise_for_status()
                after = await self.key_metadata(admin)
                save(self.args.output / "qa-policy-during.json", after)
                for field in ("tenant_id", "principal_id", "models", "scopes", "expires_at"):
                    if after.get(field) != original.get(field):
                        raise ValueError("Unexpected internal identity/policy change")
                watchers = [asyncio.create_task(self.health_loop(http)), asyncio.create_task(self.cluster_loop())]
                verdicts = []
                for c in range(1, self.args.cohorts + 1):
                    verdicts.append(await self.cohort(http, c))
                    if not verdicts[-1]:
                        break
                save(self.args.output / "summary.json", {"at": now(), "tenant": "system", "principal": "qa",
                     "key_id": QA_KEY_ID, "cohorts_verified": verdicts, "requested_concurrency": self.args.requests,
                     "peak_running_gpu_claims": max((r["running_gpu_claims"] for r in self.cluster), default=0),
                     "peak_observed_gromacs_devices": max((r["observed_gromacs_devices"] for r in self.cluster), default=0),
                     "api_probe_count": len(self.health), "api_probe_failures": sum(r.get("http_status") != 200 for r in self.health),
                     "requests": {n: {k: v for k, v in j["receipt"].items() if k not in ("verified_artifacts", "native_outputs", "output_manifest")} for n, j in self.jobs.items()}})
            finally:
                try:
                    await self.drain(http, list(self.jobs))
                finally:
                    self.done.set()
                    if watchers:
                        await asyncio.gather(*watchers, return_exceptions=True)
                    current = await self.key_metadata(admin)
                    if current["max_concurrency"] not in (self.args.requests, original["max_concurrency"]):
                        raise RuntimeError("Concurrent policy edit detected; do not overwrite it")
                    response = await admin.patch("/admin/api/v1/keys/" + QA_KEY_ID,
                                                 json={"max_concurrency": original["max_concurrency"]})
                    response.raise_for_status()
                    save(self.args.output / "qa-policy-restored.json", await self.key_metadata(admin))
                    await admin.delete("/admin/api/v1/session")
                    emit(phase="qa_policy_restored", max_concurrency=original["max_concurrency"])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("client-root", "qa-env", "fixture", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--origin", default="https://89.169.99.188")
    parser.add_argument("--context", default="nebius-mk8s-k8s-inference-h100-e00j5z9te7x5dd9g6a")
    parser.add_argument("--requests", type=int, default=16, choices=range(1, 17))
    parser.add_argument("--cohorts", type=int, default=2, choices=(1, 2))
    parser.add_argument("--production-ps", type=int, default=1000)
    parser.add_argument("--timeout", type=int, default=1200)
    parser.add_argument("--campaign-id", default=TASK_ID)
    parser.add_argument("--seed-base", type=int, default=20261003)
    args = parser.parse_args()
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,100}", args.campaign_id):
        parser.error("campaign-id must be a bounded, path-safe identifier")
    os.umask(0o077)
    args.output.mkdir(parents=True, exist_ok=True)
    sys.path.insert(0, str(args.client_root))
    spec = importlib.util.spec_from_file_location("scientific_acceptance", args.client_root / "scripts/scientific-batch-acceptance.py")
    helper = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(helper)
    asyncio.run(Run(args, helper).execute())


if __name__ == "__main__":
    main()
