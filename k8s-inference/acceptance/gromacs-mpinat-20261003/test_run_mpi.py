from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from run_mpi import MPICampaign, shape_parameters
from verify_concurrency import save, load


class Shapes(unittest.TestCase):
    def test_matched_protocols_keep_input_and_explicit_dispatch(self):
        for protocol in ("fixed-cpu-pme", "fixed-gpu-pme"):
            for nodes, gpus in ((1, 1), (1, 4), (2, 8)):
                value = shape_parameters({"id": "benchmem"}, nodes, gpus, protocol=protocol)
                self.assertEqual(value["jobs"][0]["steps"][0]["args"],
                                 ["-s", "original.tpr", "-o", "benchmark.tpr", "-nsteps", "10000"])
                for step in value["jobs"][0]["steps"]:
                    if step["command"] != "mdrun":
                        continue
                    args = step["args"]
                    self.assertIn("-notunepme", args)
                    self.assertEqual(args[args.index("-bonded") + 1], "cpu")
                    self.assertEqual(args[args.index("-pme") + 1], "gpu" if protocol.endswith("gpu-pme") else "cpu")
                    self.assertEqual(args[args.index("-npme") + 1],
                                     "1" if protocol.endswith("gpu-pme") and nodes * gpus > 1 else "0")

    def test_supported_scaling_matrix_retains_physics(self):
        for nodes, gpus in ((1, 1), (1, 2), (1, 4), (1, 8), (2, 8)):
            p = shape_parameters({"id": "benchmem"}, nodes, gpus)
            self.assertEqual((p["nodes"], p["gpus_per_node"]), (nodes, gpus))
            md = [s for s in p["jobs"][0]["steps"] if s["command"] == "mdrun"]
            self.assertEqual(len(md), 3)
            for s in md:
                self.assertEqual(s["args"][s["args"].index("-update") + 1], "cpu")
                self.assertNotIn("-resethway", s["args"])

    def test_invalid_resources_and_smoke_only_lengths_rejected(self):
        for nodes, gpus in ((0, 1), (2, 16), (3, 8), (1, 3), (True, 1)):
            with self.assertRaises(ValueError):
                shape_parameters({"id": "benchmem"}, nodes, gpus)
        with self.assertRaises(ValueError):
            shape_parameters({"id": "benchmem"}, 1, 1, steps=100)


class Submission(unittest.IsolatedAsyncioTestCase):
    async def test_existing_policy_allows_only_one_explicit_peer_and_owned_case(self):
        with tempfile.TemporaryDirectory() as directory:
            run = MPICampaign(SimpleNamespace(output=Path(directory), peer_operation=["peer"]), None)
            run.jobs = {"case": {"receipt": {"operation_id": "own"}}}
            for ids in ([], ["peer"], ["own"], ["own", "peer"]):
                with patch("run_mpi.active_operations", new=AsyncMock(return_value=[{"id": value} for value in ids])):
                    await run.guard_existing_policy(None)
            with patch("run_mpi.active_operations", new=AsyncMock(return_value=[{"id": "unrelated"}])):
                with self.assertRaisesRegex(ValueError, "explicit peer"):
                    await run.guard_existing_policy(None)
            run.jobs["second"] = {"receipt": {"operation_id": "second"}}
            with patch("run_mpi.active_operations", new=AsyncMock(return_value=[{"id": "own"}, {"id": "second"}])):
                with self.assertRaisesRegex(ValueError, "explicit peer"):
                    await run.guard_existing_policy(None)

    async def test_existing_policy_never_enters_admin_or_writes_policy(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            env = root / "runtime.env"
            env.write_text("SCIENTIFIC_MODELS_API_KEY=fs2_pat_56130b22ae09_test\n")
            args = SimpleNamespace(output=root, qa_env=env, origin="https://example.invalid",
                                   requests=1, cases=["benchmem"], cohorts=1, peer_operation=[])
            run = MPICampaign(args, None)
            for name in ("prepare", "guard_existing_policy", "health_loop", "cluster_loop", "drain"):
                setattr(run, name, AsyncMock())
            run.cohort = AsyncMock(return_value=True)
            run.kjson = AsyncMock(side_effect=AssertionError("No admin secret read"))
            transport = AsyncMock()
            with patch("httpx2.AsyncClient", return_value=transport) as client:
                self.assertTrue(await run.execute_existing_policy())
            self.assertEqual(client.call_count, 1)
            self.assertEqual(transport.__aenter__.return_value.method_calls, [])
            self.assertFalse(load(root / "summary.json")["policy_written"])
            run.drain.assert_awaited_once()
            run.kjson.assert_not_awaited()

    async def test_existing_policy_cleans_up_owned_watchers_on_error(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            env = root / "runtime.env"
            env.write_text("SCIENTIFIC_MODELS_API_KEY=fs2_pat_56130b22ae09_test\n")
            run = MPICampaign(SimpleNamespace(output=root, qa_env=env, origin="https://example.invalid",
                              requests=1, cases=["benchmem"], cohorts=1, peer_operation=[]), None)
            for name in ("prepare", "guard_existing_policy", "health_loop", "cluster_loop", "drain"):
                setattr(run, name, AsyncMock())
            run.cohort = AsyncMock(side_effect=RuntimeError("retained failure"))
            with patch("httpx2.AsyncClient", return_value=AsyncMock()):
                with self.assertRaisesRegex(RuntimeError, "retained failure"):
                    await run.execute_existing_policy()
            self.assertTrue(run.done.is_set())
            run.drain.assert_awaited_once()

    async def test_rest_targets_mpi_and_reuses_the_same_operation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            save(root / "request.json", {"parameters": {"nodes": 2, "gpus_per_node": 8}})
            run = MPICampaign(SimpleNamespace(interface="rest"), None)
            job = {"out": root, "receipt": {"state": "prepared"}, "idem": "same-key"}
            run.jobs = {"run": job}
            records = []
            async def post(path, **kwargs):
                records.append((path, kwargs))
                return SimpleNamespace(status_code=202, json=lambda: {"operation": {
                    "id": "op", "model_id": "gromacs-mpi", "status": "queued",
                    "reused": len(records) > 1}})
            await run.submit(SimpleNamespace(post=post), "run")
            self.assertEqual(len(records), 2)
            self.assertEqual(records[0][0], "/v1/models/gromacs-mpi:submit")
            self.assertEqual(records[0][1], records[1][1])
            self.assertTrue(job["receipt"]["idempotency_verified"])
            await run.submit(SimpleNamespace(post=post), "run")
            self.assertEqual(len(records), 2)

    async def test_mcp_uses_typed_tool_with_same_idempotency_key(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            save(root / "request.json", {"parameters": {"nodes": 1, "gpus_per_node": 8}})
            run = MPICampaign(SimpleNamespace(interface="mcp"), None)
            run.jobs = {"run": {"out": root, "receipt": {"state": "prepared"}, "idem": "same-key"}}
            operation = {"id": "op", "model_id": "gromacs-mpi", "status": "queued"}
            run.mcp_call = AsyncMock(side_effect=[{"operation": operation},
                {"operation": {**operation, "reused": True}}])
            await run.submit(None, "run")
            for call in run.mcp_call.call_args_list:
                self.assertEqual(call.args[1], "submit_gromacs_mpi_workflow")
                self.assertEqual(call.args[2]["idempotency_key"], "same-key")
            self.assertTrue(load(root / "receipt.json")["idempotency_verified"])


if __name__ == "__main__":
    unittest.main()
