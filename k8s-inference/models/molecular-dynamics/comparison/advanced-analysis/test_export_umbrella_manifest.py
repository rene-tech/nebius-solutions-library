"""Offline path/content binding tests; no actual trajectories or cloud writes."""
import importlib.util
import json
from pathlib import Path
import tarfile
import tempfile
import unittest

SPEC = importlib.util.spec_from_file_location("exporter", Path(__file__).with_name("export_umbrella_manifest.py"))
module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(module)


def fixture(root):
    native, public = root / "native", root / "public"
    native.mkdir()
    public.mkdir()
    overlay = native / "frames.csv"
    overlay.write_bytes(b"native overlay")
    value = {"temperature_k": 300, "windows": [],
             "unbiased": {"frames_csv": str(overlay), "frames_csv_sha256": module.digest(overlay)}}
    for i in range(24):
        name = f"window-{i:02d}"
        directory = native / "batch" / name
        directory.mkdir(parents=True)
        row = {"id": name, "center_degrees": -180 + i * 15}
        for field in ("tpr", "pullx", "native_result"):
            file = directory / field
            file.write_text(name + field)
            row[field] = str(file.relative_to(native))
            row[field + "_sha256"] = module.digest(file)
        with tarfile.open(public / (name + ".tar.gz"), "w:gz") as tar:
            tar.add(directory, arcname=name)
        value["windows"].append(row)
    source = native / "windows.json"
    source.write_text(json.dumps(value))
    return source, public, value


class ExportTests(unittest.TestCase):
    def test_portable_paths_and_unchanged_bytes(self):
        with tempfile.TemporaryDirectory() as temp:
            source, destination, original = fixture(Path(temp))
            result = module.export(source, destination)
            value = json.loads((destination / "windows.json").read_text())
            self.assertEqual(result["native_archive_references_verified"], 72)
            self.assertEqual((destination / "windows-original.json").read_bytes(), source.read_bytes())
            self.assertEqual((destination / value["unbiased"]["frames_csv"]).read_bytes(), b"native overlay")
            for before, after in zip(original["windows"], value["windows"]):
                self.assertEqual(before["center_degrees"], after["center_degrees"])
                for field in ("tpr", "pullx", "native_result"):
                    self.assertEqual(after[field], after["id"] + "/" + field)
                    self.assertEqual(after[field + "_sha256"], before[field + "_sha256"])
            with self.assertRaises(ValueError):
                module.export(source, destination)

    def test_changed_native_reference_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            source, destination, value = fixture(Path(temp))
            (source.parent / value["windows"][0]["tpr"]).write_text("changed")
            with self.assertRaises(ValueError):
                module.export(source, destination)
            self.assertFalse((destination / "windows.json").exists())

    def test_cross_window_reference_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            source, destination, value = fixture(Path(temp))
            value["windows"][0]["tpr"] = value["windows"][1]["tpr"]
            source.write_text(json.dumps(value))
            with self.assertRaises(ValueError):
                module.export(source, destination)


if __name__ == "__main__":
    unittest.main()
