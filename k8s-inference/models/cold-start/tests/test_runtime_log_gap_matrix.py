from __future__ import annotations

import argparse
import importlib.util
import os
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


BASELINE = load_module("fs2_baseline_for_gap_test", ROOT / "full_catalog_baseline.py")
MARKERS = load_module(
    "fs2_markers_for_gap_test", ROOT / "capture_runtime_log_markers.py"
)
GAPS = load_module("fs2_gap_matrix", ROOT / "runtime_log_gap_matrix.py")
SOURCE_COMMIT = "5559d0cd6f3be8faf9c93bfdaae9f2531a05c5d1"
SOURCE_TREE = "d6c877510badaf20bf92cc0a6bdc5c71ac066fdf"


def private_json(path: Path, value: object) -> None:
    BASELINE.write_json_new(path.resolve(), value)


class RuntimeLogGapMatrixTests(unittest.TestCase):
    @staticmethod
    def _plan(directory: Path) -> tuple[dict, Path]:
        acceptance = directory / "all-model-acceptance.json"
        provenance = directory / "terraform-provenance.json"
        private_json(
            acceptance,
            {"source_commit": SOURCE_COMMIT, "result": "PASS"},
        )
        private_json(
            provenance,
            {
                "schema": "fs2-serve.nebius.ai/full-catalog-terraform-provenance/v1",
                "source_commit": SOURCE_COMMIT,
                "source_tree": SOURCE_TREE,
                "run_id": "r-test",
                "region": "eu-west2",
                "profile": "full_catalog",
                "replica_owner": "keda",
                "plan_hashes": {"workloads": "a" * 64},
                "feature_flags": {
                    "challengers_enabled": False,
                    "cold_start_mechanism": "conventional",
                    "hot_model_ids": [],
                },
            },
        )
        plan = BASELINE.build_plan(
            argparse.Namespace(
                campaign_id="gap-test",
                source_commit=SOURCE_COMMIT,
                source_tree=SOURCE_TREE,
                acceptance_receipt=acceptance,
                terraform_provenance=provenance,
                nim_cache_identity=[],
            )
        )
        plan_path = directory / "plan.json"
        private_json(plan_path, plan)
        return plan, plan_path

    def test_exact_matrix_keeps_missing_logs_and_blocked_identity_distinct(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            _, plan_path = self._plan(directory)
            packet = GAPS.build_gap_matrix(
                argparse.Namespace(plan=plan_path, discovery_receipt=[])
            )
        self.assertEqual(packet["summary"]["route_count"], 15)
        self.assertEqual(packet["summary"]["blocked_identity_route_count"], 3)
        self.assertEqual(packet["summary"]["incomplete_route_count"], 12)
        self.assertEqual(packet["summary"]["complete_route_count"], 0)
        self.assertGreater(packet["summary"]["instrumentation_gap_count"], 12)
        self.assertEqual(packet["status"], "INCOMPLETE")

    def test_private_discovery_receipt_is_plan_and_container_bound(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            plan, plan_path = self._plan(directory)
            cell = next(
                item
                for item in plan["cells"]
                if item["model_id"] == "qwen3-8b"
                and item["capacity_state"] == "fresh-node-zero-pod"
            )
            digest = "sha256:2286e8533ca8b6bc777594bae30524f1426ba46ca21797524e06df6a94b06635"
            image_id = "registry/runtime@" + digest
            acceptance = directory / "qwen-acceptance.json"
            private_json(
                acceptance,
                {
                    "result": "PASS",
                    "model_id": "qwen3-8b",
                    "runtime_identity_observation": {
                        "pod": {"uid": "pod-uid-1"},
                        "container_image_ids": [{"name": "vllm", "image_id": image_id}],
                    },
                    "phase_timestamps": {
                        "activation_accepted_at": "2026-08-28T12:00:00Z",
                        "semantic_call1_accepted_at": "2026-08-28T12:10:00Z",
                    },
                },
            )
            raw_log = directory / "qwen.log"
            raw_log.write_text("2026-08-28T12:01:00Z server ready\n", encoding="utf-8")
            os.chmod(raw_log, 0o600)
            receipt = MARKERS.build_receipt(
                argparse.Namespace(
                    attempt_id=cell["expected_attempt_ids"][0],
                    model_id="qwen3-8b",
                    pod_uid="pod-uid-1",
                    container="vllm",
                    image_id=image_id,
                    attempt_t0="2026-08-28T12:00:00Z",
                    attempt_t1="2026-08-28T12:10:00Z",
                    acceptance_receipt=acceptance,
                    raw_log=raw_log,
                )
            )
            discovery = directory / "qwen-discovery.json"
            private_json(discovery, receipt)
            packet = GAPS.build_gap_matrix(
                argparse.Namespace(
                    plan=plan_path,
                    discovery_receipt=[discovery],
                )
            )

        qwen = next(row for row in packet["rows"] if row["model_id"] == "qwen3-8b")
        self.assertEqual(packet["summary"]["discovery_receipt_count"], 1)
        self.assertEqual(qwen["captured_containers"], ["vllm"])
        self.assertIn("localize-model", qwen["expected_containers"])
        self.assertEqual(qwen["status"], "INCOMPLETE")
        self.assertEqual(
            {item["state"] for item in qwen["gaps"]},
            {"UNOBSERVED_INSTRUMENTATION_GAP"},
        )


if __name__ == "__main__":
    unittest.main()
