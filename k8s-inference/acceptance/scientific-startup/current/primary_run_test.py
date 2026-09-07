from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path


SOURCE = Path(__file__).with_name("primary_run.py")
SPEC = importlib.util.spec_from_file_location("fs2_primary_run", SOURCE)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


class PrimaryRunTests(unittest.TestCase):
    def test_safe_pod_record_drops_environment_and_annotations(self) -> None:
        record = MODULE.safe_pod_record(
            {
                "metadata": {
                    "name": "pod",
                    "namespace": "fs2-models",
                    "uid": "uid",
                    "creationTimestamp": "2026-09-07T00:00:00Z",
                    "annotations": {"sensitive": "value"},
                    "labels": {
                        "fs2.nebius.ai/operation-id": "operation",
                        "tenant-secret": "must-not-survive",
                    },
                },
                "spec": {
                    "nodeName": "node",
                    "containers": [
                        {
                            "name": "scientific-stage",
                            "image": "image@sha256:digest",
                            "env": [{"name": "TOKEN", "value": "secret"}],
                            "resources": {"limits": {"nvidia.com/gpu": "1"}},
                        }
                    ],
                },
                "status": {"phase": "Running"},
            }
        )
        encoded = json.dumps(record)
        self.assertNotIn("secret", encoded)
        self.assertNotIn("TOKEN", encoded)
        self.assertNotIn("annotations", record)
        self.assertEqual(
            record["labels"]["fs2.nebius.ai/operation-id"], "operation"
        )

    def test_private_write_is_exclusive(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "receipt.json"
            MODULE.private_write(path, {"status": "ok"})
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            with self.assertRaises(FileExistsError):
                MODULE.private_write(path, {"status": "replacement"})


if __name__ == "__main__":
    unittest.main()
