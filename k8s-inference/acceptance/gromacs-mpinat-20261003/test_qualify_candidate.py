import copy
import json
from pathlib import Path
import tempfile
import unittest

from qualify_candidate import continuation_checks, free_capacity, make_fixture, parse_args, sha, validate, validate_gro


class CapacityTests(unittest.TestCase):
    def setUp(self):
        self.node = {"metadata": {"name": "node", "labels": {}}, "spec": {},
                     "status": {"conditions": [{"type": "Ready", "status": "True"}], "allocatable": {"nvidia.com/gpu": "4"}}}

    def pod(self, count=1, phase="Running"):
        return {"metadata": {"namespace": "other", "name": "preserve-me"}, "spec": {"nodeName": "node", "containers": [{"resources": {"requests": {"nvidia.com/gpu": str(count)}}}]}, "status": {"phase": phase}}

    def test_free_capacity_does_not_require_eviction(self):
        before = copy.deepcopy(self.node)
        result = free_capacity("node", [self.node], [self.pod()], 2)
        self.assertEqual(result["free_by_requests"], 3)
        self.assertEqual(before, self.node)
        with self.assertRaises(ValueError):
            free_capacity("node", [self.node], [self.pod()], 4)

    def test_pending_gpu_reservations_count(self):
        with self.assertRaises(ValueError):
            free_capacity("node", [self.node], [self.pod(4, "Pending")], 1)

    def test_terminal_pods_release_requests(self):
        self.assertEqual(free_capacity("node", [self.node], [self.pod(4, "Succeeded")], 4)["free_by_requests"], 4)

    def test_init_container_peak_counts(self):
        pod = self.pod(1)
        pod["spec"]["initContainers"] = [{"resources": {"requests": {"nvidia.com/gpu": "3"}}}]
        self.assertEqual(free_capacity("node", [self.node], [pod], 1)["free_by_requests"], 1)

    def test_not_ready_no_probe(self):
        self.node["status"]["conditions"][0]["status"] = "False"
        with self.assertRaises(ValueError):
            free_capacity("node", [self.node], [], 1)

    def test_no_extra_taint_toleration(self):
        self.node["spec"]["taints"] = [{"key": "node.kubernetes.io/unreachable", "effect": "NoSchedule"}]
        with self.assertRaises(ValueError):
            free_capacity("node", [self.node], [], 1)


class FixtureTests(unittest.TestCase):
    def test_prepare_and_legacy_cli_keep_their_own_arguments(self):
        prepared = parse_args(["prepare", "--source", "source", "--output", "new", "--mpi-gpus", "2",
                               "--mpi-nodes", "2", "--parameters", "corrected.json"])
        self.assertEqual((prepared.mpi_gpus, prepared.mpi_nodes), (2, 2))
        self.assertEqual(prepared.parameters, Path("corrected.json"))
        finalized = parse_args(["finalize-legacy", "--input", "new", "--output", "evidence",
                               "--source-revision", "source-revision"])
        self.assertFalse(hasattr(finalized, "mpi_gpus"))

    def source(self, directory, reset=False):
        source = directory / "source"
        source.mkdir()
        (source / "input.tar.gz").write_bytes(b"immutable test fixture bytes")
        (source / "provenance.json").write_text("{}")
        request = {"schema": "fs2-serve.nebius.ai/gromacs-workflow-request/v1", "jobs": [{"id": "benchmark", "steps": [{"id": "repeat-1", "command": "mdrun", "args": ["-s", "input.tpr", "-update", "cpu"] + (["-resethway"] if reset else [])}]}]}
        (source / "parameters.json").write_text(json.dumps(request))
        return source

    def test_fixture_rejects_obsolete_forced_reset(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with self.assertRaises(ValueError):
                make_fixture(self.source(root, True), root / "new")
            self.assertFalse((root / "new").exists())

    def test_mpi_fixture_has_typed_gang_and_preserves_bundle(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = self.source(root)
            make_fixture(source, root / "new", mpi_gpus=4)
            request = json.loads((root / "new/request.json").read_text())
            self.assertEqual(request["jobs"][0]["id"], "gang")
            self.assertEqual(request["gpus_per_node"], 4)
            self.assertEqual(sha(source / "input.tar.gz"), sha(root / "new/input.tar.gz"))
            self.assertEqual(request["jobs"][0]["steps"][0]["args"][-4:], ["-pme", "cpu", "-npme", "0"])

    def test_corrected_saved_wrapper_overrides_old_parameters_explicitly(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = self.source(root, True)
            request = json.loads((source / "parameters.json").read_text())
            request["jobs"][0]["steps"][0]["args"].remove("-resethway")
            parameters = root / "saved-request.json"
            parameters.write_text(json.dumps({"parameters": request}))
            make_fixture(source, root / "new", parameters=parameters)
            fixture = json.loads((root / "new/fixture.json").read_text())
            self.assertEqual(fixture["source_parameters_sha256"], sha(parameters))
            self.assertIn("-resethway", (source / "parameters.json").read_text())

    def test_missing_native_outputs_return_failure_not_exception(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            result = {"status": "failed", "files": [], "commands": [], "completed_steps": [], "inventory_complete": True}
            (root / "result.json").write_text(json.dumps(result))
            validation = validate(root, {"jobs": [{"steps": [{"id": "md"}]}]}, 1, False, False, 1)
            self.assertEqual(validation["status"], "failed")
            self.assertIn("repeat 1 energy file absent", validation["errors"])

    def test_inventory_failure_before_first_checkpoint_has_two_zero_generations(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "data").mkdir()
            log = root / "data/native.log"
            log.write_text("retained native diagnostic")
            result = {"status": "failed", "files": [{"path": "native.log", "size_bytes": log.stat().st_size, "sha256": sha(log)}],
                      "inventory_complete": False, "inventory_scope": "bounded-failure-logs-only",
                      "error": "workspace file/byte budget", "native_checkpoint_generation": 0,
                      "committed_checkpoint_generation": 0, "checkpoint_commit_scope": "local-only"}
            (root / "result.json").write_text(json.dumps(result))
            self.assertEqual(validate(root, {}, 1, False, True, 1)["status"], "passed")
            result["committed_checkpoint_generation"] = 1
            (root / "result.json").write_text(json.dumps(result))
            self.assertEqual(validate(root, {}, 1, False, True, 1)["status"], "failed")

    def test_native_gro_count_coordinates_and_cell(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "final.gro"
            path.write_text("fixture\n1\n" + "    1ALA      N    1" + f"{.1:8.3f}{.2:8.3f}{.3:8.3f}" + "\n1 1 1\n")
            self.assertTrue(validate_gro(path, 1)["finite"])
            with self.assertRaises(ValueError):
                validate_gro(path, 2)
            path.write_text(path.read_text().replace("0.200", "  nan"))
            with self.assertRaises(ValueError):
                validate_gro(path, 1)


class DeviceProbeTests(unittest.TestCase):
    def fixture(self):
        rows = [{"kind": "rank", "rank": i, "gpu_uuid_hex": str(i + 1) * 32,
                 "MPIX_Query_cuda_support": 1} for i in range(2)]
        rows += [{"kind": "summary", "status": "passed", "ranks": 2,
                  "message_bytes": [4, 4096, 1048576], "point_to_point_repeats": 3,
                  "checks": ["Sendrecv", "Isend/Irecv/Waitall", "Allreduce"]}]
        gpu = "uuid, name, driver\nGPU-" + "1" * 32 + ", L40S, 580\nGPU-" + "2" * 32 + ", L40S, 580\n"
        return rows, gpu

    def check(self, rows, gpu, code=0):
        from qualify_candidate import validate_device_probe
        return validate_device_probe("\n".join(json.dumps(row, separators=(",", ":")) for row in rows), gpu, 2, code)

    def test_actual_positive_device_probe(self):
        self.assertEqual(self.check(*self.fixture())["status"], "passed")

    def test_nonzero_exit_keeps_native_failure(self):
        self.assertEqual(self.check(*self.fixture(), code=2)["status"], "failed")

    def test_query_zero_is_not_cuda_awareness(self):
        rows, gpu = self.fixture()
        rows[0]["MPIX_Query_cuda_support"] = 0
        self.assertEqual(self.check(rows, gpu)["status"], "failed")

    def test_duplicate_gpu_does_not_qualify_two_gpu(self):
        rows, gpu = self.fixture()
        rows[1]["gpu_uuid_hex"] = rows[0]["gpu_uuid_hex"]
        self.assertEqual(self.check(rows, gpu)["status"], "failed")

    def test_query_only_without_device_transfers_does_not_pass(self):
        rows, gpu = self.fixture()
        self.assertEqual(self.check(rows[:2], gpu)["status"], "failed")

    def test_probe_only_is_explicit_not_default(self):
        args = ["run", "--node", "node", "--name", "probe", "--image", "repo@sha256:hash",
                "--source-revision", "revision", "--input", "/input", "--output", "/output", "--gpus", "2"]
        self.assertFalse(parse_args(args).device_probe_only)
        self.assertTrue(parse_args(args + ["--device-probe-only"]).device_probe_only)


class ContinuationTests(unittest.TestCase):
    def fixture(self, root):
        (root / "data").mkdir()
        commands = []
        for repeat in (1, 2, 3):
            (root / "data" / f"fs2-repeat-{repeat}_prev.cpt").write_bytes(b"native checkpoint")
            for segment, step in enumerate((4000, 10000)):
                log = f"segment-{repeat}-{segment}.log"
                (root / "data" / log).write_text("continuing from step 4000, 8.000 ps\n" if segment else "starting mdrun\n")
                commands.append({"step_id": f"repeat-{repeat}", "command": ["gmx_mpi", "mdrun"] +
                                 (["-cpi", f"fs2-repeat-{repeat}.cpt"] if segment else []),
                                 "checkpoint_step": step, "exit_code": 0, "log": log})
        return commands

    def test_native_reported_checkpoint_chain(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            result = continuation_checks(root, self.fixture(root))
            self.assertEqual(result["status"], "passed")
            self.assertEqual(result["chains"][0]["segments"][-1]["start_step"], 4000)

    def test_restart_from_zero_or_missing_link_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            commands = self.fixture(root)
            commands[1]["command"] = ["gmx_mpi", "mdrun"]
            self.assertEqual(continuation_checks(root, commands)["status"], "failed")
            commands[1]["command"] += ["-cpi", "fs2-repeat-1.cpt"]
            (root / "data" / commands[1]["log"]).write_text("continuing from step 0\n")
            self.assertEqual(continuation_checks(root, commands)["status"], "failed")


if __name__ == "__main__":
    unittest.main()
