import json
from pathlib import Path
import tempfile
import unittest

from candidate_report import CASES, gpu_observations, legacy_projection
from qualify_candidate import sha


class ReportTests(unittest.TestCase):
    def legacy(self, root):
        (root / "workspace").mkdir()
        image = "private.invalid/gromacs@sha256:" + "a" * 64
        values = {
            "workspace/result.json": {"status": "succeeded", "finished_at": "2026-10-03T00:00:00Z"},
            "qualification.json": {"image": image, "gpu_count": 2, "worker_exit_code": 0, "pool": "h100-ondemand-1x"},
            "identity-cleanup.json": {"image": image, "source_revision": "observed", "input_sha256": "b" * 64,
                                      "request_sha256": "c" * 64, "cleanup": "absence observed",
                                      "pods": [{"images": [{"name": "runtime", "image_id": image}]} for _ in range(2)]},
        }
        for name, value in values.items():
            (root / name).write_text(json.dumps(value))
        (root / "final-state-validation.json").write_text(json.dumps({
            "status": "passed", "native_result_sha256": sha(root / "workspace/result.json")}))

    def test_projection_is_additive_and_unknown_occupancy_stays_null(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.legacy(root)
            original = sha(root / "qualification.json")
            receipt, capacity = legacy_projection(root)
            self.assertTrue(receipt["derived_receipt"])
            self.assertEqual(len(receipt["all_observed_image_ids"]), 2)
            self.assertIsNone(capacity["free_by_requests"])
            self.assertIn("NOT a preflight", capacity["capture_scope"])
            self.assertEqual(original, sha(root / "qualification.json"))
            self.assertFalse((root / "receipt.json").exists())

    def test_wrong_observed_image_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.legacy(root)
            path = root / "identity-cleanup.json"
            identity = json.loads(path.read_text())
            identity["pods"][1]["images"][0]["image_id"] = "unqualified:latest"
            path.write_text(json.dumps(identity))
            with self.assertRaises(ValueError):
                legacy_projection(root)

    def test_changed_native_result_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.legacy(root)
            (root / "workspace/result.json").write_text('{"status":"failed"}')
            with self.assertRaises(ValueError):
                legacy_projection(root)

    def test_gpu_observations_do_not_infer_unobserved_devices(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.assertEqual(gpu_observations(root), [])
            (root / "gpu.txt").write_text("uuid, name, driver_version, compute_cap, memory.total [MiB]\nGPU-known, NVIDIA L40S, 580.173.02, 8.9, 46068 MiB\n")
            self.assertEqual(gpu_observations(root), [{"uuid": "GPU-known", "name": "NVIDIA L40S",
                                                      "driver_version": "580.173.02", "compute_cap": "8.9"}])

    def test_declared_scope_does_not_include_unrun_shapes(self):
        self.assertEqual(len(CASES), 6)
        self.assertNotIn("mpi8-h100", CASES)


if __name__ == "__main__":
    unittest.main()
