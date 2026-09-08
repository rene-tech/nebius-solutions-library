"""The partial exporter must not turn stopped or failed clients into passes."""

import json
import tempfile
import unittest
from pathlib import Path

from export_interrupted_trial import export


class InterruptedExportTests(unittest.TestCase):
    def test_preserves_failure_and_missing_terminal_without_writing_raw(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            raw = root / "raw"
            raw.mkdir()

            def write(name, value):
                (raw / name).write_text(json.dumps(value))

            scenarios = [
                {"id": name, "model_id": "example", "phase": "mixed-batch"}
                for name in ("passed", "failed", "blocked")
            ]
            write("campaign-plan.json", {"run_id": "test", "source_commit": "test", "scenarios": scenarios})
            started = "2026-09-07T00:00:00Z"
            for name, status in (("passed", 200), ("failed", 409), ("blocked", 200)):
                event = {"at": started, "method": "GET", "path": "/v1/operations/test", "status": status, "elapsed_seconds": 0.1}
                write(f"{name}.http.jsonl", event)
                if name == "blocked":
                    continue
                write(f"{name}.json", {"outcome": name})
                write(f"{name}.customer-summary.json", {
                    "id": name, "phase": "mixed-batch", "outcome": name,
                    "client_started_at": started, "wall_seconds": 1,
                    "resources_released": name == "passed",
                    "error_code": "http_submit_409" if name == "failed" else None,
                })
            before = {path.name: path.read_bytes() for path in raw.iterdir()}
            result = export(raw, root / "out", {
                "stopped_at": "2026-09-07T00:00:10Z",
                "blocked_cases": {"blocked": {"client_started_at": started}},
            })
            self.assertEqual(result["outcome_counts"], {"passed": 1, "failed": 1, "blocked_client_stopped": 1})
            self.assertFalse(result["runner_completed"])
            self.assertFalse(result["all_task_resources_released"])
            self.assertEqual(result["http_status_counts"]["409"], 1)
            self.assertFalse((root / "out/aggregate.json").exists())
            self.assertFalse((root / "out/blocked.json").exists())
            self.assertEqual(before, {path.name: path.read_bytes() for path in raw.iterdir()})

    def test_completed_runner_requires_original_exporter(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "aggregate.json").write_text("{}")
            with self.assertRaisesRegex(ValueError, "original exporter"):
                export(root, root / "out", {})
            self.assertFalse((root / "out").exists())


if __name__ == "__main__":
    unittest.main()
