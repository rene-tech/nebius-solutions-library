"""Resume only the stopped MPI lane beside an unchanged, running REST matrix.

This borrower NEVER logs into admin, writes key policy, or calls Campaign.execute.
The existing matrix remains the only policy writer. It may restore two once its
REST pair drains; this lane then has at most one operation under that baseline.
Use a new campaign/output: the original failed MPI operation remains evidence.
"""
import argparse
import asyncio
import fcntl
import hashlib
import importlib.util
import os
from pathlib import Path
import re
import sys

import httpx2

from run_matrix import MPICase, MatrixCampaign, validate_mpi_case
from run_mpi import MPICampaign
from run_rest import Campaign
from verify_concurrency import QA_KEY_ID, active_operations, append, emit, load, now, save


def file_sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def process_identity(pid):
    """Bind the real local owner without copying its command/environment."""
    root = Path("/proc") / str(pid)
    try:
        stat = (root / "stat").read_text().rsplit(")", 1)[1].split()
        command = (root / "cmdline").read_bytes().split(b"\0")
    except FileNotFoundError:
        return None
    return {"pid": pid, "start_ticks": int(stat[19])}, [part.decode() for part in command if part]


def owner_identity(pid, output, campaign_id):
    found = process_identity(pid)
    if found is None:
        raise ValueError("Initial handoff requires the existing matrix process")
    identity, command = found
    if not any(Path(part).name == "run_matrix.py" for part in command):
        raise ValueError("Parent PID is not the matrix supervisor")
    for flag, expected in (("--output", str(output)), ("--campaign-id", campaign_id)):
        if command.count(flag) != 1 or command[command.index(flag) + 1] != expected:
            raise ValueError("Parent process does not own this exact matrix")
    return identity


def parent_snapshot(args):
    root = args.parent_output.resolve()
    if root == args.output.resolve() or root in args.output.resolve().parents:
        raise ValueError("Borrower output must be separate from every parent receipt")
    if file_sha(root / "matrix-plan.json") != args.parent_plan_sha256:
        raise ValueError("Parent matrix identity changed")
    plan = load(root / "matrix-plan.json")
    if (plan.get("schema") != "gromacs-mpinat-matrix/v1"
            or plan.get("maximum_owned_operations") != 3 or plan.get("rest_slots") != 2
            or plan.get("mpi_slots") != 1 or plan.get("restore_qa_concurrency") != 2
            or plan.get("steps") != 10000 or plan.get("repetitions") != 3
            or plan.get("fixture") != str(args.fixture.resolve()) or plan.get("origin") != args.origin
            or plan.get("rest_cases") != ["benchpep", "benchpep-h"]
            or plan.get("campaign_id") == args.campaign_id):
        raise ValueError("Parent is not the reviewed two-PEP/serial-MPI matrix, or campaign ID was reused")
    progress = load(root / "matrix-progress.json", {})
    if progress.get("mpi") != [{"cohort": 2, "verified": False}]:
        raise ValueError("Original MPI lane must be stopped after its first failed operation")
    peers = {}
    for case in plan["rest_cases"]:
        receipt = load(root / "cohort-1" / case / "receipt.json", {})
        if not receipt.get("operation_id"):
            raise ValueError("Both original PEP operation IDs must already exist")
        peers[case] = receipt["operation_id"]
    if len(set(peers.values())) != 2:
        raise ValueError("Parent PEP operation identities are not distinct")
    specs = plan["mpi_cases"]
    if not 1 <= len(specs) <= 24 or [row["cohort"] for row in specs] != list(range(2, 2 + len(specs))):
        raise ValueError("Invalid parent MPI sequence")
    for row in specs:
        validate_mpi_case(MPICase(**{key: value for key, value in row.items() if key != "cohort"}))
    failed = load(root / "cohort-2" / specs[0]["case"] / "receipt.json", {})
    if failed.get("operation_id") != args.failed_operation_id or failed.get("state") not in {"failed", "collected_failure"}:
        raise ValueError("Original failed MPI receipt must be retained unchanged")
    for row in specs[1:]:
        if load(root / f"cohort-{row['cohort']}" / row["case"] / "receipt.json", {}).get("operation_id"):
            raise ValueError("Original MPI supervisor progressed; do not create a competing lane")
    for name, limit in (("qa-policy-before.json", 2), ("qa-policy-during.json", 3)):
        policy = load(root / name, {})
        if (policy.get("id") != QA_KEY_ID or policy.get("tenant_id") != "system"
                or policy.get("principal_id") != "qa" or policy.get("max_concurrency") != limit):
            raise ValueError("Parent QA-policy evidence differs from the reviewed lease")
    return plan, peers, failed


def verify_rollout(deployment, replicasets, pods, expected_image):
    desired = deployment["spec"].get("replicas", 1)
    status = deployment.get("status", {})
    images = [c["image"] for c in deployment["spec"]["template"]["spec"]["containers"]
              if c["name"] == "control-plane"]
    if (images != [expected_image] or desired < 1
            or status.get("observedGeneration", 0) < deployment["metadata"]["generation"]
            or status.get("updatedReplicas") != desired or status.get("readyReplicas") != desired):
        raise ValueError("Expected exact API image is not fully rolled out")
    owners = {row["metadata"]["uid"] for row in replicasets["items"]
              if any(ref.get("uid") == deployment["metadata"]["uid"] and ref.get("controller") is True
                     for ref in row["metadata"].get("ownerReferences", []))}
    selected = [pod for pod in pods["items"]
                if pod.get("status", {}).get("phase") not in {"Succeeded", "Failed"}
                and any(ref.get("uid") in owners and ref.get("controller") is True
                        for ref in pod["metadata"].get("ownerReferences", []))]
    if len(selected) != desired or any(
            pod["metadata"].get("deletionTimestamp")
            or not any(c.get("type") == "Ready" and c.get("status") == "True"
                       for c in pod.get("status", {}).get("conditions", []))
            or [c["image"] for c in pod["spec"]["containers"] if c["name"] == "control-plane"] != [expected_image]
            for pod in selected):
        raise ValueError("API readers include an old, extra, terminating or unready Pod")
    return {"image": expected_image, "deployment_uid": deployment["metadata"]["uid"],
            "pods": [{"name": pod["metadata"]["name"], "uid": pod["metadata"]["uid"]} for pod in selected]}


class BorrowedMPI(MPICampaign):
    async def submit(self, http, run_id):
        try:
            await self.owner.guard(http)
        except Exception as error:
            self.owner.guard_error = error
            self.owner.guard_failed.set()
            raise
        await super().submit(http, run_id)

    async def cohort(self, http, cohort):
        # Campaign deliberately retains ordinary submission errors and keeps
        # observing. An ownership failure is different: stop future admissions
        # immediately, then let the borrower drain only its own MPI operations.
        worker = asyncio.create_task(super().cohort(http, cohort))
        unsafe = asyncio.create_task(self.owner.guard_failed.wait())
        try:
            await asyncio.wait((worker, unsafe), return_when=asyncio.FIRST_COMPLETED)
            if self.owner.guard_failed.is_set():
                raise self.owner.guard_error
            return await worker
        finally:
            for task in (worker, unsafe):
                if not task.done():
                    task.cancel()
            await asyncio.gather(worker, unsafe, return_exceptions=True)


class BorrowedLane(Campaign):
    wait_mpi_capacity = MatrixCampaign.wait_mpi_capacity

    def __init__(self, args, helper, matrix_lease):
        super().__init__(args, helper)
        self.matrix_lease = matrix_lease
        self.parent, self.peers, self.failed = parent_snapshot(args)
        declared = [row["cohort"] for row in self.parent["mpi_cases"]]
        order = args.cohort_order or declared
        if len(order) != len(declared) or set(order) != set(declared) or order[0] != 2:
            raise ValueError("Cohort order must contain every original MPI case once, with the failed 1x1 retry first")
        previous = load(args.output / "mpi-lane-plan.json")
        identity = {"schema": "gromacs-mpinat-borrowed-mpi-lane/v1", "campaign_id": args.campaign_id,
                    "parent_output": str(args.parent_output.resolve()), "parent_plan_sha256": args.parent_plan_sha256,
                    "peer_operations": self.peers, "prior_failed_operation": args.failed_operation_id,
                    "expected_api_image": args.expected_api_image, "mpi_cases": self.parent["mpi_cases"],
                    "execution_order": order,
                    "policy_writes": False, "maximum_mpi_operations": 1,
                    "original_failure_class": "platform_before_native_initialization",
                    "failure_evidence": str(args.parent_output / "cohort-2" / self.parent["mpi_cases"][0]["case"])}
        identity["parent_process"] = (previous["parent_process"] if previous else
            owner_identity(args.parent_pid, args.parent_output.resolve(), self.parent["campaign_id"]))
        if identity["parent_process"]["pid"] != args.parent_pid or previous is not None and previous != identity:
            raise ValueError("Borrower identity changed; use a new campaign/output")
        if previous is None and any(args.output.glob("cohort-*/*/receipt.json")):
            raise ValueError("Borrower must not adopt unbound previous receipts")
        self.identity = identity
        save(args.output / "mpi-lane-plan.json", identity)
        self.children, self.owners = [], {}
        self.guard_failed, self.guard_error = asyncio.Event(), None
        self.mpi_capacity_released = False
        args.mpi_capacity_gate = Path(self.parent["mpi_capacity_gate"]) if self.parent.get("mpi_capacity_gate") else None
        by_cohort = {row["cohort"]: row for row in self.parent["mpi_cases"]}
        for row in (by_cohort[cohort] for cohort in order):
            child_args = argparse.Namespace(**vars(args))
            child_args.cases, child_args.requests, child_args.cohorts = [row["case"]], 1, 1
            child_args.nodes, child_args.gpus_per_node = row["nodes"], row["gpus_per_node"]
            child_args.interface, child_args.protocol = row["interface"], row["protocol"]
            child_args.steps, child_args.repetitions = self.parent["steps"], self.parent["repetitions"]
            child_args.campaign_id = f"{args.campaign_id}-mpi-{row['cohort']}"
            if len(child_args.campaign_id) > 101:
                raise ValueError("Campaign ID leaves no space for MPI identities")
            child = BorrowedMPI(child_args, helper)
            child.owner = self
            self.children.append((row["cohort"], child))
            name = f"cohort-{row['cohort']}/{row['case']}"
            receipt = load(args.output / name / "receipt.json")
            if receipt is not None:
                job = {"out": args.output / name, "receipt": receipt,
                       "idem": f"{child_args.campaign_id}-{row['case']}"}
                child.jobs[name] = self.jobs[name] = job
                self.owners[name] = child

    async def execute(self):
        raise RuntimeError("Borrower must never enter the policy-owning Campaign.execute")

    async def guard(self, http):
        _, peers, _ = parent_snapshot(self.args)
        if peers != self.peers:
            raise ValueError("Parent PEP identities changed")
        active = await active_operations(http)
        own = {job["receipt"].get("operation_id") for job in self.jobs.values()} - {None}
        peer_ids = set(self.peers.values())
        active_ids = {row["id"] for row in active}
        if active_ids - peer_ids - own or len(active_ids & own) > 1 or len(active_ids) > 3:
            raise ValueError("Active QA work exceeds the exact two-PEP/one-MPI ownership bound")
        restored = load(self.args.parent_output / "qa-policy-restored.json")
        try:
            fcntl.flock(self.matrix_lease.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            acquired = True  # Keep it through closeout; prevent a second matrix owner.
        except BlockingIOError:
            acquired = False
        if restored is not None:
            if (restored.get("id") != QA_KEY_ID or restored.get("tenant_id") != "system"
                    or restored.get("principal_id") != "qa" or restored.get("max_concurrency") != 2
                    or active_ids & peer_ids):
                raise ValueError("Baseline handoff requires both PEPs drained and QA policy restored to two")
            phase = "baseline_two_with_one_mpi_slot"
        else:
            found = process_identity(self.args.parent_pid)
            if acquired or found is None or found[0] != self.identity["parent_process"]:
                raise ValueError("Original policy owner disappeared before safe baseline handoff")
            phase = "borrowed_third_slot_beside_parent_rest"
        # A different local matrix must not own the lock after the parent exits.
        found = process_identity(self.args.parent_pid)
        if not acquired and (found is None or found[0] != self.identity["parent_process"]):
            raise ValueError("Another local policy owner may hold the matrix lease")
        append(self.args.output / "mpi-lane-ownership.jsonl",
               {"at": now(), "phase": phase, "peer_active": len(active_ids & peer_ids),
                "own_active": len(active_ids & own), "policy_written": False})

    async def check_api_image(self):
        deployment, replicasets, pods = await asyncio.gather(
            self.kjson("-n", "fs2-system", "get", "deployment", "fs2-serve-control-plane"),
            self.kjson("-n", "fs2-system", "get", "replicasets"),
            self.kjson("-n", "fs2-system", "get", "pods"))
        proof = verify_rollout(deployment, replicasets, pods, self.args.expected_api_image)
        append(self.args.output / "api-image-checks.jsonl", {"at": now(), **proof})

    async def observe(self, http, run_id):
        await self.owners[run_id].observe(http, run_id)

    async def execute_lane(self):
        values = dict(line.split("=", 1) for line in self.args.qa_env.read_text().splitlines() if "=" in line)
        key = values["SCIENTIFIC_MODELS_API_KEY"]
        if not key.startswith("fs2_pat_" + QA_KEY_ID.replace("-", "")[:12]):
            raise ValueError("Only the existing system/qa key is accepted")
        async with httpx2.AsyncClient(base_url=self.args.origin, headers={"Authorization": "Bearer " + key},
                                     timeout=90, trust_env=False) as http:
            await self.guard(http)
            await self.check_api_image()
            http.event_hooks["request"].append(self.trace_request)
            http.event_hooks["response"].append(self.trace_response)
            for cohort, child in self.children:
                child.tracing_installed = True
                await child.prepare(http, cohort, 1)
                self.jobs.update(child.jobs)
                self.owners.update({name: child for name in child.jobs})
            first = next(iter(self.children[0][1].jobs.values()))["receipt"]
            if first.get("input_sha256") != self.failed.get("input_sha256"):
                raise ValueError("Retry input differs from the retained original MPI failure")
            await self.guard(http)
            watchers = [asyncio.create_task(self.health_loop(http)), asyncio.create_task(self.cluster_loop())]
            outcomes = {"started_at": now(), "prior_failed_operation": self.args.failed_operation_id,
                        "original_failure_preserved": True, "policy_written": False, "mpi": []}
            try:
                async with asyncio.timeout(self.args.timeout):
                    for cohort, child in self.children:
                        await self.guard(http)
                        await self.check_api_image()
                        await self.wait_mpi_capacity(cohort, child)
                        verified = await child.cohort(http, cohort)
                        outcomes["mpi"].append({"cohort": cohort, "verified": verified})
                        save(self.args.output / "mpi-lane-progress.json", outcomes)
                        if not verified:
                            break
            except Exception as error:
                outcomes["error_type"] = type(error).__name__
                raise
            finally:
                try:
                    # This dictionary contains only new MPI jobs, never the PEP pair.
                    await self.drain(http, list(self.jobs))
                finally:
                    self.done.set()
                    await asyncio.gather(*watchers, return_exceptions=True)
                    outcomes["finished_at"] = now()
                    outcomes["all_verified"] = (len(outcomes["mpi"]) == len(self.children)
                                                 and all(row["verified"] for row in outcomes["mpi"]))
                    save(self.args.output / "mpi-lane-progress.json", outcomes)
                    emit(phase="mpi_lane_finished", all_verified=outcomes["all_verified"], policy_written=False)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for field in ("client-root", "qa-env", "fixture", "output", "parent-output"):
        parser.add_argument("--" + field, type=Path, required=True)
    parser.add_argument("--campaign-id", required=True)
    parser.add_argument("--parent-pid", type=int, required=True)
    parser.add_argument("--parent-plan-sha256", required=True)
    parser.add_argument("--failed-operation-id", required=True)
    parser.add_argument("--expected-api-image", required=True)
    parser.add_argument("--cohort-order", type=lambda value: [int(item) for item in value.split(",")],
                        help="Explicit permutation of parent MPI cohorts; retry cohort2 first, e.g. 2,3,5,6,7,9,4,8")
    parser.add_argument("--origin", default="https://89.169.99.188")
    parser.add_argument("--context", default="nebius-mk8s-k8s-inference-h100-e00j5z9te7x5dd9g6a")
    parser.add_argument("--timeout", type=int, default=21600)
    args = parser.parse_args()
    if (not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,80}", args.campaign_id)
            or not re.fullmatch(r"[a-f0-9]{64}", args.parent_plan_sha256)
            or not re.fullmatch(r"[^\s]+@sha256:[a-f0-9]{64}", args.expected_api_image)
            or args.parent_pid < 1 or not 1 <= args.timeout <= 21600):
        parser.error("Use bounded identifiers, an immutable API digest and at most six hours")
    os.umask(0o077)
    args.output.mkdir(mode=0o700, parents=True, exist_ok=True)
    lock_root = args.qa_env.resolve().parent
    with (lock_root / "gromacs-mpi-lane-system-qa.lock").open("a") as lane_lease, \
            (lock_root / "gromacs-matrix-system-qa.lock").open("a") as matrix_lease:
        try:
            fcntl.flock(lane_lease.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            parser.error("Another serial MPI borrower owns this QA lane")
        sys.path.insert(0, str(args.client_root))
        spec = importlib.util.spec_from_file_location(
            "scientific_acceptance", args.client_root / "scripts/scientific-batch-acceptance.py")
        helper = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(helper)
        lane = BorrowedLane(args, helper, matrix_lease)
        asyncio.run(lane.execute_lane())


if __name__ == "__main__":
    main()
