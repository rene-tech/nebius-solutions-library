import argparse
import asyncio
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from run_matrix import DEFAULT_MPI_CASES, MPICase, MatrixCampaign, parse_mpi_case
from verify_concurrency import QA_KEY_ID, load, save


def arguments(root, mpi_cases=None):
    return argparse.Namespace(output=root, fixture=root / "fixtures", origin="https://example.invalid",
                              qa_env=root / "qa.env", campaign_id="test-matrix", context="test-context",
                              cases=["benchpep", "benchpep-h"], steps=10000, repetitions=3, timeout=30,
                              mpi_cases=tuple(mpi_cases or DEFAULT_MPI_CASES[:2]))


def response(value):
    return SimpleNamespace(raise_for_status=lambda: None, json=lambda: value)


class MatrixIdentity(unittest.TestCase):
    def test_default_is_exact_eight_functional_cases_without_large_nodes(self):
        self.assertEqual([(case.interface, case.nodes, case.gpus_per_node) for case in DEFAULT_MPI_CASES],
                         [(interface, nodes, gpus) for interface in ("rest", "mcp")
                          for nodes, gpus in ((1, 1), (1, 2), (1, 4), (2, 1))])
        self.assertTrue(all(case.case == "benchmem" and case.protocol == "auto" for case in DEFAULT_MPI_CASES))

    def test_parse_rejects_unsupported_shape_or_path(self):
        self.assertEqual(parse_mpi_case("benchmem:mcp:1:4:auto"), MPICase("benchmem", "mcp", 1, 4))
        for value in ("../mem:rest:1:1:auto", "benchmem:rest:3:8:auto", "benchmem:raw:1:1:auto",
                      "benchmem:rest:1:3:auto", "benchmem:rest:1:1:invented"):
            with self.assertRaises(argparse.ArgumentTypeError):
                parse_mpi_case(value)

    def test_bounded_slots_distinct_cohorts_and_shared_output(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            campaign = MatrixCampaign(arguments(root), None)
            self.assertEqual(campaign.args.requests, 3)
            self.assertEqual(campaign.rest.args.requests, 2)
            self.assertEqual([cohort for cohort, _ in campaign.mpi], [2, 3])
            self.assertTrue(all(child.args.requests == 1 and child.args.output == root
                                for _, child in campaign.mpi))
            self.assertEqual(campaign.prepared_requests(), 1)
            self.assertFalse(campaign.manifest()["uncontended_scaling_claimed"])

    def test_too_many_rest_cases_or_duplicate_mpi_cases_fail(self):
        with tempfile.TemporaryDirectory() as directory:
            args = arguments(Path(directory))
            args.cases.append("benchmem")
            with self.assertRaises(ValueError):
                MatrixCampaign(args, None)
            args = arguments(Path(directory), [DEFAULT_MPI_CASES[0]] * 2)
            with self.assertRaises(ValueError):
                MatrixCampaign(args, None)


class MatrixExecution(unittest.IsolatedAsyncioTestCase):
    async def test_capacity_gate_leaves_rest_and_one_gpu_running_then_releases_serial_lane(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            args = arguments(root)
            args.mpi_capacity_gate = root / "release.json"
            campaign = MatrixCampaign(args, None)
            events = []

            async def rest(http, cohort):
                while not (root / "matrix-capacity-gate.jsonl").exists():
                    await asyncio.sleep(0)
                events.append("rest_while_waiting")
                save(args.mpi_capacity_gate, {"released": True})
                return True

            async def mpi(http, cohort):
                events.append(f"mpi-{cohort}")
                return True

            campaign.rest.cohort = rest
            for _, child in campaign.mpi:
                child.cohort = mpi
            self.assertTrue(await campaign.cohort(None, 1))
            self.assertEqual(events, ["mpi-2", "rest_while_waiting", "mpi-3"])
            self.assertTrue(campaign.mpi_capacity_released)
            rows = (root / "matrix-capacity-gate.jsonl").read_text().splitlines()
            self.assertEqual(len(rows), 2)
            self.assertIn('"event": "waiting"', rows[0])
            self.assertIn('"event": "released"', rows[1])

    async def test_capacity_gate_timeout_does_not_submit_multigpu_or_cancel_rest(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            args = arguments(root)
            args.mpi_capacity_gate, args.timeout = root / "release.json", 0.01
            save(args.mpi_capacity_gate, {"released": "true"})
            campaign = MatrixCampaign(args, None)
            campaign.rest.cohort = AsyncMock(return_value=True)
            for _, child in campaign.mpi:
                child.cohort = AsyncMock(return_value=True)
            self.assertFalse(await campaign.cohort(None, 1))
            campaign.rest.cohort.assert_awaited_once()
            campaign.mpi[0][1].cohort.assert_awaited_once()
            campaign.mpi[1][1].cohort.assert_not_called()
            outcome = load(root / "matrix-progress.json")
            self.assertEqual(outcome["mpi"][-1]["error_type"], "TimeoutError")
            self.assertEqual(outcome["mpi"][-1]["phase"], "capacity_gate")

    async def test_matrix_identity_drift_and_prior_receipts_fail_before_live_actions(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            campaign = MatrixCampaign(arguments(root), None)
            save(root / "matrix-plan.json", {**campaign.manifest(), "repetitions": 4})
            campaign.kjson = AsyncMock()
            with self.assertRaisesRegex(ValueError, "identity differs"):
                await campaign.execute()
            campaign.kjson.assert_not_called()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            save(root / "cohort-1/benchpep/receipt.json", {"operation_id": "old-failure"})
            campaign = MatrixCampaign(arguments(root), None)
            with self.assertRaisesRegex(ValueError, "fresh matrix output"):
                await campaign.execute()
            self.assertEqual(load(root / "cohort-1/benchpep/receipt.json"), {"operation_id": "old-failure"})

    async def test_preparation_registers_all_jobs_once_and_one_trace_hook(self):
        with tempfile.TemporaryDirectory() as directory:
            campaign = MatrixCampaign(arguments(Path(directory)), None)
            children = [campaign.rest, *(child for _, child in campaign.mpi)]
            for child in children:
                async def prepare(http, cohort, index, child=child):
                    name = f"cohort-{cohort}/{child.args.cases[index - 1]}"
                    child.jobs[name] = {"receipt": {"state": "prepared"}, "out": child.args.output / name}
                child.prepare = prepare
            http = SimpleNamespace(event_hooks={"request": [], "response": []})
            with patch("run_matrix.active_operations", new=AsyncMock(return_value=[])):
                await campaign.prepare(http, 1, 1)
            self.assertEqual(len(campaign.jobs), 4)
            self.assertEqual(len(http.event_hooks["request"]), 1)
            self.assertEqual(len(http.event_hooks["response"]), 1)
            self.assertEqual(set(campaign.jobs), set(campaign.owners))
            with self.assertRaisesRegex(ValueError, "exactly once"):
                await campaign.prepare(http, 1, 1)

    async def test_unselected_active_operation_is_not_adopted(self):
        with tempfile.TemporaryDirectory() as directory:
            campaign = MatrixCampaign(arguments(Path(directory)), None)
            for child in [campaign.rest, *(child for _, child in campaign.mpi)]:
                child.prepare = AsyncMock()
            http = SimpleNamespace(event_hooks={"request": [], "response": []})
            with patch("run_matrix.active_operations", new=AsyncMock(return_value=[{"id": "unselected"}])):
                with self.assertRaisesRegex(ValueError, "outside the selected matrix"):
                    await campaign.prepare(http, 1, 1)

    async def test_single_policy_lifecycle_three_operations_and_restoration_after_both_lanes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            args = arguments(root)
            args.qa_env.write_text("SCIENTIFIC_MODELS_API_KEY=fs2_pat_56130b22ae09_test\n")
            campaign = MatrixCampaign(args, None)
            campaign.kjson = AsyncMock(return_value={"data": {"token": "dGVzdA=="}})
            events, active, peak = [], 0, 0
            rest_started, mpi_finished = asyncio.Event(), asyncio.Event()
            key = {"id": QA_KEY_ID, "tenant_id": "system", "principal_id": "qa", "expires_at": None,
                   "max_concurrency": 2, "models": ["gromacs", "gromacs-mpi"], "scopes": ["submit"]}

            class Client:
                def __init__(self, **kwargs):
                    self.event_hooks = {"request": [], "response": []}

                async def __aenter__(self):
                    return self

                async def __aexit__(self, *_args):
                    return None

                async def post(self, *_args, **_kwargs):
                    return response({})

                async def get(self, path, **_kwargs):
                    return response({"data": {"items": [dict(key)]}} if "keys" in path else {"data": []})

                async def patch(self, path, *, json):
                    events.append(("policy", json["max_concurrency"]))
                    if json["max_concurrency"] == 2:
                        self_test.assertEqual(active, 0)
                        self_test.assertIn(("drain", 4), events)
                    key.update(json)
                    return response({})

                async def delete(self, *_args, **_kwargs):
                    return response({})

            self_test = self
            for child in [campaign.rest, *(child for _, child in campaign.mpi)]:
                async def prepare(http, cohort, index, child=child):
                    name = f"cohort-{cohort}/{child.args.cases[index - 1]}"
                    child.jobs[name] = {"receipt": {"state": "prepared"}, "out": root / name}
                child.prepare = prepare

            async def rest_cohort(http, cohort):
                nonlocal active, peak
                active += 2
                peak = max(peak, active)
                events.append(("rest", "start"))
                rest_started.set()
                await mpi_finished.wait()
                active -= 2
                events.append(("rest", "finish"))
                return True

            async def mpi_cohort(http, cohort):
                nonlocal active, peak
                await rest_started.wait()
                active += 1
                peak = max(peak, active)
                events.append(("mpi", cohort))
                await asyncio.sleep(0)
                active -= 1
                if cohort == 3:
                    mpi_finished.set()
                return True

            async def drain(http, names):
                self.assertEqual(active, 0)
                events.append(("drain", len(names)))

            campaign.rest.cohort = rest_cohort
            for _, child in campaign.mpi:
                child.cohort = mpi_cohort
            campaign.drain = drain
            campaign.health_loop = AsyncMock()
            campaign.cluster_loop = AsyncMock()
            with patch("verify_concurrency.httpx2.AsyncClient", new=Client):
                await campaign.execute()
            self.assertEqual(peak, 3)
            self.assertEqual([event for event in events if event[0] == "policy"], [("policy", 3), ("policy", 2)])
            self.assertEqual([event for event in events if event[0] == "mpi"], [("mpi", 2), ("mpi", 3)])
            self.assertTrue(load(root / "matrix-progress.json")["all_verified"])
            self.assertEqual(load(root / "qa-policy-restored.json")["max_concurrency"], 2)

    async def test_mpi_error_stops_only_later_mpi_cases_and_awaits_rest_lane(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            campaign = MatrixCampaign(arguments(root), None)
            events = []

            async def rest(http, cohort):
                await asyncio.sleep(0)
                events.append("rest_finished")
                return True

            campaign.rest.cohort = rest
            campaign.mpi[0][1].cohort = AsyncMock(side_effect=ValueError("bounded failure"))
            campaign.mpi[1][1].cohort = AsyncMock()
            self.assertFalse(await campaign.cohort(None, 1))
            self.assertEqual(events, ["rest_finished"])
            campaign.mpi[1][1].cohort.assert_not_called()
            result = load(root / "matrix-progress.json")
            self.assertEqual(result["mpi"][0]["error_type"], "ValueError")


if __name__ == "__main__":
    unittest.main()
