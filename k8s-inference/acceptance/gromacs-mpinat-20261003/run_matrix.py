"""Two single-GPU REST cases beside one serial MPI qualification lane.

One inherited Campaign.execute owns the QA admission-policy change, health and
GPU observers, final drain, and restoration to two. Child campaigns never call
execute. This is a functional/concurrent-isolation matrix, not uncontended
strong-scaling evidence. Stop any previous QA policy owner before starting it.
"""
import argparse
import asyncio
from dataclasses import asdict, dataclass
import fcntl
import importlib.util
import json
import os
from pathlib import Path
import re
import sys

from run_mpi import MPICampaign, PROTOCOLS, shape_parameters
from run_rest import Campaign
from verify_concurrency import active_operations, append, emit, load, now, save


@dataclass(frozen=True)
class MPICase:
    case: str
    interface: str
    nodes: int
    gpus_per_node: int
    protocol: str = "auto"


DEFAULT_MPI_CASES = tuple(
    MPICase("benchmem", interface, nodes, gpus)
    for interface in ("rest", "mcp")
    for nodes, gpus in ((1, 1), (1, 2), (1, 4), (2, 1))
)


def parse_mpi_case(value):
    """CASE:INTERFACE:NODES:GPUS_PER_NODE:PROTOCOL, without placement knobs."""
    try:
        case, interface, nodes, gpus, protocol = value.split(":")
        parsed = MPICase(case, interface, int(nodes), int(gpus), protocol)
        validate_mpi_case(parsed)
        return parsed
    except (TypeError, ValueError) as error:
        raise argparse.ArgumentTypeError(
            "Use CASE:rest|mcp:NODES:GPUS_PER_NODE:auto|fixed-cpu-pme|fixed-gpu-pme"
        ) from error


def validate_mpi_case(case):
    if (not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,100}", case.case)
            or case.interface not in ("rest", "mcp") or case.protocol not in PROTOCOLS):
        raise ValueError("Invalid MPI case identity or protocol")
    shape_parameters({"id": case.case}, case.nodes, case.gpus_per_node, protocol=case.protocol)


class MatrixCampaign(Campaign):
    def __init__(self, args, helper):
        if (not 1 <= len(args.cases) <= 2 or len(set(args.cases)) != len(args.cases)
                or not 1 <= len(args.mpi_cases) <= 24
                or len(set(args.mpi_cases)) != len(args.mpi_cases)):
            raise ValueError("Use one or two distinct REST cases and one to 24 distinct MPI cases")
        for identifier in (args.campaign_id, *args.cases):
            if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,100}", identifier):
                raise ValueError("Campaign and fixture IDs must be path-safe")
        for case in args.mpi_cases:
            validate_mpi_case(case)
        # All records are visible to Run.execute's existing cohort-* ownership
        # recovery guard. Cohort 1 is REST-D; cohorts 2+ are serial MPI cases.
        args.cohorts, args.requests = 1, 3
        super().__init__(args, helper)
        rest_args = argparse.Namespace(**vars(args))
        rest_args.requests = min(2, len(args.cases))
        self.rest = Campaign(rest_args, helper)
        self.mpi = []
        for cohort, case in enumerate(args.mpi_cases, 2):
            mpi_args = argparse.Namespace(**vars(args))
            mpi_args.cases, mpi_args.requests = [case.case], 1
            mpi_args.nodes, mpi_args.gpus_per_node = case.nodes, case.gpus_per_node
            mpi_args.interface, mpi_args.protocol = case.interface, case.protocol
            mpi_args.campaign_id = f"{args.campaign_id}-mpi-{cohort}"
            if len(mpi_args.campaign_id) > 101:
                raise ValueError("Campaign ID leaves no room for bounded MPI identities")
            self.mpi.append((cohort, MPICampaign(mpi_args, helper)))
        self.owners = {}
        self.policy_checked = False
        self.mpi_capacity_released = False

    def manifest(self):
        return {"schema": "gromacs-mpinat-matrix/v1", "campaign_id": self.args.campaign_id,
                "fixture": str(self.args.fixture.resolve()), "origin": self.args.origin,
                "rest_cases": self.args.cases, "rest_slots": self.rest.args.requests,
                "mpi_cases": [{"cohort": cohort, **asdict(case)}
                              for cohort, case in enumerate(self.args.mpi_cases, 2)],
                "mpi_slots": 1, "maximum_owned_operations": 3,
                "steps": self.args.steps, "repetitions": self.args.repetitions,
                "uncontended_scaling_claimed": False, "restore_qa_concurrency": 2,
                "mpi_capacity_gate": str(self.args.mpi_capacity_gate.resolve())
                if getattr(self.args, "mpi_capacity_gate", None) else None}

    def prepared_requests(self):
        # The parent policy lifecycle prepares the entire fixed matrix once.
        return 1

    async def prepare(self, http, cohort, index):
        if (cohort, index) != (1, 1) or self.jobs:
            raise ValueError("Matrix preparation must run exactly once")
        http.event_hooks["request"].append(self.trace_request)
        http.event_hooks["response"].append(self.trace_response)
        for child_cohort, child in [(1, self.rest), *self.mpi]:
            child.tracing_installed = True
            for case_index in range(1, len(child.args.cases) + 1):
                await child.prepare(http, child_cohort, case_index)
            for run_id, job in child.jobs.items():
                if run_id in self.jobs:
                    raise ValueError("Duplicate matrix job identity")
                self.jobs[run_id], self.owners[run_id] = job, child
        # The inherited initial guard accepts any owned receipt in this root.
        # Tighten that to this exact resumed matrix before borrowing its policy.
        active = await active_operations(http)
        selected = {j["receipt"].get("operation_id") for j in self.jobs.values()} - {None}
        if any(operation["id"] not in selected for operation in active):
            raise ValueError("Active QA operation is outside the selected matrix")
        rest_ids = {j["receipt"].get("operation_id") for j in self.rest.jobs.values()}
        if (len(active) > 3 or sum(o["id"] in rest_ids for o in active) > self.rest.args.requests
                or sum(o["id"] not in rest_ids for o in active) > 1):
            raise ValueError("Existing work exceeds the two-REST/one-MPI lane bounds")

    async def key_metadata(self, admin):
        value = await super().key_metadata(admin)
        if not self.policy_checked:
            original = load(self.args.output / "qa-policy-before.json")
            if original is not None and original.get("max_concurrency") != 2:
                raise ValueError("Matrix must restore the reviewed QA concurrency of two")
            allowed = (2, 3) if original is not None else (2,)
            if value.get("max_concurrency") not in allowed:
                raise ValueError("Unexpected QA limit or competing policy owner")
            self.policy_checked = True
        return value

    async def observe(self, http, run_id):
        await self.owners[run_id].observe(http, run_id)

    async def wait_mpi_capacity(self, cohort, child):
        path = getattr(self.args, "mpi_capacity_gate", None)
        if path is None or self.mpi_capacity_released or child.args.nodes * child.args.gpus_per_node < 2:
            return

        def record(event):
            row = {"at": now(), "event": event, "cohort": cohort, "gate": str(path),
                   "timeout_seconds": self.args.timeout}
            append(self.args.output / "matrix-capacity-gate.jsonl", row)
            emit(phase="mpi_capacity_gate", **row)

        async def released():
            while True:
                try:
                    value = load(path) if path.is_file() and path.stat().st_size <= 4096 else None
                except json.JSONDecodeError:
                    value = None  # A writer may still be replacing the small marker.
                if isinstance(value, dict) and value.get("released") is True:
                    return
                await asyncio.sleep(1)

        record("waiting")
        try:
            await asyncio.wait_for(released(), timeout=self.args.timeout)
        except TimeoutError:
            record("timeout")
            raise
        self.mpi_capacity_released = True
        record("released")

    async def cohort(self, http, cohort):
        if cohort != 1:
            raise ValueError("The matrix has one shared policy lifecycle")
        outcomes = {"started_at": now(), "mpi": []}

        async def run_child(child, child_cohort):
            try:
                return {"cohort": child_cohort, "verified": await child.cohort(http, child_cohort)}
            except Exception as error:
                # Keep the other lane supervised until it finishes; do not let
                # an exception unwind into final draining while it still submits.
                return {"cohort": child_cohort, "verified": False, "error_type": type(error).__name__}

        async def rest_lane():
            outcomes["rest"] = await run_child(self.rest, 1)

        async def mpi_lane():
            for child_cohort, child in self.mpi:
                try:
                    await self.wait_mpi_capacity(child_cohort, child)
                except Exception as error:
                    outcomes["mpi"].append({"cohort": child_cohort, "verified": False,
                                            "error_type": type(error).__name__, "phase": "capacity_gate"})
                    break
                result = await run_child(child, child_cohort)
                outcomes["mpi"].append(result)
                save(self.args.output / "matrix-progress.json", outcomes)
                if not result["verified"]:
                    break

        async with asyncio.TaskGroup() as group:
            group.create_task(rest_lane())
            group.create_task(mpi_lane())
        outcomes["finished_at"] = now()
        outcomes["all_verified"] = (outcomes["rest"]["verified"]
                                    and len(outcomes["mpi"]) == len(self.mpi)
                                    and all(row["verified"] for row in outcomes["mpi"]))
        save(self.args.output / "matrix-progress.json", outcomes)
        return outcomes["all_verified"]

    async def execute(self):
        identity = self.manifest()
        path = self.args.output / "matrix-plan.json"
        previous = load(path)
        if previous is not None and previous != identity:
            raise ValueError("Existing matrix identity differs; use a fresh output/campaign")
        if previous is None and any(self.args.output.glob("cohort-*/*/receipt.json")):
            raise ValueError("Use a fresh matrix output; preserve prior campaign receipts separately")
        save(path, identity)
        await super().execute()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for field in ("client-root", "qa-env", "fixture", "output"):
        parser.add_argument("--" + field, type=Path, required=True)
    parser.add_argument("--campaign-id", required=True)
    parser.add_argument("--rest-cases", default="benchpep,benchpep-h")
    parser.add_argument("--mpi-case", action="append", type=parse_mpi_case,
                        help="Repeat CASE:INTERFACE:NODES:GPUS_PER_NODE:PROTOCOL; defaults to eight MEM cases")
    parser.add_argument("--origin", default="https://89.169.99.188")
    parser.add_argument("--context", default="nebius-mk8s-k8s-inference-h100-e00j5z9te7x5dd9g6a")
    parser.add_argument("--timeout", type=int, default=21600)
    parser.add_argument("--mpi-capacity-gate", type=Path,
                        help="Before the first >=2-GPU MPI case, wait for this task-owned JSON {released:true}; REST continues")
    parser.add_argument("--steps", type=int, default=10000, choices=(10000,))
    parser.add_argument("--repetitions", type=int, default=3, choices=(3,))
    args = parser.parse_args()
    args.cases = args.rest_cases.split(",")
    args.mpi_cases = tuple(args.mpi_case or DEFAULT_MPI_CASES)
    os.umask(0o077)
    args.output.mkdir(parents=True, exist_ok=True)
    # This bounded local lock prevents two matrix supervisors for the same QA
    # credential file. It is not a distributed lease or permission to displace
    # other QA users; the inherited API ownership guard must also pass.
    with (args.qa_env.resolve().parent / "gromacs-matrix-system-qa.lock").open("a") as lease:
        try:
            fcntl.flock(lease.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            parser.error("Another matrix supervisor holds this QA lease")
        sys.path.insert(0, str(args.client_root))
        spec = importlib.util.spec_from_file_location(
            "scientific_acceptance", args.client_root / "scripts/scientific-batch-acceptance.py")
        helper = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(helper)
        campaign = MatrixCampaign(args, helper)
        emit(phase="matrix_start", maximum_owned_operations=3, rest_slots=campaign.rest.args.requests,
             mpi_slots=1, uncontended_scaling_claimed=False, agent_qualification_claimed=False)
        asyncio.run(campaign.execute())


if __name__ == "__main__":
    main()
