import argparse
import asyncio
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from run_mpi_lane import BorrowedLane, file_sha, parent_snapshot, verify_rollout
from verify_concurrency import QA_KEY_ID, load, save


IMAGE = "registry.example/api@sha256:" + "a" * 64
OWNER = {"pid": 123, "start_ticks": 456}


def inputs(root):
    parent, output = root / "parent", root / "borrower"
    parent.mkdir()
    output.mkdir()
    fixture = root / "fixtures"
    plan = {"schema": "gromacs-mpinat-matrix/v1", "campaign_id": "original-matrix",
            "maximum_owned_operations": 3, "rest_slots": 2, "mpi_slots": 1,
            "restore_qa_concurrency": 2, "steps": 10000, "repetitions": 3,
            "fixture": str(fixture), "origin": "https://example.invalid",
            "rest_cases": ["benchpep", "benchpep-h"], "mpi_capacity_gate": None,
            "mpi_cases": [{"cohort": i, "case": "benchmem", "interface": "rest",
                           "nodes": 1, "gpus_per_node": i - 1, "protocol": "auto"} for i in (2, 3)]}
    save(parent / "matrix-plan.json", plan)
    save(parent / "matrix-progress.json", {"mpi": [{"cohort": 2, "verified": False}]})
    for case in plan["rest_cases"]:
        save(parent / "cohort-1" / case / "receipt.json", {"operation_id": case, "state": "running"})
    save(parent / "cohort-2/benchmem/receipt.json", {"operation_id": "original-failure", "state": "failed",
                                                 "input_sha256": "original-input"})
    for name, limit in (("before", 2), ("during", 3)):
        save(parent / f"qa-policy-{name}.json", policy(limit))
    args = argparse.Namespace(parent_output=parent, output=output, fixture=fixture,
                              origin=plan["origin"], parent_plan_sha256=file_sha(parent / "matrix-plan.json"),
                              campaign_id="new-mpi-campaign", failed_operation_id="original-failure",
                              parent_pid=123, expected_api_image=IMAGE, timeout=1,
                              cohort_order=None,
                              qa_env=root / "qa.env", context="unused", client_root=root)
    args.qa_env.write_text("SCIENTIFIC_MODELS_API_KEY=fs2_pat_56130b22ae09_test\n")
    return args


def policy(limit):
    return {"id": QA_KEY_ID, "tenant_id": "system", "principal_id": "qa", "max_concurrency": limit}


def lane(args):
    with patch("run_mpi_lane.owner_identity", return_value=OWNER):
        return BorrowedLane(args, None, SimpleNamespace(fileno=lambda: 900))


def rollout():
    deployment = {"metadata": {"uid": "deployment", "generation": 3},
                  "spec": {"replicas": 2, "template": {"spec": {"containers": [
                      {"name": "control-plane", "image": IMAGE}]}}},
                  "status": {"observedGeneration": 3, "readyReplicas": 2, "updatedReplicas": 2}}
    replicasets = {"items": [{"metadata": {"uid": "rs", "ownerReferences": [
        {"uid": "deployment", "controller": True}]}}]}
    pods = {"items": [{"metadata": {"name": f"pod-{i}", "uid": f"uid-{i}", "ownerReferences": [
        {"uid": "rs", "controller": True}]}, "status": {"phase": "Running", "conditions": [
            {"type": "Ready", "status": "True"}]}, "spec": {"containers": [
                {"name": "control-plane", "image": IMAGE}]}} for i in (1, 2)]}
    return deployment, replicasets, pods


class Identity(unittest.TestCase):
    def test_retains_original_failure_and_peps_without_changing_parent(self):
        with tempfile.TemporaryDirectory() as directory:
            args = inputs(Path(directory))
            before = {str(p): p.read_bytes() for p in args.parent_output.rglob("*") if p.is_file()}
            borrower = lane(args)
            self.assertEqual(borrower.identity["prior_failed_operation"], "original-failure")
            self.assertFalse(borrower.identity["policy_writes"])
            self.assertEqual([c.args.requests for _, c in borrower.children], [1, 1])
            self.assertTrue(all(c.args.campaign_id.startswith("new-mpi-campaign-") for _, c in borrower.children))
            self.assertEqual(before, {str(p): p.read_bytes() for p in args.parent_output.rglob("*") if p.is_file()})

    def test_parent_drift_progress_duplicate_id_and_same_output_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            args = inputs(Path(directory))
            args.campaign_id = "original-matrix"
            with self.assertRaisesRegex(ValueError, "campaign ID"):
                parent_snapshot(args)
            args.campaign_id = "new"
            save(args.parent_output / "cohort-3/benchmem/receipt.json", {"operation_id": "unexpected"})
            with self.assertRaisesRegex(ValueError, "progressed"):
                parent_snapshot(args)
            args.parent_plan_sha256 = "wrong"
            with self.assertRaisesRegex(ValueError, "identity changed"):
                parent_snapshot(args)
            args.output = args.parent_output / "nested"
            with self.assertRaisesRegex(ValueError, "separate"):
                parent_snapshot(args)

    def test_resume_recognizes_existing_own_operation_but_freezes_api_image(self):
        with tempfile.TemporaryDirectory() as directory:
            args = inputs(Path(directory))
            lane(args)
            save(args.output / "cohort-2/benchmem/receipt.json", {"operation_id": "own-existing", "state": "running"})
            resumed = lane(args)
            self.assertEqual(resumed.jobs["cohort-2/benchmem"]["receipt"]["operation_id"], "own-existing")
            args.expected_api_image = IMAGE.replace("a" * 64, "b" * 64)
            with self.assertRaisesRegex(ValueError, "Borrower identity changed"):
                lane(args)

    def test_exact_rollout_rejects_mixed_or_terminating_or_unready_readers(self):
        for field in ("image", "terminating", "ready", "generation"):
            with self.subTest(field=field):
                deployment, rs, pods = rollout()
                self.assertEqual(len(verify_rollout(deployment, rs, pods, IMAGE)["pods"]), 2)
                if field == "image":
                    pods["items"][0]["spec"]["containers"][0]["image"] = "old"
                elif field == "terminating":
                    pods["items"][0]["metadata"]["deletionTimestamp"] = "now"
                elif field == "ready":
                    pods["items"][0]["status"]["conditions"][0]["status"] = "False"
                else:
                    deployment["status"]["observedGeneration"] = 2
                with self.assertRaises(ValueError):
                    verify_rollout(deployment, rs, pods, IMAGE)

    def test_explicit_order_is_frozen_and_cannot_skip_or_duplicate_a_case(self):
        with tempfile.TemporaryDirectory() as directory:
            args = inputs(Path(directory))
            args.cohort_order = [2, 2]
            with self.assertRaisesRegex(ValueError, "every original MPI case"):
                lane(args)
            args.cohort_order = [2, 3]
            self.assertEqual(lane(args).identity["execution_order"], [2, 3])
            args.cohort_order = [3, 2]
            with self.assertRaisesRegex(ValueError, "retry first"):
                lane(args)


class Lifecycle(unittest.IsolatedAsyncioTestCase):
    async def test_guard_accepts_only_two_original_peps_plus_one_own_mpi(self):
        with tempfile.TemporaryDirectory() as directory:
            borrower = lane(inputs(Path(directory)))
            borrower.jobs = {"own": {"receipt": {"operation_id": "mine"}}}
            active = [{"id": name} for name in ("benchpep", "benchpep-h", "mine")]
            with patch("run_mpi_lane.active_operations", new=AsyncMock(return_value=active)), \
                    patch("run_mpi_lane.process_identity", return_value=(OWNER, [])), \
                    patch("run_mpi_lane.fcntl.flock", side_effect=BlockingIOError):
                await borrower.guard(None)
                active.append({"id": "unrelated"})
                with self.assertRaisesRegex(ValueError, "ownership bound"):
                    await borrower.guard(None)

    async def test_parent_loss_and_early_restore_fail_closed_without_policy_writes(self):
        with tempfile.TemporaryDirectory() as directory:
            args = inputs(Path(directory))
            borrower = lane(args)
            with patch("run_mpi_lane.active_operations", new=AsyncMock(return_value=[{"id": "benchpep"}])), \
                    patch("run_mpi_lane.process_identity", return_value=None), \
                    patch("run_mpi_lane.fcntl.flock"):
                with self.assertRaisesRegex(ValueError, "disappeared"):
                    await borrower.guard(None)
                save(args.parent_output / "qa-policy-restored.json", policy(2))
                with self.assertRaisesRegex(ValueError, "both PEPs drained"):
                    await borrower.guard(None)
            with self.assertRaisesRegex(RuntimeError, "never enter"):
                await borrower.execute()

    async def test_parent_finished_handoff_keeps_one_mpi_under_restored_two(self):
        with tempfile.TemporaryDirectory() as directory:
            args = inputs(Path(directory))
            borrower = lane(args)
            save(args.parent_output / "qa-policy-restored.json", policy(2))
            borrower.jobs = {"own": {"receipt": {"operation_id": "mine"}}}
            with patch("run_mpi_lane.active_operations", new=AsyncMock(return_value=[{"id": "mine"}])), \
                    patch("run_mpi_lane.process_identity", return_value=None), patch("run_mpi_lane.fcntl.flock"):
                await borrower.guard(None)
            evidence = json.loads((args.output / "mpi-lane-ownership.jsonl").read_text())
            self.assertEqual(evidence["phase"], "baseline_two_with_one_mpi_slot")
            self.assertFalse(evidence["policy_written"])

    async def test_guard_failure_stops_retry_loop_immediately(self):
        with tempfile.TemporaryDirectory() as directory:
            borrower = lane(inputs(Path(directory)))
            child = borrower.children[0][1]
            borrower.guard = AsyncMock(side_effect=ValueError("foreign work"))

            async def ordinary_cohort(instance, http, cohort):
                try:
                    await instance.submit(http, "unused")
                except ValueError:
                    pass  # The reused cohort normally retains submission errors.
                await asyncio.sleep(60)

            with patch("run_mpi_lane.MPICampaign.cohort", ordinary_cohort):
                with self.assertRaisesRegex(ValueError, "foreign work"):
                    await asyncio.wait_for(child.cohort(None, 2), timeout=0.1)

    async def test_one_serial_lane_no_admin_calls_or_parent_cancellation(self):
        with tempfile.TemporaryDirectory() as directory:
            args = inputs(Path(directory))
            borrower = lane(args)
            borrower.guard, borrower.check_api_image = AsyncMock(), AsyncMock()
            borrower.health_loop, borrower.cluster_loop = AsyncMock(), AsyncMock()
            events = []

            class Client:
                def __init__(self, **kwargs):
                    self.event_hooks = {"request": [], "response": []}

                async def __aenter__(self):
                    return self

                async def __aexit__(self, *_args):
                    return None

                def __getattr__(self, name):
                    raise AssertionError("No policy/admin/network action expected: " + name)

            for _, child in borrower.children:
                async def prepare(http, cohort, index, child=child):
                    name = f"cohort-{cohort}/benchmem"
                    child.jobs[name] = {"out": args.output / name, "receipt": {"input_sha256": "original-input"}}

                async def cohort(http, cohort, child=child):
                    events.append(cohort)
                    child.jobs[f"cohort-{cohort}/benchmem"]["receipt"].update(
                        operation_id=f"own-mpi-{cohort}", state="verified")
                    await asyncio.sleep(0)
                    return True

                child.prepare, child.cohort = prepare, cohort
                child.execute = AsyncMock(side_effect=AssertionError("Do not create a second policy owner"))

            async def drain(http, names):
                self.assertEqual(names, ["cohort-2/benchmem", "cohort-3/benchmem"])
                self.assertFalse(any("pep" in name for name in names))

            borrower.drain = drain
            with patch("run_mpi_lane.httpx2.AsyncClient", Client):
                await borrower.execute_lane()
            self.assertEqual(events, [2, 3])
            self.assertTrue(load(args.output / "mpi-lane-progress.json")["all_verified"])
            self.assertEqual(borrower.check_api_image.await_count, 3)
            self.assertFalse((args.output / "qa-policy-before.json").exists())


if __name__ == "__main__":
    unittest.main()
