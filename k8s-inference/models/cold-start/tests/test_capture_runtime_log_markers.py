from __future__ import annotations

import argparse
import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "capture_runtime_log_markers.py"
SPEC = importlib.util.spec_from_file_location("fs2_capture_runtime_logs", SCRIPT)
assert SPEC and SPEC.loader
MARKERS = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MARKERS)


def private_file(path: Path, value: bytes) -> None:
    path.write_bytes(value)
    os.chmod(path, 0o600)


class RuntimeLogMarkerTests(unittest.TestCase):
    @staticmethod
    def _args(
        directory: Path, model_id: str, container: str, digest: str
    ) -> argparse.Namespace:
        image_id = "registry/runtime@" + digest
        acceptance = directory / "acceptance.json"
        private_file(
            acceptance,
            json.dumps(
                {
                    "result": "PASS",
                    "model_id": model_id,
                    "runtime_identity_observation": {
                        "pod": {"uid": "pod-uid-1"},
                        "container_image_ids": [
                            {"name": container, "image_id": image_id}
                        ],
                    },
                    "phase_timestamps": {
                        "activation_accepted_at": "2026-08-28T12:00:00Z",
                        "semantic_call1_accepted_at": "2026-08-28T12:10:00Z",
                    },
                }
            ).encode("utf-8"),
        )
        return argparse.Namespace(
            attempt_id=f"{model_id}--fresh-node-zero-pod--r01",
            model_id=model_id,
            pod_uid="pod-uid-1",
            container=container,
            image_id=image_id,
            attempt_t0="2026-08-28T12:00:00Z",
            attempt_t1="2026-08-28T12:10:00Z",
            acceptance_receipt=acceptance,
            raw_log=directory / "runtime.log",
        )

    def test_admits_one_exact_source_bound_marker_per_event(self) -> None:
        digest = (
            "sha256:5bee4a3103f4111a5ff4dc597d2e052b39e1d66c782941b5fb64957bb1ab601c"
        )
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            args = self._args(directory, "evo2-40b", "model", digest)
            private_file(
                args.raw_log,
                b"\n".join(
                    [
                        b'2026-08-28T12:01:00.000000001Z {"event": "fs2-startup-phase", "name": "weight-load-start"}',
                        b'2026-08-28T12:02:00Z {"event": "fs2-startup-phase", "name": "weight-load-end"}',
                        b'2026-08-28T12:03:00Z {"event": "fs2-startup-phase", "name": "engine-build-or-compile-start"}',
                        b'2026-08-28T12:04:00Z {"event": "fs2-startup-phase", "name": "engine-build-or-compile-end"}',
                    ]
                ),
            )
            receipt = MARKERS.build_receipt(args)

        self.assertEqual(receipt["status"], "PASS")
        self.assertEqual(len(receipt["admitted_events"]), 4)
        self.assertEqual(receipt["gaps"], [])

    def test_duplicate_or_unpinned_markers_remain_instrumentation_gaps(self) -> None:
        evo_digest = (
            "sha256:5bee4a3103f4111a5ff4dc597d2e052b39e1d66c782941b5fb64957bb1ab601c"
        )
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            args = self._args(directory, "evo2-40b", "model", evo_digest)
            private_file(
                args.raw_log,
                b"\n".join(
                    [
                        b'2026-08-28T12:01:00Z {"event":"fs2-startup-phase","name":"weight-load-start"}',
                        b'2026-08-28T12:01:01Z {"event":"fs2-startup-phase","name":"weight-load-start"}',
                    ]
                ),
            )
            receipt = MARKERS.build_receipt(args)
            self.assertEqual(receipt["status"], "INCOMPLETE")
            self.assertIn(
                "marker-ambiguous", {item["reason"] for item in receipt["gaps"]}
            )

        qwen_digest = (
            "sha256:2286e8533ca8b6bc777594bae30524f1426ba46ca21797524e06df6a94b06635"
        )
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            args = self._args(directory, "qwen3-8b", "vllm", qwen_digest)
            private_file(args.raw_log, b"2026-08-28T12:01:00Z server ready")
            receipt = MARKERS.build_receipt(args)
            self.assertEqual(receipt["status"], "INCOMPLETE")
            self.assertEqual(
                receipt["gaps"][0]["state"], "UNOBSERVED_INSTRUMENTATION_GAP"
            )

    def test_rejects_caller_boundary_that_differs_from_acceptance_clock(self) -> None:
        digest = (
            "sha256:5bee4a3103f4111a5ff4dc597d2e052b39e1d66c782941b5fb64957bb1ab601c"
        )
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            args = self._args(directory, "evo2-40b", "model", digest)
            args.attempt_t0 = "2026-08-28T11:59:00Z"
            private_file(
                args.raw_log,
                b'2026-08-28T12:01:00Z {"event":"fs2-startup-phase","name":"weight-load-start"}',
            )
            with self.assertRaisesRegex(
                MARKERS.LogMarkerError, "acceptance_attempt_boundary_mismatch"
            ):
                MARKERS.build_receipt(args)


if __name__ == "__main__":
    unittest.main()
