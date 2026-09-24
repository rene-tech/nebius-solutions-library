"""CPU-only regression gates; run under the exact client's analysis interpreter."""
import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import types
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("dense_native", Path(__file__).with_name("namd_lammps.py"))
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


class Timeline(unittest.TestCase):
    def schedule(self, initial=False):
        steps = list(range(600000 if initial else 600010, 610001, 10))
        return steps, [s * .002 for s in steps]

    def test_positive_samples(self):
        steps, times = self.schedule()
        self.assertEqual(m.frame_schedule(steps, times, 600000, initial=False)["positive_frames"], 1000)

    def test_real_initial_frame(self):
        steps, times = self.schedule(True)
        self.assertEqual(m.frame_schedule(steps, times, 600000, initial=True)["native_frames"], 1001)

    def test_missing_frame_rejected(self):
        steps, times = self.schedule()
        with self.assertRaisesRegex(ValueError, "count/step"):
            m.frame_schedule(steps[:-1], times[:-1], 600000, initial=False)

    def test_duplicate_native_step_rejected(self):
        steps, times = self.schedule()
        steps[5] = steps[4]
        with self.assertRaisesRegex(ValueError, "count/step"):
            m.frame_schedule(steps, times, 600000, initial=False)

    def test_wrong_native_origin_rejected(self):
        steps, times = self.schedule()
        with self.assertRaisesRegex(ValueError, "count/step"):
            m.frame_schedule(steps, times, 605000, initial=False)

    def test_time_scale_rejected(self):
        steps, times = self.schedule()
        with self.assertRaisesRegex(ValueError, "timestamps"):
            m.frame_schedule(steps, [t * 2 for t in times], 600000, initial=False)

    def test_nonfinite_time_rejected(self):
        steps, times = self.schedule()
        times[42] = float("nan")
        with self.assertRaisesRegex(ValueError, "timestamps"):
            m.frame_schedule(steps, times, 600000, initial=False)

    def test_native_pme_wrong_grid_rejected(self):
        pattern = r"grid = 64 64 64$"
        m.require_patterns("  grid = 64 64 64\n", [pattern])
        with self.assertRaisesRegex(ValueError, "native setting"):
            m.require_patterns("  grid = 44 44 44\n", [pattern])


class Inventory(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.fixture, self.workspace = self.root / "fixture", self.root / "workspace"
        self.fixture.mkdir()
        (self.workspace / "data").mkdir(parents=True)
        self.contract = types.SimpleNamespace(normalize=lambda r: r, canonical=lambda r: json.dumps(r, sort_keys=True).encode())
        self.package = types.SimpleNamespace(ENGINE_ID="synthetic-engine", RESULT_SCHEMA="synthetic-result")
        context = patch.dict("sys.modules", {"fs2_namd": self.package, "fs2_namd.contracts": self.contract})
        context.start()
        self.addCleanup(context.stop)
        m.save(self.fixture / "request.json", {"jobs": [{"id": "dense-motion"}]})
        (self.fixture / "input.tar.gz").write_bytes(b"synthetic")
        (self.workspace / "data/input.in").write_text("same physics")
        entry = {"path": "input.in", "size_bytes": 12, "sha256": m.sha(self.workspace / "data/input.in")}
        self.result = {"status": "succeeded", "schema": self.package.RESULT_SCHEMA, "engine_id": self.package.ENGINE_ID,
                       "job_id": "dense-motion", "completed_steps": ["dense"], "files": [entry],
                       "commands": [{"step_id": "dense", "exit_code": 0}]}
        self.result["recipe_sha256"] = hashlib.sha256(self.contract.canonical({"request": m.read(self.fixture / "request.json"), "job": "dense-motion", "image": self.package.ENGINE_ID})).hexdigest()
        m.save(self.fixture / "fixture.json", {"input_sha256": m.sha(self.fixture / "input.tar.gz"), "request_sha256": m.sha(self.fixture / "request.json"),
            "input_files": [{"path": "input.in", "bytes": 12, "sha256": entry["sha256"]}]})

    def audit(self):
        m.save(self.workspace / "result.json", self.result)
        return m.inventory_audit("namd", self.fixture, self.workspace)

    def test_package_identity_and_exact_recipe(self):
        # ENGINE_ID is exported by the package, not its contracts submodule.
        self.assertEqual(self.audit()[1]["verified_files"], 1)

    def test_changed_recipe_rejected(self):
        self.result["recipe_sha256"] = "0" * 64
        with self.assertRaisesRegex(ValueError, "native recipe"):
            self.audit()

    def test_hidden_retry_rejected(self):
        self.result["commands"].append(dict(self.result["commands"][0]))
        with self.assertRaisesRegex(ValueError, "hidden retry"):
            self.audit()

    def test_changed_native_input_rejected_even_with_rehashed_result(self):
        (self.workspace / "data/input.in").write_text("new physics!")
        self.result["files"][0]["sha256"] = m.sha(self.workspace / "data/input.in")
        with self.assertRaisesRegex(ValueError, "immutable native input"):
            self.audit()

    def test_duplicate_inventory_rejected(self):
        self.result["files"].append(dict(self.result["files"][0]))
        with self.assertRaisesRegex(ValueError, "inventory mismatch"):
            self.audit()


if __name__ == "__main__":
    unittest.main()
