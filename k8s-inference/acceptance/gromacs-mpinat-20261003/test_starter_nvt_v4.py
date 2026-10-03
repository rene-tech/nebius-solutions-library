import copy
import io
import json
import os
from pathlib import Path
import tarfile
import tempfile
import unittest

import starter_nvt_v4 as fix


def parameters(jobs=1):
    return {"schema": "fs2-serve.nebius.ai/gromacs-workflow-request/v1", "jobs": [
        {"id": f"replica-{i}", "steps": [
            {"id": "nvt", "command": "mdrun", "args": ["-s", "nvt.tpr", "-notunepme"]},
            {"id": "energies-nvt", "command": "energy", "args": ["-f", "nvt.edr"],
             "stdin": fix.OLD_STDIN},
            {"id": "energies-npt", "command": "energy", "stdin": fix.OLD_STDIN}]}
        for i in range(jobs)]}


def xvg(labels=fix.TERMS, values=None):
    labels = ["Total Energy" if name == "Total-Energy" else name for name in labels]
    header = "\n".join(f'@ s{i} legend "{name}"' for i, name in enumerate(labels))
    if values is None:
        values = [[0, -100, -20, 300, 10], [1, -99, -19, 301, 11], [2, -98, -18, 302, 12]]
    return header + "\n" + "\n".join(" ".join(map(str, row)) for row in values) + "\n"


class SelectionTests(unittest.TestCase):
    def test_cpu_replay_preserves_stdin_and_uid_without_gpu_or_network(self):
        command = fix.energy_command(Path("/native"), Path("/output"), "sha256:example", "test")
        self.assertIn("-i", command)
        self.assertEqual(command[command.index("--user") + 1], f"{os.getuid()}:{os.getgid()}")
        self.assertEqual(command[command.index("--network") + 1], "none")
        self.assertIn("NVIDIA_VISIBLE_DEVICES=void", command)
        self.assertNotIn("--gpus", command)
        self.assertIn("type=bind,src=/native,dst=/input,readonly", command)
        self.assertNotIn("mdrun", command)

    def test_only_nvt_selector_changes_and_input_not_mutated(self):
        original = parameters(2)
        before = copy.deepcopy(original)
        changed, edits = fix.corrected_parameters(original, 2)
        self.assertEqual(original, before)
        self.assertEqual(len(edits), 2)
        for job in changed["jobs"]:
            self.assertEqual(job["steps"][1]["stdin"], fix.NEW_STDIN)
            job["steps"][1]["stdin"] = fix.OLD_STDIN
        self.assertEqual(changed, original)

    def test_unexpected_or_already_corrected_selection_is_rejected(self):
        for value in (fix.NEW_STDIN, "Temp\nDensity\n0\n", fix.OLD_STDIN + "0\n"):
            value_request = parameters()
            value_request["jobs"][0]["steps"][1]["stdin"] = value
            with self.assertRaisesRegex(ValueError, "unexpected_nvt_selection"):
                fix.corrected_parameters(value_request, 1)

    def test_ambiguous_step_and_job_counts_are_rejected(self):
        value = parameters()
        value["jobs"][0]["steps"].append(copy.deepcopy(value["jobs"][0]["steps"][1]))
        with self.assertRaisesRegex(ValueError, "not_unique"):
            fix.corrected_parameters(value, 1)
        with self.assertRaisesRegex(ValueError, "job_count"):
            fix.corrected_parameters(parameters(), 2)

    def check(self, text=None, log="", terms=fix.TERMS, code=0):
        return fix.validate_energy(xvg() if text is None else text, log, terms, code, frames=3, final_ps=2)

    def test_exact_names_accept_only_explicit_hyphen_space_alias(self):
        result = self.check()
        self.assertEqual(result["status"], "passed")
        self.assertEqual(result["frames"], 3)
        self.assertIsNone(result["density"]["value"])
        self.assertFalse(result["density"]["zero_imputed"])

    def test_exit_zero_missing_density_is_failure(self):
        result = self.check(log="String 'Density' does not match anything", terms=(*fix.TERMS, "Density"))
        self.assertEqual(result["native_exit_code"], 0)
        self.assertEqual(result["status"], "failed")
        self.assertIn("native_selection_diagnostic", result["errors"])
        self.assertIn("observable_column_count_mismatch", result["errors"])

    def test_missing_series_rejected_even_without_diagnostic(self):
        result = self.check(terms=(*fix.TERMS, "Density"))
        self.assertIn("missing_or_extra_legend", result["errors"])

    def test_wrong_or_ambiguous_observable_not_positionally_accepted(self):
        for labels in (("Potential", "Total-Energy", "Temperature", "Pres-XX"),
                       ("Potential", "Total-Energy", "Temperature", "Temperature")):
            self.assertEqual(self.check(xvg(labels))["status"], "failed")
        self.assertIn("empty_or_ambiguous_requested_terms", self.check(
            terms=("Temperature", "temperature", "Pressure", "Potential"))["errors"])

    def test_duplicate_or_noncontiguous_legend_indices_rejected(self):
        for value in (xvg() + '@ s0 legend "Potential"\n', xvg().replace("@ s3", "@ s4")):
            self.assertEqual(self.check(value)["status"], "failed")

    def test_nonfinite_and_ragged_rows_are_rejected(self):
        for row in ([1, -99, -19, "nan", 11], [1, -99, -19, "inf", 11], [1, -99, -19]):
            result = self.check(xvg(values=[[0, -100, -20, 300, 10], row, [2, -98, -18, 302, 12]]))
            self.assertEqual(result["status"], "failed")

    def test_exit_nonzero_empty_and_duplicate_times_rejected(self):
        self.assertEqual(self.check(code=1)["status"], "failed")
        self.assertEqual(self.check("")["status"], "failed")
        self.assertIn("nonincreasing_energy_time", self.check(xvg().replace("1 -99", "0 -99"))["errors"])

    def test_wrong_extent_cadence_and_ambiguous_native_diagnostic_rejected(self):
        self.assertEqual(self.check(xvg().replace("1 -99", "0.5 -99"))["status"], "failed")
        self.assertEqual(self.check(xvg().replace("2 -98", "3 -98"))["status"], "failed")
        self.assertEqual(self.check(log="ambiguous selection: Pres")["status"], "failed")


class DeltaTests(unittest.TestCase):
    @staticmethod
    def archive(coupling="no"):
        result = io.BytesIO()
        data = f"dt = 0.002\nnsteps = 10000\npcoupl = {coupling}\n".encode()
        with tarfile.open(fileobj=result, mode="w:gz") as archive:
            item = tarfile.TarInfo("nvt.mdp")
            item.size = len(data)
            archive.addfile(item, io.BytesIO(data))
        return result.getvalue()

    def base(self, path):
        path.mkdir()
        objects = {}
        for case, count in fix.CASES.items():
            prefix = "molecular-dynamics/" + case
            params = parameters(count)
            objects[prefix + "/gromacs/parameters.json"] = fix.encoded(params)
            objects[prefix + "/recipes.json"] = fix.encoded({"recipes": [
                {"model_id": "gromacs", "arguments": {"parameters": params}},
                {"model_id": "amber", "arguments": {"untouched": True}}]})
            objects[prefix + "/gromacs/input.tar.gz"] = self.archive()
        for i in range(14):
            objects[f"molecular-dynamics/unchanged-{i}/engine/input.tar.gz"] = self.archive()
        entries = []
        for name, data in objects.items():
            fix.save_new(path / name, data)
            entries.append({"path": name, "sha256": fix.sha(data), "size_bytes": len(data)})
        manifest = {"schema": "fs2-serve.nebius.ai/customer-starter-pack/v1", "version": "v3",
                    "release_status": "qualified", "objects": entries}
        raw = fix.encoded(manifest)
        fix.save_new(path / "manifest.json", raw)
        return fix.sha(raw), objects

    def test_reproducible_delta_preserves_all_archives_and_non_gromacs_recipes(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            digest, before = self.base(root / "v3")
            one = fix.build_delta(root / "v3", root / "delta-1", expected_sha=digest)
            two = fix.build_delta(root / "v3", root / "delta-2", expected_sha=digest)
            self.assertEqual(one, two)
            self.assertEqual(one["version"], "v4")
            self.assertEqual(one["release_status"], "draft")
            self.assertFalse(one["customer_ready"])
            self.assertEqual(len(one["replacements"]), 6)
            self.assertEqual(len(one["preserved_md_input_archives"]), 17)
            for name, data in before.items():
                self.assertEqual((root / "v3" / name).read_bytes(), data)
            for row in one["replacements"]:
                if row["path"].endswith("recipes.json"):
                    value = json.loads((root / "delta-1/replacements" / row["path"]).read_bytes())
                    self.assertEqual(value["recipes"][1], {"model_id": "amber", "arguments": {"untouched": True}})

    def test_unknown_pin_changed_source_and_existing_output_fail_closed(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            digest, _ = self.base(root / "v3")
            with self.assertRaisesRegex(ValueError, "unreviewed"):
                fix.build_delta(root / "v3", root / "out")
            with self.assertRaisesRegex(ValueError, "fresh"):
                fix.build_delta(root / "v3", root / "v3/new", expected_sha=digest)
            target = root / "v3/molecular-dynamics/alanine-1ns/gromacs/input.tar.gz"
            target.write_bytes(b"changed physics")
            with self.assertRaisesRegex(ValueError, "source_object_changed"):
                fix.build_delta(root / "v3", root / "out", expected_sha=digest)

    def test_fixed_volume_guard_and_unsafe_names(self):
        with self.assertRaisesRegex(ValueError, "fixed_volume"):
            fix.archive_inventory(self.archive("C-rescale"))
        for name in ("../escape", "/absolute", "a/../b", "a\\b", "a//b"):
            with self.assertRaisesRegex(ValueError, "unsafe"):
                fix.safe_relative(name)


if __name__ == "__main__":
    unittest.main()
