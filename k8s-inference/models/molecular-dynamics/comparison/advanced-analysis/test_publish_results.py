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


if __name__ == "__main__":
    unittest.main()
