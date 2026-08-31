from __future__ import annotations

import argparse
import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock


SCRIPT = Path(__file__).resolve().parents[1] / "capture_cpu_fallback_warm.py"
SPEC = importlib.util.spec_from_file_location("fs2_cpu_fallback_warm", SCRIPT)
assert SPEC and SPEC.loader
FALLBACK = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(FALLBACK)


def identity() -> dict:
    image = "registry/fallback@sha256:" + "6" * 64
    return {
        "pod": {
            "name": "msa-search-pdb70-abc",
            "uid": "pod-uid-1",
            "node_name": "node-1",
        },
        "deployment_annotations": {},
        "container_image_ids": [{"name": "msa-search-pdb70", "image_id": image}],
        "pod_image_ids": [image],
        "runtime_argv_digest": "a" * 64,
        "runtime_environment_digest": "b" * 64,
        "node": {
            "metadata": {"name": "node-1", "uid": "node-uid-1", "labels": {}},
            "status": {},
        },
    }


class CpuFallbackWarmTests(unittest.TestCase):
    @staticmethod
    def _args(directory: Path) -> argparse.Namespace:
        return argparse.Namespace(
            endpoint="http://127.0.0.1:18000",
            kubeconfig=directory / "kubeconfig",
            context="fs2-disposable-test",
            namespace="fs2-models",
            deployment="msa-search-pdb70",
            service="msa-search-pdb70",
            optimization_matrix=FALLBACK.MATRIX_PATH,
            request_file=FALLBACK.FIXTURE_PATH,
            ready_timeout_seconds=60,
            request_timeout_seconds=300,
        )

    def test_two_distinct_direct_calls_keep_fallback_non_alias(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            args = self._args(Path(temporary))
            posts = iter(
                (
                    (
                        b'{"result":1}',
                        1.0,
                        "2026-08-28T12:00:00Z",
                        "2026-08-28T12:00:01Z",
                    ),
                    (
                        b'{"result":2}',
                        1.0,
                        "2026-08-28T12:00:02Z",
                        "2026-08-28T12:00:03Z",
                    ),
                )
            )
            with (
                mock.patch.object(
                    FALLBACK.ACCEPTANCE, "Kubectl", return_value=object()
                ),
                mock.patch.object(
                    FALLBACK.ACCEPTANCE,
                    "clock_domain",
                    return_value="12345678-1234-1234-1234-123456789abc",
                ),
                mock.patch.object(
                    FALLBACK, "_identity", side_effect=(identity(), identity())
                ),
                mock.patch.object(FALLBACK.VALIDATOR, "_read_fixture", return_value={}),
                mock.patch.object(
                    FALLBACK.VALIDATOR,
                    "_wait_ready",
                    return_value="2026-08-28T11:59:59Z",
                ),
                mock.patch.object(
                    FALLBACK.VALIDATOR,
                    "_request_for_case",
                    side_effect=lambda _template, query: {"query_sha256": query[:1]},
                ),
                mock.patch.object(
                    FALLBACK.VALIDATOR, "_post", side_effect=lambda *_: next(posts)
                ),
                mock.patch.object(
                    FALLBACK.VALIDATOR,
                    "_validate_response",
                    side_effect=lambda _value, query: {
                        "query_sha256": query[:1],
                        "records": 128,
                    },
                ),
            ):
                receipt = FALLBACK.run(args)

        self.assertEqual(receipt["result"], "PASS")
        self.assertEqual(
            receipt["identity_relationship"], "capability-equivalent-non-alias"
        )
        self.assertIs(receipt["exact_pdb70_parity"], False)
        self.assertEqual(len(receipt["semantic_calls"]), 2)
        self.assertNotEqual(
            receipt["semantic_calls"][0]["result_sha256"],
            receipt["semantic_calls"][1]["result_sha256"],
        )
        serialized = json.dumps(receipt, sort_keys=True)
        self.assertNotIn('result": 1', serialized)
        self.assertNotIn('result": 2', serialized)

    def test_endpoint_and_private_writer_fail_closed(self) -> None:
        for endpoint in (
            "https://127.0.0.1:18000",
            "http://msa-search-pdb70.fs2-models:8000",
            "http://127.0.0.1",
        ):
            with (
                self.subTest(endpoint=endpoint),
                self.assertRaises(FALLBACK.FallbackWarmError),
            ):
                FALLBACK._origin(endpoint)
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "receipt.json"
            FALLBACK.write_new(path.resolve(), {"result": "PASS"})
            self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
            with self.assertRaises(FALLBACK.FallbackWarmError):
                FALLBACK.write_new(path.resolve(), {"result": "PASS"})


if __name__ == "__main__":
    unittest.main()
