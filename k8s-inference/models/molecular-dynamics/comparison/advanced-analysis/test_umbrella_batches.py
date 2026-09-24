"""Offline fixture/transport tests; synthetic data are never MD qualification."""
import copy
import importlib.util
import json
from pathlib import Path
import tarfile
import tempfile
import unittest

spec = importlib.util.spec_from_file_location("umbrella_batches", Path(__file__).with_name("umbrella_batches.py"))
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
from fs2_gromacs.worker import expand_args


def request(index):
    steps = []
    for i, stage in enumerate(m.STAGES):
        previous = "start" if i == 0 else m.STAGES[i - 1]
        args = ["-f", stage + ".mdp", "-c", previous + ".gro", "-p", "system.top", "-n", "dihedrals.ndx", "-o", stage + ".tpr"]
        if stage in ("npt", "production"):
            args += ["-t", "fs2-" + previous + ".cpt"]
        steps += [{"id": "prepare-" + stage, "command": "grompp", "args": args, "expected_outputs": [stage + ".tpr"]},
                  {"id": stage, "command": "mdrun", "args": ["-s", stage + ".tpr", "-deffnm", stage, "-notunepme"],
                   "expected_outputs": [stage + ".gro"]}]
        if stage != "minimize":
            steps.append({"id": "energies-" + stage, "command": "energy",
                          "args": ["-f", {"files": stage + "*.edr"}, "-o", stage + "-energy.xvg"],
                          "stdin": "Potential\nTemperature\n0\n", "expected_outputs": [stage + "-energy.xvg"]})
    steps.append({"id": "trajectory", "command": "trjcat", "args": ["-f", {"files": "production.part*.xtc"}, "-o", "production-canonical.xtc"],
                  "expected_outputs": ["production-canonical.xtc"]})
    return {"schema": "fs2-serve.nebius.ai/gromacs-workflow-request/v1", "threads": 8,
            "checkpoint_minutes": 1, "segment_minutes": 10, "max_wall_seconds": 3600,
            "max_output_bytes": 1048576, "output_destination": "platform-artifacts",
            "output_prefix": "runs/test-umbrella", "jobs": [{"id": f"window-{index:02d}", "steps": steps}]}


def fixture(root, index):
    folder = root / f"window-{index:02d}"
    inputs = folder / "inputs"
    inputs.mkdir(parents=True)
    for name in sorted(m.REQUIRED_FILES):
        if name == "protocol.json":
            m.save(inputs / name, {"window_id": folder.name, "seed": 202609240 + index})
        else:
            (inputs / name).write_text(f"synthetic input {index} {name}\nseed = {202609240 + index}\n")
    m.save(folder / "request.json", request(index))
    m.archive(inputs, folder / "input.tar.gz")
    m.save(folder / "preparation.json", {"window_id": folder.name, "center_degrees": -180 + 15 * index,
        "files": [m.record(p) for p in sorted(inputs.iterdir())], "request": m.record(folder / "request.json"),
        "bundle": m.record(folder / "input.tar.gz")})
    return folder


class TransformTests(unittest.TestCase):
    def test_only_input_paths_and_energy_postprocessing_change(self):
        original = request(1)["jobs"][0]
        untouched = copy.deepcopy(original)
        variant = m.transform_job(original)
        self.assertEqual(original, untouched)
        without_merges = [s for s in variant["steps"] if s["command"] != "eneconv"]
        self.assertEqual(len(without_merges), len(original["steps"]))
        for before, after in zip(original["steps"], without_merges):
            restored = copy.deepcopy(after)
            if before["command"] == "grompp":
                restored["args"] = [a.removeprefix("window-01/") if isinstance(a, str) else a for a in after["args"]]
            if before["command"] == "energy":
                restored["args"][1] = before["args"][1]
            self.assertEqual(before, restored)

    def test_stage_outputs_and_checkpoint_references_stay_in_cwd(self):
        variant = m.transform_job(request(23)["jobs"][0])
        for step in variant["steps"]:
            self.assertEqual(step.get("directory", "."), ".")
            self.assertTrue(all("/" not in p for p in step.get("expected_outputs", [])))
            if step["id"] in ("prepare-npt", "prepare-production"):
                self.assertNotIn("/", step["args"][m.value_index(step["args"], "-t")])
        self.assertEqual(variant["steps"][0]["args"][3], "window-23/start.gro")
        self.assertEqual(variant["steps"][2]["args"][3], "minimize.gro")

    def test_eneconv_precedes_every_single_input_energy(self):
        steps = m.transform_job(request(1)["jobs"][0])["steps"]
        self.assertEqual(len(steps), 15)
        for i, step in enumerate(steps):
            if step["command"] == "energy":
                stage = step["id"].removeprefix("energies-")
                self.assertEqual(steps[i - 1]["command"], "eneconv")
                self.assertEqual(steps[i - 1]["args"], ["-f", {"files": stage + ".part*.edr"}, "-o", stage + "-canonical.edr"])
                self.assertEqual(step["args"][1], stage + "-canonical.edr")

    def test_runtime_glob_excludes_previous_canonical_file(self):
        with tempfile.TemporaryDirectory() as root:
            directory = Path(root)
            for name in ("nvt.part0002.edr", "nvt.part0001.edr", "nvt-canonical.edr", "nvt.edr", "npt.part0001.edr"):
                (directory / name).write_text("synthetic")
            result = expand_args(["-f", {"files": "nvt.part*.edr"}, "-o", "nvt-canonical.edr"], directory)
            self.assertEqual(result, ["-f", "nvt.part0001.edr", "nvt.part0002.edr", "-o", "nvt-canonical.edr"])

    def test_reject_stage_reorder_or_foreign_directory(self):
        job = request(1)["jobs"][0]
        job["steps"].reverse()
        with self.assertRaisesRegex(ValueError, "stage sequence"):
            m.transform_job(job)
        job = request(1)["jobs"][0]
        job["steps"][0]["directory"] = "other"
        with self.assertRaisesRegex(ValueError, "cwd"):
            m.transform_job(job)

    def test_reject_already_rewritten_input(self):
        job = request(1)["jobs"][0]
        job["steps"][0]["args"][1] = "other.mdp"
        with self.assertRaisesRegex(ValueError, "unexpected original"):
            m.transform_job(job)

    def test_no_window_zero_or_duplicate_coverage(self):
        with self.assertRaisesRegex(ValueError, "outside"):
            m.transform_job(request(0)["jobs"][0])
        for groups in ((tuple(range(0, 8)), tuple(range(8, 16)), tuple(range(16, 23))),
                       (tuple(range(1, 9)), tuple(range(9, 17)), tuple(range(16, 23)))):
            with self.assertRaisesRegex(ValueError, "coverage"):
                m.validate_coverage(groups)
        m.validate_coverage(m.GROUPS)

    def test_missing_or_duplicate_flags_rejected(self):
        for args in (["-f"], ["-f", "one", "-f", "two"], ["-c", "one"]):
            with self.assertRaises(ValueError):
                m.value_index(args, "-f")

    def test_transformed_schema_valid_and_managed_flags_still_rejected(self):
        value = request(1)
        value["jobs"][0] = m.transform_job(value["jobs"][0])
        self.assertEqual(len(m.contracts.normalize(value)["jobs"][0]["steps"]), 15)
        value["jobs"][0]["steps"][1]["args"] += ["-multidir", "window-01"]
        with self.assertRaisesRegex(ValueError, "platform-managed"):
            m.contracts.normalize(value)


class InputTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.fixtures = self.root / "fixtures"
        self.folder = fixture(self.fixtures, 1)

    def tearDown(self):
        self.temporary.cleanup()

    def test_load_verifies_request_archive_and_all_science_files(self):
        result = m.load_window(self.fixtures, 1)
        self.assertEqual(len(result["inputs"]), 8)
        (self.folder / "inputs/nvt.mdp").write_text("changed seed")
        with self.assertRaisesRegex(ValueError, "hash/size"):
            m.load_window(self.fixtures, 1)

    def test_reject_request_tampering(self):
        (self.folder / "request.json").write_text("{}")
        with self.assertRaisesRegex(ValueError, "hash/size"):
            m.load_window(self.fixtures, 1)

    def test_reject_extra_source_input(self):
        (self.folder / "inputs/unexpected").write_text("extra")
        with self.assertRaisesRegex(ValueError, "inventory differs"):
            m.load_window(self.fixtures, 1)

    def test_archive_round_trip_is_deterministic(self):
        other = self.root / "other.tar.gz"
        m.archive(self.folder / "inputs", other)
        self.assertEqual(m.sha(other), m.sha(self.folder / "input.tar.gz"))
        expected = m.inventory(self.folder / "inputs", max_bytes=1048576)
        m.verify_archive(other, expected)

    def test_archive_rejects_symlink_and_changed_inventory(self):
        expected = m.inventory(self.folder / "inputs", max_bytes=1048576)
        expected[0]["sha256"] = "0" * 64
        with self.assertRaisesRegex(ValueError, "bundle differs"):
            m.verify_archive(self.folder / "input.tar.gz", expected)
        (self.folder / "inputs/link").symlink_to("nvt.mdp")
        with self.assertRaisesRegex(ValueError, "symlinks"):
            m.archive(self.folder / "inputs", self.root / "link.tar.gz")

    def test_source_window_identity_checked(self):
        preparation = m.read(self.folder / "preparation.json")
        preparation["window_id"] = "window-02"
        (self.folder / "preparation.json").write_text(json.dumps(preparation))
        with self.assertRaisesRegex(ValueError, "identity"):
            m.load_window(self.fixtures, 1)

    def test_full_build_coverage_parity_and_original_variants(self):
        for index in range(2, 24):
            fixture(self.fixtures, index)
        before = {str(p.relative_to(self.fixtures)): m.sha(p) for p in self.fixtures.rglob("*") if p.is_file()}
        output = self.root / "batches"
        result = m.build(self.fixtures, output)
        self.assertEqual(result["status"], "validated-not-submitted")
        self.assertEqual([len(g["windows"]) for g in result["groups"]], [8, 8, 7])
        self.assertEqual({str(p.relative_to(self.fixtures)): m.sha(p) for p in self.fixtures.rglob("*") if p.is_file()}, before)
        for group in result["groups"]:
            count = len(group["windows"])
            self.assertEqual(group["transport_estimates"]["input_inventory_rows_across_jobs"], count * count * 8)
            self.assertTrue(all(row["byte_identical"] for row in group["science_file_parity"]))
            m.contracts.normalize(m.read(output / group["request"]["path"]))
            with tarfile.open(output / group["input"]["path"]) as tar:
                self.assertEqual(len(tar.getnames()), count * 8)
                self.assertTrue(all(name.split("/")[0] in group["windows"] for name in tar.getnames()))
        again = m.build(self.fixtures, self.root / "batches-again")
        self.assertEqual([g["input"]["sha256"] for g in result["groups"]], [g["input"]["sha256"] for g in again["groups"]])
        self.assertEqual([g["request"]["sha256"] for g in result["groups"]], [g["request"]["sha256"] for g in again["groups"]])
        with self.assertRaisesRegex(ValueError, "new directory"):
            m.build(self.fixtures, output)

    def test_unequal_workflow_budgets_are_not_silently_changed(self):
        for index in range(2, 24):
            fixture(self.fixtures, index)
        path = self.fixtures / "window-23/request.json"
        value = m.read(path); value["threads"] = 4
        path.write_text(json.dumps(value))
        prep_path = path.parent / "preparation.json"
        prep = m.read(prep_path); prep["request"] = m.record(path)
        prep_path.write_text(json.dumps(prep))
        with self.assertRaisesRegex(ValueError, "budgets/settings differ"):
            m.build(self.fixtures, self.root / "output")

    def test_no_writes_under_original_fixtures(self):
        with self.assertRaisesRegex(ValueError, "inside original"):
            m.build(self.fixtures, self.fixtures / "new-batches")


if __name__ == "__main__":
    unittest.main()
