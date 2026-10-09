"""Offline archive tests only; no cloud credentials or requests."""
import importlib.util
from pathlib import Path
import tarfile
import tempfile
import unittest

SPEC = importlib.util.spec_from_file_location("publisher", Path(__file__).with_name("publish_results.py"))
pub = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(pub)


class ArchiveTests(unittest.TestCase):
    def test_preserves_bytes_and_inventory(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "science"
            source.mkdir()
            (source / "trace").write_bytes(b"actual bytes\x00\x01")
            target = root / "delivery.tgz"
            result = pub.archive(source, target)
            self.assertEqual(result["files"], 1)
            self.assertEqual(result["sha256"], pub.digest(target))
            with tarfile.open(target) as tar:
                self.assertEqual(tar.extractfile("science/trace").read(), (source / "trace").read_bytes())
            with self.assertRaises(ValueError):
                pub.archive(source, target)
            with self.assertRaises(ValueError):
                pub.archive(source, source / "recursive.tgz")

    def test_links_not_archived(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "science"
            source.mkdir()
            (source / "link").symlink_to("missing")
            with self.assertRaises(ValueError):
                pub.archive(source, root / "delivery.tgz")

    def test_explicit_plan_excludes_unselected_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "plot.png").write_bytes(b"plot")
            (root / "private.json").write_bytes(b"not selected")
            result = pub.publication_plan(root, ["plot.png"])
            self.assertEqual(result["files"], [{"path": "plot.png", "bytes": 4,
                              "sha256": pub.digest(root / "plot.png")}])
            self.assertEqual(result["bucket"], "renes-bucket")

    def test_plan_rejects_duplicates_links_and_escape(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "file").write_bytes(b"x")
            (root / "link").symlink_to(root / "file")
            for names in ([], ["file", "file"], ["../file"], [str(root / "file")], ["link"]):
                with self.assertRaises(ValueError):
                    pub.publication_plan(root, names)


if __name__ == "__main__":
    unittest.main()
