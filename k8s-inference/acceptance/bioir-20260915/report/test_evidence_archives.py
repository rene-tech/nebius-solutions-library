"""Offline tests for byte-preserving evidence publication."""
import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location("archives", Path(__file__).with_name("evidence_archives.py"))
archives = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(archives)


class EvidenceArchivesTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.data = self.root / "lane/raw/structure.pdb"
        self.data.parent.mkdir(parents=True)
        self.data.write_bytes(b"ATOM  deliberately padded    \n")
        for name, value in (("ROOT", self.root), ("ARCHIVES", self.root / "archives"),
                            ("GROUPS", {"lane": ("lane/raw",)})):
            patcher = patch.object(archives, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_roundtrip_and_deterministic_archive(self):
        first = archives.pack()
        self.assertEqual(first, archives.pack())
        self.assertEqual(1, archives.verify()["verified_files"])
        original = self.data.read_bytes()
        self.data.unlink()
        archives.verify(unpack=True)
        self.assertEqual(original, self.data.read_bytes())

    def test_changed_local_evidence_is_preserved(self):
        archives.pack()
        self.data.write_bytes(b"changed locally")
        with self.assertRaisesRegex(ValueError, "refusing to overwrite"):
            archives.verify(unpack=True)
        self.assertEqual(b"changed locally", self.data.read_bytes())

    def test_archive_tampering_is_detected(self):
        archives.pack()
        archive = archives.ARCHIVES / "lane.tar.gz"
        archive.write_bytes(archive.read_bytes() + b"tampered")
        with self.assertRaisesRegex(ValueError, "checksum mismatch"):
            archives.verify()


if __name__ == "__main__":
    unittest.main()
