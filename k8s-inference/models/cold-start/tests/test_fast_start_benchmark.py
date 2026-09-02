from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
import tempfile
import unittest
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "fs2_fast_start_benchmark_test",
    ROOT / "aggregate_fast_start_benchmark.py",
)
assert SPEC and SPEC.loader
BENCHMARK = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = BENCHMARK
SPEC.loader.exec_module(BENCHMARK)


def digest(label: str) -> str:
    return hashlib.sha256(label.encode()).hexdigest()


def utc(value: datetime) -> str:
    return (
        value.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    )


def compatibility_tuple(**overrides: Any) -> dict[str, Any]:
    value: dict[str, Any] = {
        "source_commit": "a" * 40,
        "project_id": "project-e00rene",
        "region": "eu-north1",
        "cluster_context": "k8s-inference-h100",
        "namespace": "fs2-models",
        "model_id": "qwen3-8b",
        "model_revision": "b" * 40,
        "model_content_digest": "sha256:" + digest("model-content"),
        "artifact_manifest_digest": "sha256:" + digest("artifact-manifest"),
        "runtime_image_ref": "eu.nebius.cloud/fs2/qwen@sha256:" + digest("image"),
        "runtime_image_digest": "sha256:" + digest("image"),
        "runtime_template_digest": "sha256:" + digest("template"),
        "runtime_argv_digest": digest("argv"),
        "runtime_environment_digest": digest("environment"),
        "accelerator_class": "nvidia-h100-sxm5-80gb",
        "gpu_product": "NVIDIA H100 80GB HBM3",
        "gpu_compute_capability": "9.0",
        "gpu_memory_bytes": 85899345920,
        "gpu_count": 1,
        "driver_version": "580.82.07",
        "cuda_version": "13.0",
        "pool_id": "h100-reserved-8x",
        "capacity_type": "regular-capacity-block",
        "capacity_state": "prepared-node-zero-pod",
        "cache_tier": "SharedFilesystem",
        "mechanism": "shared-artifact-cache",
        "snapshot_digest": None,
        "storage_class": "fs2-shared-cache",
        "storage_mode": "ReadWriteMany",
        "payload_digest": digest("prompt-v1"),
        "interface_protocol": "openai-chat",
        "endpoint_path": "/v1/chat/completions",
        "streaming": False,
        "semantic_validator_digest": digest("semantic-validator"),
        "benchmark_client_digest": digest("benchmark-client"),
        "client_placement": "same-region",
    }
    value.update(overrides)
    return value


def attempt(
    ordinal: int,
    *,
    model_start: float = 90.0,
    capacity_wait: float = 0.0,
    requested_level: str = "L2",
    tuple_overrides: dict[str, Any] | None = None,
    capture_first_byte: bool = True,
    status: str = "PASS",
) -> dict[str, Any]:
    tuple_value = compatibility_tuple(**(tuple_overrides or {}))
    activation = datetime(2026, 9, 2, 12, ordinal, tzinfo=UTC)
    prepared = tuple_value["capacity_state"] == "prepared-node-zero-pod"
    capacity_requested = None if prepared else activation
    capacity_available = activation + timedelta(seconds=capacity_wait)
    ready = capacity_available + timedelta(seconds=model_start)
    first_byte = ready + timedelta(seconds=0.2)
    first_semantic = ready + timedelta(seconds=0.8)
    completed = ready + timedelta(seconds=1.0)
    returned = completed + timedelta(seconds=30)
    raw: dict[str, Any] = {
        "schema": "fs2-serve.nebius.ai/fast-start-benchmark-attempt/v1",
        "attempt_id": f"qwen3-8b-{ordinal:03d}",
        "ordinal": ordinal,
        "observed_at": utc(completed),
        "status": status,
        "failure_code": None,
        "requested_level": requested_level,
        "compatibility_tuple": tuple_value,
        "compatibility_tuple_digest": BENCHMARK.canonical_digest(tuple_value),
        "timestamps": {
            "activation_accepted": utc(activation),
            "gpu_capacity_requested": (
                None if capacity_requested is None else utc(capacity_requested)
            ),
            "gpu_capacity_available": utc(capacity_available),
            "endpoint_ready": utc(ready),
            # Public activation and inference use the same durable request.
            "request_started": utc(activation),
            "first_response_byte": utc(first_byte) if capture_first_byte else None,
            "first_semantic_output": utc(first_semantic),
            "request_completed": utc(completed),
            "return_to_floor": utc(returned),
        },
        "durations_seconds": {
            "capacity_wait": capacity_wait,
            "gpu_capacity_available_to_ready": model_start,
            "activation_to_ready": capacity_wait + model_start,
            "request_to_first_byte": (
                capacity_wait + model_start + 0.2 if capture_first_byte else None
            ),
            "request_to_first_semantic_output": capacity_wait + model_start + 0.8,
            "request_completion": capacity_wait + model_start + 1.0,
            "activation_to_first_semantic_output": capacity_wait + model_start + 0.8,
        },
        "inference": {
            "modality": "text",
            "first_output_kind": "response",
            "valid_output": True,
            "http_status": 200,
            "request_count": 1,
            "warmup_count": 0,
            "concurrency": 1,
            "input_units": {"unit": "tokens", "count": 8},
            "output_units": {"unit": "tokens", "count": 10},
            "throughput": {
                "unit": "output-tokens-per-second",
                "value": round(10 / 0.8, 6),
            },
        },
        "artifacts": {
            "raw_attempt_sha256": digest(f"raw-{ordinal}"),
            "semantic_output_sha256": digest(f"output-{ordinal}"),
            "runtime_log_sha256": digest(f"log-{ordinal}"),
            "gpu_metrics_sha256": digest(f"metrics-{ordinal}"),
        },
    }
    if status == "FAIL":
        raw["failure_code"] = "endpoint_timeout"
        raw["inference"].update(
            {
                "first_output_kind": "none",
                "valid_output": False,
                "http_status": 504,
                "output_units": None,
                "throughput": None,
            }
        )
        raw["artifacts"]["semantic_output_sha256"] = None
        for key in (
            "first_response_byte",
            "first_semantic_output",
            "request_completed",
        ):
            raw["timestamps"][key] = None
        for key in (
            "request_to_first_byte",
            "request_to_first_semantic_output",
            "request_completion",
            "activation_to_first_semantic_output",
        ):
            raw["durations_seconds"][key] = None
    return raw


class FastStartBenchmarkTests(unittest.TestCase):
    def test_fewer_than_three_attempts_are_not_exploratory_evidence(self) -> None:
        receipt = BENCHMARK.build_receipt(
            [attempt(1), attempt(2)],
            generated_at="2026-09-02T13:00:00Z",
        )

        self.assertEqual("insufficient-evidence", receipt["qualification"]["state"])
        self.assertIn(
            "minimum-exploratory-attempts-not-met",
            receipt["qualification"]["reasons"],
        )

    def test_three_attempts_are_exploratory_with_observed_p95(self) -> None:
        receipt = BENCHMARK.build_receipt(
            [
                attempt(1, model_start=91.0),
                attempt(2, model_start=95.0),
                attempt(3, model_start=100.0),
            ],
            generated_at="2026-09-02T13:00:00Z",
        )

        BENCHMARK.validate_receipt(receipt)
        self.assertEqual("PASS", receipt["status"])
        self.assertEqual("exploratory", receipt["qualification"]["state"])
        self.assertEqual("L2", receipt["qualification"]["observed_level"])
        self.assertIsNone(receipt["qualification"]["qualified_level"])
        self.assertEqual(
            100.0,
            receipt["aggregates"]["metrics_seconds"]["gpu_capacity_available_to_ready"][
                "p95"
            ],
        )
        self.assertEqual(3, receipt["aggregates"]["success_count"])

    def test_twenty_comparable_successes_can_qualify(self) -> None:
        receipt = BENCHMARK.build_receipt(
            [attempt(index, model_start=80 + index / 10) for index in range(1, 21)],
            generated_at="2026-09-02T14:00:00Z",
        )

        self.assertEqual("qualified", receipt["qualification"]["state"])
        self.assertEqual("L2", receipt["qualification"]["qualified_level"])
        self.assertTrue(receipt["qualification"]["p95_eligible_for_qualification"])

    def test_target_miss_still_reports_lower_measured_class(self) -> None:
        receipt = BENCHMARK.build_receipt(
            [
                attempt(index, model_start=45.0, requested_level="L4")
                for index in range(1, 21)
            ],
            generated_at="2026-09-02T14:00:00Z",
        )

        self.assertEqual("target-missed", receipt["qualification"]["state"])
        self.assertEqual("L3", receipt["qualification"]["observed_level"])
        self.assertEqual("L3", receipt["qualification"]["qualified_level"])
        self.assertFalse(receipt["qualification"]["target_met"])

    def test_any_failed_attempt_prevents_qualification(self) -> None:
        attempts = [attempt(index) for index in range(1, 21)]
        attempts.append(attempt(21, status="FAIL"))
        receipt = BENCHMARK.build_receipt(attempts, generated_at="2026-09-02T14:00:00Z")

        self.assertEqual("FAIL", receipt["status"])
        self.assertEqual("failed-attempts", receipt["qualification"]["state"])
        self.assertIsNone(receipt["qualification"]["qualified_level"])
        self.assertEqual(
            {"endpoint_timeout": 1}, receipt["aggregates"]["failure_codes"]
        )

    def test_incomplete_exact_tuple_never_qualifies(self) -> None:
        receipt = BENCHMARK.build_receipt(
            [
                attempt(index, tuple_overrides={"driver_version": None})
                for index in range(1, 21)
            ],
            generated_at="2026-09-02T14:00:00Z",
        )

        self.assertEqual("incomplete-evidence", receipt["qualification"]["state"])
        self.assertFalse(receipt["qualification"]["compatibility_tuple_complete"])
        self.assertIsNone(receipt["qualification"]["qualified_level"])

    def test_non_streaming_attempt_may_omit_wire_first_byte(self) -> None:
        value = attempt(1, capture_first_byte=False)
        BENCHMARK.validate_attempt(value)
        receipt = BENCHMARK.build_receipt([value], generated_at="2026-09-02T14:00:00Z")
        self.assertIsNone(
            receipt["aggregates"]["metrics_seconds"]["request_to_first_byte"]
        )

    def test_fresh_capacity_requires_request_and_reports_wait(self) -> None:
        value = attempt(
            1,
            capacity_wait=120.0,
            tuple_overrides={"capacity_state": "fresh-node-zero-pod"},
        )
        BENCHMARK.validate_attempt(value)
        value["timestamps"]["gpu_capacity_requested"] = None
        with self.assertRaisesRegex(
            BENCHMARK.FastStartEvidenceError, "missing capacity request"
        ):
            BENCHMARK.validate_attempt(value)

    def test_mixed_tuple_and_missing_attempt_cannot_be_aggregated(self) -> None:
        first = attempt(1)
        second = attempt(2, tuple_overrides={"model_revision": "c" * 40})
        with self.assertRaisesRegex(BENCHMARK.FastStartEvidenceError, "not comparable"):
            BENCHMARK.build_receipt([first, second])
        with self.assertRaisesRegex(BENCHMARK.FastStartEvidenceError, "contiguous"):
            BENCHMARK.build_receipt([first, attempt(3)])

    def test_image_identity_and_derived_aggregates_are_fail_closed(self) -> None:
        value = attempt(1)
        value["compatibility_tuple"]["runtime_image_digest"] = "sha256:" + digest(
            "different-image"
        )
        value["compatibility_tuple_digest"] = BENCHMARK.canonical_digest(
            value["compatibility_tuple"]
        )
        with self.assertRaisesRegex(
            BENCHMARK.FastStartEvidenceError, "reference and digest differ"
        ):
            BENCHMARK.validate_attempt(value)

        receipt = BENCHMARK.build_receipt(
            [attempt(1)], generated_at="2026-09-02T14:00:00Z"
        )
        receipt["aggregates"]["metrics_seconds"]
        receipt["aggregates"]["metrics_seconds"]["activation_to_ready"]["p95"] = 1
        unsigned = deepcopy(receipt)
        unsigned.pop("receipt_digest")
        receipt["receipt_digest"] = BENCHMARK.canonical_digest(unsigned)
        with self.assertRaisesRegex(
            BENCHMARK.FastStartEvidenceError, "derived aggregates"
        ):
            BENCHMARK.validate_receipt(receipt)

    def test_cli_aggregates_and_validates_new_private_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            attempt_path = root / "attempt-001.json"
            output_path = root / "receipt.json"
            attempt_path.write_text(json.dumps(attempt(1)), encoding="utf-8")
            self.assertEqual(
                0,
                BENCHMARK.main(
                    [
                        "aggregate",
                        "--attempt",
                        str(attempt_path),
                        "--generated-at",
                        "2026-09-02T14:00:00Z",
                        "--output",
                        str(output_path),
                    ]
                ),
            )
            self.assertEqual(
                0,
                BENCHMARK.main(["validate", "--receipt", str(output_path)]),
            )
            self.assertEqual(0o600, output_path.stat().st_mode & 0o777)


if __name__ == "__main__":
    unittest.main()
