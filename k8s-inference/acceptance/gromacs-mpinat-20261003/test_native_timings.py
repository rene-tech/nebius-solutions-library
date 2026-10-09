import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from native_timings import verify_native_timings


class NativeTimingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "native.json"
        self.request = {"parameters": {"jobs": [{"id": "benchmark", "steps": [
            {"id": "repeat-1", "command": "mdrun"}, {"id": "analysis", "command": "energy"}]}]}}
        self.native = {"schema": "fs2-serve.nebius.ai/gromacs-workflow-result/v1",
                       "job_id": "benchmark", "operation_id": "op", "status": "succeeded",
                       "completed_steps": ["repeat-1", "analysis"], "commands": [
                           {"step_id": "repeat-1", "command": ["gmx", "mdrun"], "exit_code": 0,
                            "wall_seconds": 10, "performance_ns_per_day": 172.8, "checkpoint_step": 10000}]}

    def receipt(self):
        raw = json.dumps(self.native).encode()
        self.path.write_bytes(raw)
        return {"operation_id": "op", "verified_artifacts": [{"path": str(self.path),
            "sha256": hashlib.sha256(raw).hexdigest(), "size_bytes": len(raw),
            "semantic_type": "gromacs-workflow-result/v1"}]}

    def test_real_native_not_envelope(self):
        result = verify_native_timings(self.request, self.receipt())
        self.assertTrue(result["benchmark_complete"])
        self.assertEqual(len(result["repeats"]), 1)
        self.assertEqual(result["repeats"][0]["command_wall_seconds"], 10)

    def test_empty_table_is_incomplete(self):
        result = verify_native_timings(self.request, {"operation_id": "op"})
        self.assertFalse(result["benchmark_complete"])
        self.native["commands"] = []
        self.assertFalse(verify_native_timings(self.request, self.receipt())["benchmark_complete"])

    def test_unknown_rate_is_not_zero_or_complete(self):
        self.native["commands"][0]["performance_ns_per_day"] = None
        result = verify_native_timings(self.request, self.receipt())
        self.assertFalse(result["benchmark_complete"])
        self.assertIsNone(result["repeats"][0]["segments"][0]["performance_ns_per_day"])

    def test_changed_hash_or_wrong_operation_fails(self):
        receipt = self.receipt()
        self.path.write_bytes(b"changed")
        with self.assertRaisesRegex(ValueError, "differs"):
            verify_native_timings(self.request, receipt)
        self.native["operation_id"] = "other"
        with self.assertRaisesRegex(ValueError, "this completed job"):
            verify_native_timings(self.request, self.receipt())

    def test_incomplete_inventory_fails(self):
        self.native["inventory_complete"] = False
        with self.assertRaises(ValueError):
            verify_native_timings(self.request, self.receipt())
