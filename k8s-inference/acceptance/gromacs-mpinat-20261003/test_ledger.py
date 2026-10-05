import tempfile
import hashlib
import json
from pathlib import Path
import unittest
from ledger import Ledger, native_counter_seconds, native_shard, protocol, union_steps


class LedgerTests(unittest.TestCase):
    def test_retry_intervals_are_not_double_counted(self):
        self.assertEqual(union_steps([(0, 100), (80, 150), (0, 100), (200, 250)]), 200)
        self.assertEqual(union_steps([]), 0)

    def test_unknown_is_not_a_zero(self):
        self.assertIsNone(protocol("no native log")['particle_count'])
        self.assertIsNone(protocol("   dt = 0.002\n")['initial_step'])

    def test_protocol_is_not_guessed(self):
        value = protocol("   dt = 0.002\n   init-step = 0\n   fep-lambdas = 0 0.5 1\nThere are: 81743 Atoms\n")
        self.assertEqual(value['particle_count'], 81743)
        self.assertEqual(value['timestep_ps'], .002)
        self.assertEqual(value['native_input_parameters']['fep-lambdas'], '0 0.5 1')
        self.assertIsNone(value['force_field_name'])

    def test_null_requires_explicit_unknown_quality(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = Ledger(Path(directory) / 'evidence.sqlite')
            with self.assertRaises(ValueError):
                ledger.measure('op', 'attempt', 'job', 'replica', 'steps', None, 'steps', 'measured', 'none')
            ledger.db.close()

    def test_native_counter_is_one_reported_wall_not_process_or_core_time(self):
        text = "               Core t (s)   Wall t (s)        (%)\n       Time:      167.384       20.935      799.5\n"
        self.assertEqual(native_counter_seconds(text), 20.935)
        self.assertIsNone(native_counter_seconds(text + text))
        self.assertIsNone(native_counter_seconds("Performance: 80.0\nTime: 1 2 3\n"))

    def test_trajectory_postprocessing_is_analysis_not_input_preparation(self):
        ledger = Ledger(":memory:")
        ledger.db.execute("INSERT INTO operations VALUES (?,?,?,?,?,?,?,?)",
                          ("op", "campaign", "case", "REST", "succeeded", "input", "tpr", "{}"))
        commands = [{"step_id": command, "segment": 1, "finished_at": "2026-10-05T16:00:00+00:00",
                     "command": ["gmx", command], "wall_seconds": 2.5, "exit_code": 0}
                    for command in ("trjcat", "check", "convert-tpr")]
        ledger.commands("op", "job", "attempt", commands, set(), lambda _: None, "source")
        phases = dict(ledger.db.execute("SELECT command_id,phase FROM commands"))
        self.assertEqual(phases, {"trjcat": "analysis", "check": "analysis", "convert-tpr": "input_preparation"})
        self.assertEqual(ledger.db.execute("SELECT sum(wall_seconds) FROM commands WHERE phase='analysis'").fetchone()[0], 5)
        ledger.db.close()

    def test_null_mapping_requires_true_gang_and_literal_gang_stays_literal(self):
        jobs = [{"id": "gang"}]
        self.assertEqual(native_shard(None, "gang-jobset", jobs), "gang")
        self.assertEqual(native_shard("gang", "independent-jobs", jobs), "gang")
        for shard, mode, source in ((None, None, jobs), (None, "independent-jobs", jobs),
                                    ("gang", "gang-jobset", jobs), (None, "gang-jobset", jobs * 2)):
            with self.subTest(shard=shard, mode=mode), self.assertRaises(ValueError):
                native_shard(shard, mode, source)

    def checkpoint_fixture(self, root, public_shard=None, mode="gang-jobset"):
        cohort, recovered = root / "campaign/cohort-1", root / "bucket"
        case, bucket = cohort / "mem", recovered / "mem"
        case.mkdir(parents=True)
        def save(path, value):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(value))
        save(case / "receipt.json", {"operation_id": "op", "steps": 100, "input_sha256": "input"})
        save(case / "request.json", {"parameters": {"jobs": [{"id": "gang", "steps": [
            {"id": "repeat-1", "command": "mdrun", "args": ["-deffnm", "repeat1"]}]}]}})
        save(case / "status.json", {"operation": {"status": "succeeded", "model_id": "gromacs-mpi"},
             "batch": {"stages": [{"stage_id": "workflow", "attempts": [{"attempt_id": "attempt",
                        "shard_id": public_shard, "attempt_number": 1, "outcome": "succeeded"}]}]}})
        save(case / "frozen-plan.json", {"operation_id": "op", "tenant_id": "system", "model_id": "gromacs-mpi",
             "source": "postgresql-admitted-plan", "plan": {"stages": [{"stage_id": "workflow", "mode": mode}]}})
        text = "   dt = 0.002\n   init-step = 0\nThere are: 1000 Atoms\nCore t (s)   Wall t (s)        (%)\nTime: 80.000 10.000 800.0\n"
        digest = hashlib.sha256(text.encode()).hexdigest()
        for path in (bucket / "native-versions" / digest, bucket / "native/repeat1.part0001.log"):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
        command = {"step_id": "repeat-1", "segment": 1, "finished_at": "2026-10-03T10:00:20+00:00",
                   "command": ["mpirun", "gmx_mpi", "mdrun", "-deffnm", "repeat1"],
                   "wall_seconds": 12, "exit_code": 0, "checkpoint_step": 100,
                   "performance_ns_per_day": 100, "log": "command.log"}
        manifest = {"state": {"operation_id": "op", "job_id": "gang", "generation": 1, "commands": [command]},
                    "files": [{"path": "command.log", "sha256": digest, "size_bytes": len(text)}]}
        save(bucket / "gang/attempt-001/checkpoint-00000001.json", manifest)
        return cohort, recovered

    def test_true_gang_checkpoint_and_per_attempt_measurements_are_indexed(self):
        for public_shard, mode in ((None, "gang-jobset"), ("gang", "independent-jobs")):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                cohort, recovered = self.checkpoint_fixture(root, public_shard, mode)
                ledger = Ledger(root / "ledger.sqlite")
                with ledger.db:
                    ledger.cohort(cohort, recovered, "REST")
                stored = ledger.db.execute("SELECT shard_id,raw_json FROM attempts").fetchone()
                self.assertEqual(stored[0], "gang")
                self.assertEqual(json.loads(stored[1])["shard_id"], public_shard)
                command = json.loads(ledger.db.execute("SELECT raw_json FROM commands").fetchone()[0])
                self.assertEqual(command["native_mdrun_counter_wall_seconds"], 10)
                interval = ledger.db.execute("SELECT value_json FROM measurements WHERE attempt_id='attempt' AND name='durable_interval'").fetchone()
                self.assertEqual(json.loads(interval[0]), [0, 100])
                ledger.db.close()

    def test_frozen_plan_identity_mismatch_never_normalizes_null(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cohort, recovered = self.checkpoint_fixture(root)
            path = cohort / "mem/frozen-plan.json"
            value = json.loads(path.read_text())
            value["operation_id"] = "different-operation"
            path.write_text(json.dumps(value))
            ledger = Ledger(root / "ledger.sqlite")
            with self.assertRaisesRegex(ValueError, "Frozen plan identity"):
                ledger.cohort(cohort, recovered, "REST")
            ledger.db.close()

    def test_final_result_can_fill_commands_without_checkpoint_recovery(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cohort, recovered = self.checkpoint_fixture(root, "gang", "independent-jobs")
            case = cohort / "mem"
            manifest = json.loads((recovered / "mem/gang/attempt-001/checkpoint-00000001.json").read_text())
            result = {"operation_id": "op", "job_id": "gang", "commands": manifest["state"]["commands"], "files": manifest["files"]}
            path = case / "result.artifact"
            path.write_text(json.dumps(result))
            log = case / "log.artifact"
            log.write_bytes((recovered / "mem/native-versions" / manifest["files"][0]["sha256"]).read_bytes())
            receipt = json.loads((case / "receipt.json").read_text())
            receipt["verified_artifacts"] = [{"path": str(p), "sha256": hashlib.sha256(p.read_bytes()).hexdigest(),
                "size_bytes": p.stat().st_size, "semantic_type": semantic} for p, semantic in (
                    (path, "gromacs-workflow-result/v1"), (log, "gromacs-file/v1"))]
            (case / "receipt.json").write_text(json.dumps(receipt))
            later = cohort / "zz-next-case"
            later.mkdir()
            (later / "receipt.json").write_text(json.dumps({"operation_id": "op2", "steps": 100}))
            for name in ("request.json", "status.json"):
                (later / name).write_bytes((case / name).read_bytes())
            ledger = Ledger(root / "ledger.sqlite")
            ledger.cohort(cohort, root / "no-recovery", "REST")
            command = ledger.db.execute("SELECT attempt_id,raw_json FROM commands").fetchone()
            self.assertEqual(command[0], "attempt")
            self.assertEqual(json.loads(command[1])["native_mdrun_counter_wall_seconds"], 10)
            self.assertEqual(ledger.db.execute("SELECT DISTINCT campaign FROM operations").fetchall(), [("campaign",)])
            ledger.db.close()

    def test_init_process_and_collector_tail_are_not_active_compute_or_exact_export(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = Ledger(Path(directory) / "ledger.sqlite")
            ledger.db.execute("INSERT INTO operations VALUES (?,?,?,?,?,?,?,?)", ("op", "c", "case", "REST", "succeeded", None, None, "{}"))
            pod = {"metadata": {"uid": "pod", "labels": {"fs2.nebius.ai/operation-id": "op", "fs2.nebius.ai/attempt-id": "a"}},
                   "spec": {"nodeName": "node", "initContainers": [{"name": "materialize-0"}], "containers": [
                       {"name": "scientific-stage", "resources": {"requests": {"nvidia.com/gpu": "2"}}}]},
                   "status": {"conditions": [{"type": "PodScheduled", "status": "True", "lastTransitionTime": "2026-10-03T10:00:00Z"}],
                       "initContainerStatuses": [{"name": "materialize-0", "state": {"terminated": {
                           "startedAt": "2026-10-03T10:00:01Z", "finishedAt": "2026-10-03T10:00:04Z", "exitCode": 0}}}],
                       "containerStatuses": [{"name": "scientific-stage", "state": {"terminated": {
                           "startedAt": "2026-10-03T10:00:05Z", "finishedAt": "2026-10-03T10:00:20Z"}}},
                           {"name": "artifact-collector", "state": {"running": {"startedAt": "2026-10-03T10:00:05Z"}}}]}}
            for source, at, value in (("pods.jsonl", "2026-10-03T10:00:25Z", {"pod": pod}),
                                      ("pod-disappearance.jsonl", "2026-10-03T10:00:30Z", {"pod_uid": "pod"})):
                ledger.db.execute("INSERT INTO observations VALUES (?,?,?,?,?,?)", (source, 1, at, "a", "pod", json.dumps(value)))
            ledger.correlate_allocations()
            rows = {name: json.loads(value) for name, value in ledger.db.execute("SELECT name,value_json FROM measurements")}
            self.assertEqual(rows["init_container_process_seconds"]["total_seconds"], 3)
            self.assertEqual(rows["post_stage_collector_tail_bounds"]["lower"], 5)
            self.assertEqual(rows["post_stage_collector_tail_bounds"]["upper"], 10)
            self.assertNotIn("export_seconds", rows)
            ledger.db.close()


if __name__ == '__main__':
    unittest.main()
