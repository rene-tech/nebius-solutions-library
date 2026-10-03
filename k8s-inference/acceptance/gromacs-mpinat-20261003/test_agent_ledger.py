import copy
import csv
import io
import json
from pathlib import Path
import shlex
import tempfile
import unittest
from unittest.mock import patch

from agent_ledger import (Evidence, WorkspaceReader, admitted_studies, agent_metadata,
                          bounded, collect_case, duration, save, unique_operations)
from cost_report import REFERENCES, build_report
from ledger import Ledger
from verify_agent_case import canonical, normalize, runtime_canonical
from verify_agent_study import sha

OP = "c966e97d-6d68-4a72-8281-d8d2cba1a808"
STUDY = "063c8ece-afe1-544c-b086-526ad76d4667"
ATTEMPT = "ec5ab029-bdeb-550c-aa72-fe099e5bf23e"


class AgentLedgerTests(unittest.TestCase):
    def fixture(self, root):
        values = {}
        output = "/workspace/replays/cohort/model/mpinat-benchsfc"
        native_root = output + "/steps/simulate/operation"
        source = "/workspace/inputs/mpinat/benchsfc/input.tar.gz"
        parameter_path = source.replace("input.tar.gz", "parameters-candidate.json")

        def put(path, value):
            data = value if isinstance(value, bytes) else canonical(value)
            values[path] = data
            return {"path": path, "size_bytes": len(data), "sha256": sha(data)}

        parameters = {"schema": "fs2-serve.nebius.ai/gromacs-workflow-request/v1",
                      "jobs": [{"id": "benchmark", "steps": [
                          {"id": "repeat-" + str(i), "command": "mdrun", "args": [
                              "-s", "original.tpr", "-deffnm", "repeat" + str(i)]}
                          for i in range(1, 4)]}], "output_prefix": "runs/fs2-mpinat-case"}
        parameter = put(parameter_path, parameters)
        provenance = {"id": "benchsfc", "bundle_sha256": "a" * 64, "bundle_bytes": 100,
                      "tpr_sha256": sha(b"original"), "tpr_bytes": 8}
        put(source.replace("input.tar.gz", "provenance.json"), provenance)
        plan = {"steps": [{"id": "simulate", "kind": "batch", "model": "gromacs",
                "tool": "submit_gromacs_workflow", "operation": "run-workflow", "source": source,
                "parameters": parameter_path, "compression": "gzip", "idempotency_key": "key"}]}
        record = {"id": STUDY, "state": "completed", "owner": "qa", "output_directory": output,
                  "created_at": 100.0, "finished_at": 120.0,
                  "steps": {"simulate": {"operation_id": OP}}, "inputs": {
                      source: {"sha256": provenance["bundle_sha256"], "size_bytes": 100},
                      parameter_path: parameter}}
        record["identity"] = sha(canonical({"plan": plan, "output": output, "owner": "qa"}))
        frozen = "/workspace/.scientific-studies/qa/" + STUDY + "/receipt.json"
        put(frozen, record)
        put(str(Path(frozen).with_name("plan.json")), plan)
        original = {"path": "original.tpr", "sha256": provenance["tpr_sha256"], "size_bytes": 8}
        # This deliberately unretained large output must never be read or copied.
        trajectory = {"path": "trajectory.xtc", "sha256": "d" * 64, "size_bytes": 10**10}
        commands, files, artifacts, mapped = [], [original, trajectory], [], []
        for i in range(1, 4):
            log = ("    dt = 0.002\n    init-step = 0\nThere are: 100 Atoms\n"
                   "Core t (s) Wall t (s) (%)\nTime: 10.0 2.0 500.0\n").encode()
            name = f"repeat{i}.part0001.log"
            item = put(native_root + f"/output-{i:02}.artifact", log)
            artifacts.append({**item, "semantic_type": "gromacs-file/v1"})
            files.append({**item, "path": name})
            mapped.append({**item, "native_path": name})
            commands.append({"step_id": f"repeat-{i}", "segment": 1, "command": ["gmx", "mdrun", "-s", "original.tpr", "-nsteps", "10000"],
                             "exit_code": 0, "performance_ns_per_day": 100.0 + i, "wall_seconds": 3.0,
                             "finished_at": f"2026-10-03T15:00:{i * 4:02}+00:00", "checkpoint_step": 10000, "log": name})
        recipe = sha(runtime_canonical({"request": normalize(parameters), "job": "benchmark", "image": "image-digest"}))
        native = {"schema": "fs2-serve.nebius.ai/gromacs-workflow-result/v1", "operation_id": OP,
                  "status": "succeeded", "job_id": "benchmark", "commands": commands, "files": files,
                  "completed_steps": [c["step_id"] for c in commands], "engine_id": "image-digest", "recipe_sha256": recipe}
        native_ref = {**put(native_root + "/output-00.artifact", native), "semantic_type": "gromacs-workflow-result/v1"}
        artifacts = [native_ref, *artifacts, {**trajectory, "path": native_root + "/output-99.artifact", "semantic_type": "gromacs-file/v1"}]
        put(native_root + "/native-files.json", {"results": [{"source_result_sha256": native_ref["sha256"], "files": mapped}]})
        receipt = {"operation_id": OP, "state": "verified", "identity": {"model_id": "gromacs",
                   "source_sha256": provenance["bundle_sha256"], "parameters_sha256": sha(canonical(parameters)), "idempotency_key": "key"},
                   "request_descriptor": {"compression": "gzip"}, "verified_artifacts": artifacts}
        put(native_root + "/receipt.json", receipt)
        put(native_root + "/request.json", {"parameters": parameters, "idempotency_key": "key"})
        put(native_root + "/status.json", {"operation": {"id": OP, "model_id": "gromacs", "status": "succeeded"},
            "batch": {"stages": [{"stage_id": "workflow", "attempts": [{"attempt_id": ATTEMPT,
            "shard_id": "benchmark", "attempt_number": 1, "outcome": "succeeded"}]}]}})
        rows = [{**c, "job_id": "benchmark", "requested_steps": 10000, "source_result_sha256": native_ref["sha256"],
                 "log_file": artifacts[i + 1]["path"], "log_sha256": artifacts[i + 1]["sha256"]} for i, c in enumerate(commands)]
        report = {"schema": "scientific-ai/native-md-timing-report/v1", "operation_id": OP,
                  "complete": True, "gaps": [], "reported_repeats": 3, "receipt_file": native_root + "/receipt.json",
                  "sources": [native_ref], "timing_rows": rows}
        report_root = output + "/steps/timing"
        put(report_root + "/native-timing-report.json", report)
        table = io.StringIO()
        writer = csv.DictWriter(table, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows([{**r, "command": shlex.join(r["command"])} for r in rows])
        put(report_root + "/native-timings.csv", table.getvalue().encode())
        put(report_root + "/native-timing-report.md", b"repeat-1 101.0 repeat-2 102.0 repeat-3 103.0")
        downloaded = [p for p in values if p.startswith(native_root) and Path(p).name not in {"request.json", "status.json"}
                      or p.startswith(report_root)]
        verification = {"operation_id": OP, "study_id": STUDY, "native_report_verified": True,
                        "durable_study_delivery_verified": True, "scientific_convergence_claimed": False,
                        "verified_repeats": 3, "verified_native_segments": 3, "source_result_hashes": [native_ref["sha256"]],
                        "authenticated_downloads": [{"path": p, "sha256": sha(values[p]), "size_bytes": len(values[p])} for p in downloaded]}
        proof = {"case_id": "mpinat-benchsfc", "operation_id": OP, "study_id": STUDY, "model_step": "simulate",
                 "plan_identity": record["identity"], "client_image": "client-image", "native_replayed": False,
                 "selected_case_verified": True, "repeat_count": 3, "requested_steps_per_repeat": 10000,
                 "input_sha256": provenance["bundle_sha256"], "parameter_file_sha256": parameter["sha256"],
                 "parameters_sha256": sha(canonical(parameters)), "report_verification": verification,
                 "native_results": [{"source_sha256": native_ref["sha256"], "recipe_sha256": recipe,
                                     "original_tpr_sha256": provenance["tpr_sha256"]}]}
        summary = {"case_id": proof["case_id"], "conversation_id": "chat", "model": "actual-agent",
                   "output_directory": output, "seconds": 25.0, "tool_seconds": 7.0, "errors": ["retained incidental error"]}
        messages = [{"content": [{"type": "tool_call", "tool_call": {"name": "run_scientific_workflow",
                    "output": json.dumps({"id": STUDY, "study_admission": "accepted", "durable_study": True})}}]}]
        chat = root / "chat"
        for name, value in (("summary.json", summary), ("messages.json", messages), ("durable-terminal-state.json", record)):
            save(chat / name, value)
        proof_path = root / "proof.json"
        save(proof_path, proof)
        campaign = {"tenant": "system", "principal": "qa", "client_image": "client-image"}
        spec = {"proof": str(proof_path), "chat_directory": str(chat), "frozen_record": frozen}
        return spec, values, campaign, proof, summary, record

    def test_existing_ledger_and_cost_report_accept_projection_without_large_payloads(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            spec, values, campaign, *_ = self.fixture(root)
            read = []
            def fetch(path):
                read.append(path)
                return values[path]
            cohort = root / "snapshot/cohort-1"
            metadata = collect_case(spec, fetch, cohort / "benchsfc", campaign)
            self.assertFalse(any(p.endswith((".xtc", "input.tar.gz", "original.tpr", "output-99.artifact")) for p in read))
            self.assertEqual(metadata["chat"]["planning_seconds"]["value"], None)
            self.assertEqual(metadata["chat"]["errors"], ["retained incidental error"])
            self.assertEqual(metadata["durable_delivery"]["study_elapsed_seconds"]["value"], 20.0)
            self.assertTrue(all(Path(item["retained_path"]).is_file() for item in metadata["source_evidence"]))
            self.assertFalse((cohort / "benchsfc/artifacts/output-99.artifact").exists())
            index = Ledger(root / "evidence.sqlite")
            with index.db:
                index.cohort(cohort, root / "no-recovery", "agent-skill-MCP")
            self.assertEqual(index.db.execute("SELECT interface FROM operations").fetchone()[0], "agent-skill-MCP")
            self.assertEqual(index.db.execute("SELECT count(*) FROM commands").fetchone()[0], 3)
            index.db.close()
            report = build_report(root / "evidence.sqlite", json.loads(REFERENCES.read_text()), [cohort])
            self.assertEqual(len(report["operations"]), 1)
            self.assertEqual(len(report["commands"]), 3)
            self.assertEqual(report["operations"][0]["occupancy_gpu_seconds"]["upper"], None)

    def test_receipt_request_source_and_proof_drift_fail_closed(self):
        for mutation in ("parameters", "proof", "input", "native", "status", "image"):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                spec, values, campaign, proof, *_ = self.fixture(root)
                if mutation == "parameters":
                    path = next(p for p in values if p.endswith("/request.json"))
                    request = json.loads(values[path])
                    request["parameters"]["threads"] = 7
                    values[path] = canonical(request)
                elif mutation == "proof":
                    proof["native_results"][0]["recipe_sha256"] = "bad"
                    Path(spec["proof"]).write_bytes(canonical(proof))
                elif mutation == "input":
                    path = next(p for p in values if p.endswith("/provenance.json"))
                    values[path] = canonical({**json.loads(values[path]), "id": "wrong-case"})
                elif mutation == "native":
                    path = next(p for p in values if p.endswith("output-00.artifact"))
                    values[path] += b" "
                elif mutation == "status":
                    path = next(p for p in values if p.endswith("/status.json"))
                    value = json.loads(values[path])
                    value["operation"]["id"] = "other"
                    values[path] = canonical(value)
                else:
                    campaign["client_image"] = "different-image"
                with self.assertRaises(ValueError):
                    collect_case(spec, values.__getitem__, root / "out", campaign)

    def test_failed_proof_and_admission_only_cannot_pass(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            spec, values, campaign, *_ = self.fixture(root)
            with self.assertRaisesRegex(ValueError, "Failure evidence"):
                collect_case({**spec, "proof": "case-failure.json"}, values.__getitem__, root / "out", campaign)
            record = Path(spec["chat_directory"]) / "durable-terminal-state.json"
            record.write_bytes(canonical({"id": STUDY, "state": "observation_expired"}))
            with self.assertRaisesRegex(ValueError, "Admission-only"):
                collect_case(spec, values.__getitem__, root / "out", campaign)

    def test_authenticated_binary_reference_is_not_a_copy_instruction(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            spec, values, campaign, proof, *_ = self.fixture(root)
            proof["report_verification"]["authenticated_downloads"].append(
                {"path": "/workspace/small-trajectory.xtc", "sha256": "f" * 64, "size_bytes": 10})
            Path(spec["proof"]).write_bytes(canonical(proof))
            metadata = collect_case(spec, values.__getitem__, root / "out", campaign)
            self.assertFalse(any(item["path"].endswith(".xtc") for item in metadata["source_evidence"]))

    def test_recovery_associations_do_not_duplicate_native_operations(self):
        with tempfile.TemporaryDirectory() as directory:
            _, _, campaign, proof, summary, study = self.fixture(Path(directory))
            first = agent_metadata(proof, summary, study, campaign, {})
            recovery = copy.deepcopy(first)
            recovery["chat"]["conversation_id"] = "recovery-chat"
            recovery["chat"]["client_image"] = "later-analysis-only-client"
            grouped = unique_operations([first, recovery])
            self.assertEqual(len(grouped), 1)
            self.assertEqual(len(grouped[0]["agent_associations"]), 2)
            with self.assertRaisesRegex(ValueError, "Duplicate chat"):
                unique_operations([first, first])
            recovery["native_identity"]["input_sha256"] = "changed"
            with self.assertRaisesRegex(ValueError, "different native physics"):
                unique_operations([first, recovery])

    def test_boundaries_and_unknown_times(self):
        with self.assertRaises(ValueError):
            duration(-1, "invalid")
        self.assertEqual(duration(None, "not captured")["status"], "unknown")
        with patch("agent_ledger.MAX_BYTES", 3), self.assertRaises(ValueError):
            bounded(b"large")
        evidence = Evidence(lambda _: b"bytes")
        with self.assertRaises(ValueError):
            evidence.fetch("/workspace/file", {"size_bytes": 5, "sha256": "bad"})
        self.assertEqual(admitted_studies([{"text": "report succeeded " + STUDY}]), set())
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            workspace.mkdir()
            (root / "outside").write_bytes(b"not workspace")
            (workspace / "escape").symlink_to(root / "outside")
            reader = WorkspaceReader(workspace)
            for path in ("/workspace/../outside", "/tmp/other", "/workspace/escape"):
                with self.assertRaises(ValueError):
                    reader(path)


if __name__ == "__main__":
    unittest.main()
