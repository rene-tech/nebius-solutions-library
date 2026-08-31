from __future__ import annotations

import argparse
import importlib.util
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "capture_nim_cache_manifest.py"
SPEC = importlib.util.spec_from_file_location("fs2_capture_nim_cache", SCRIPT)
assert SPEC and SPEC.loader
CAPTURE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CAPTURE)

CATALOG_ROOT = ROOT.parent.parent / "catalog/runtime"
sys.path.insert(0, str(CATALOG_ROOT))
from fs2_serve_catalog.artifacts import artifact_manifest_from_value  # noqa: E402


class NimCacheManifestCaptureTests(unittest.TestCase):
    @staticmethod
    def _args(root: Path) -> argparse.Namespace:
        return argparse.Namespace(
            root=root,
            model_id="openfold2",
            namespace="fs2-models",
            namespace_uid="namespace-uid-1",
            pod_uid="pod-uid-1",
            container="nim",
            pvc_name="openfold2-nim-cache",
            pvc_uid="pvc-uid-1",
            source_commit="5" * 40,
            source_tree="d" * 40,
            source_repository="nvcr.io/nim/openfold/openfold2",
            source_revision="sha256:" + "a" * 64,
            runtime_image_digest="sha256:" + "a" * 64,
            capacity_bound_bytes=1024 * 1024,
            license_id="UNVERIFIED",
            license_state="unverified",
            entitlement_state="unverified",
            owner="nim-operator-nimcache",
            retention="ephemeral-test",
            stability_delay_seconds=1,
        )

    def test_double_capture_emits_catalog_valid_exact_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "profiles").mkdir()
            (root / "profiles" / "weights.bin").write_bytes(b"weights")
            (root / "profiles" / "download.complete").touch()
            (root / "tokenizer.json").write_text('{"kind":"test"}', encoding="utf-8")
            with mock.patch.object(CAPTURE.time, "sleep", return_value=None):
                receipt = CAPTURE.build_capture(self._args(root.resolve()))

        manifest = artifact_manifest_from_value(receipt["artifact_manifest"])
        self.assertEqual(manifest.model_id, "openfold2")
        self.assertEqual(manifest.kind, "nim-cache")
        self.assertEqual(manifest.digest, receipt["artifact_manifest_digest"])
        self.assertTrue(receipt["stability"]["stable"])
        empty = next(
            item for item in manifest.files if item.path == "profiles/download.complete"
        )
        self.assertEqual(empty.bytes, 0)
        self.assertEqual(
            empty.sha256,
            "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
        )
        unsigned = dict(receipt)
        del unsigned["receipt_digest"]
        self.assertEqual(CAPTURE.digest_value(unsigned), receipt["receipt_digest"])

    def test_rejects_all_empty_cache_tree(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "download.lock").touch()
            (root / "download.complete").touch()
            with (
                mock.patch.object(CAPTURE.time, "sleep", return_value=None),
                self.assertRaisesRegex(
                    CAPTURE.CacheManifestError, "no_positive_payload"
                ),
            ):
                CAPTURE.build_capture(self._args(root.resolve()))

    def test_rejects_symlink_and_runtime_source_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / "target"
            target.write_bytes(b"content")
            os.symlink(target, root / "link")
            with (
                mock.patch.object(CAPTURE.time, "sleep", return_value=None),
                self.assertRaisesRegex(CAPTURE.CacheManifestError, "non_regular"),
            ):
                CAPTURE.build_capture(self._args(root.resolve()))

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "cache.bin").write_bytes(b"content")
            args = self._args(root.resolve())
            args.runtime_image_digest = "sha256:" + "b" * 64
            with self.assertRaisesRegex(
                CAPTURE.CacheManifestError, "runtime_source_digest_mismatch"
            ):
                CAPTURE.build_capture(args)


if __name__ == "__main__":
    unittest.main()
