from __future__ import annotations

import importlib.util
import json
import os
import stat
import subprocess
import sys
import threading
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from unittest import mock

MODULE_PATH = Path(__file__).resolve().parents[1] / "run_fleet_acceptance.py"
SPEC = importlib.util.spec_from_file_location(
    "fs2_scientific_fleet_orchestrator", MODULE_PATH
)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)

PRIMARY_PATHS = {
    "boltzgen": "models/cancer-immunotherapy/runtime-images/boltzgen/activation/fragment.json",
    "bindcraft": "models/cancer-immunotherapy/images/bindcraft-native/activation/fragment.json",
    "mosaic": "models/cancer-immunotherapy/runtime-images/mosaic/activation/fragment.json",
    "proteina-complexa": (
        "models/cancer-immunotherapy/runtime-images/proteina-complexa/activation/fragment.json"
    ),
    "rfdiffusion": (
        "models/cancer-immunotherapy/runtime-images/rfdiffusion/activation/fragment.json"
    ),
}
SECONDARY_DIRECTORIES = {
    "alphafold3": "alphafold3",
    "esmfold2": "esmfold2",
    "esmfold2-fast": "esmfold2-fast",
    "openfold3-openbind": "openfold3",
    "protenix-v2": "protenix-v2",
}
SECRET = "fleet-bearer-value-must-never-appear"


def canonical(value: object) -> bytes:
    return (
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n"
    ).encode()


def write_mode_0600(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(canonical(value))


def option(command: list[str], name: str) -> str:
    return command[command.index(name) + 1]


class FakeChildRunner:
    def __init__(
        self,
        *,
        failed_model: str | None = None,
        inject_sensitive_receipt: bool = False,
    ) -> None:
        self.failed_model = failed_model
        self.inject_sensitive_receipt = inject_sensitive_receipt
        self.active = 0
        self.maximum_active = 0
        self.commands: list[list[str]] = []
        self.lock = threading.Lock()

    def __call__(
        self, command: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[bytes]:
        with self.lock:
            self.commands.append(command)
            self.active += 1
            self.maximum_active = max(self.maximum_active, self.active)
        try:
            self._validate_invocation(command, kwargs)
            repository_root = Path(str(kwargs["cwd"]))
            fragment_path = repository_root / option(command, "--activation-fragment")
            model_id = json.loads(fragment_path.read_bytes())["model_id"]
            time.sleep(0.02)
            if model_id == self.failed_model:
                return subprocess.CompletedProcess(
                    command,
                    1,
                    stdout=b"",
                    stderr=b"scientific acceptance failed: http_submit_503\n",
                )
            receipt_path = Path(option(command, "--receipt"))
            receipt = self._receipt(model_id)
            write_mode_0600(receipt_path, receipt)
            return subprocess.CompletedProcess(
                command,
                0,
                stdout=b'{"status":"succeeded"}\n',
                stderr=b"",
            )
        finally:
            with self.lock:
                self.active -= 1

    @staticmethod
    def _validate_invocation(command: list[str], kwargs: dict[str, object]) -> None:
        assert command[1].endswith("run_acceptance.py")
        assert option(command, "--token-env") == "FS2_TEST_FLEET_TOKEN"
        assert SECRET not in command
        assert kwargs["stdin"] is subprocess.DEVNULL
        assert kwargs["stdout"] is subprocess.PIPE
        assert kwargs["stderr"] is subprocess.PIPE
        assert kwargs["check"] is False
        assert "env" not in kwargs

    def _receipt(self, model_id: str) -> dict[str, Any]:
        gpu_accounting: dict[str, Any] = {
            "scheduler_occupied_gpu_seconds": 20.0,
            "device_allocated_gpu_seconds": 19.5,
            "active_gpu_seconds": 15.0,
            "occupied_idle_gpu_seconds": 4.5,
            "phase_gpu_seconds": {
                "artifact_loading": 2.0,
                "active_compute": 15.0,
                "grace_drain": 2.5,
            },
        }
        if self.inject_sensitive_receipt:
            gpu_accounting["detail"] = f"Bearer {SECRET}"
        return {
            "schema": MODULE.MODEL_RECEIPT_SCHEMA,
            "endpoint": {"host": "inference.example", "tls": True},
            "model": {"model_id": model_id, "variant_id": f"{model_id}-h100"},
            "operation_identity": {
                "operation_id": f"operation-{model_id}",
                "batch_id": f"batch-{model_id}",
                "workload_id": f"workload-{model_id}",
            },
            "terminal_state": {
                "operation": "succeeded",
                "batch": "succeeded",
                "result": "succeeded",
                "semantic_validation": "passed",
            },
            "timestamps": {
                "accepted_at": "2026-09-04T12:00:00Z",
                "available_at": "2026-09-04T12:00:00Z",
                "activation_started_at": "2026-09-04T12:00:01Z",
                "ready_at": "2026-09-04T12:00:04Z",
                "started_at": "2026-09-04T12:00:04Z",
                "completed_at": "2026-09-04T12:00:20Z",
                "result_submitted_at": "2026-09-04T12:00:00Z",
                "result_completed_at": "2026-09-04T12:00:20Z",
            },
            "cold_start": {
                "cold_start_seconds": 3.0,
                "runtime": {
                    "pod_uid": f"pod-{model_id}",
                    "node_uid": "node-h100",
                    "gpu_uuids": ["GPU-test"],
                    "gpu_count": 1,
                    "preemptible": False,
                },
            },
            "execution_identity": {
                "model_id": model_id,
                "variant_id": f"{model_id}-h100",
                "model_revision": "a" * 40,
                "runtime_image_digest": "sha256:" + "b" * 64,
                "runtime_recipe_sha256": "c" * 64,
                "workload_recipe_sha256": "d" * 64,
                "model_artifact_manifest_digest": "e" * 64,
                "execution_identity_sha256": "f" * 64,
            },
            "queue": {
                "scheduling_snapshot_digest": "1" * 64,
                "policy_revision": "policy-1",
                "captured_at": "2026-09-04T12:00:00Z",
                "service_class": "customer-batch",
                "tenant_queue": "scientific",
                "model_lane": model_id,
                "stage_decisions": [],
                "observed_stages": [],
            },
            "attempts": [
                {
                    "attempt_id": f"attempt-{model_id}",
                    "stage_id": "run",
                    "started_at": "2026-09-04T12:00:01Z",
                    "completed_at": "2026-09-04T12:00:20Z",
                    "gpu_uuids": ["GPU-test"],
                }
            ],
            "artifact_digests": {
                "uploads": [],
                "input_manifest": {"sha256": "2" * 64},
                "output_manifest": {"sha256": "3" * 64},
                "semantic_validation_receipt_sha256": "4" * 64,
            },
            "gpu_accounting": gpu_accounting,
        }


class FleetAcceptanceTest(unittest.TestCase):
    def _repository(self, root: Path) -> Path:
        for model_id, relative in PRIMARY_PATHS.items():
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(
                canonical(
                    {
                        "model_id": model_id,
                        "public_fixtures": {"request": f"fixtures/{model_id}.json"},
                    }
                )
            )
        for model_id, directory in SECONDARY_DIRECTORIES.items():
            path = (
                root
                / "models/structure/batch-adapters"
                / directory
                / "activation/public-acceptance.json"
            )
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(
                canonical(
                    {
                        "model_id": model_id,
                        "public_fixtures": {"request": f"fixtures/{model_id}.json"},
                    }
                )
            )
        return root

    def _config(self, root: Path) -> MODULE.FleetConfig:
        return MODULE.FleetConfig(
            endpoint="https://inference.example",
            repository_root=root,
            receipt_root=root / "receipts",
            run_id="fleet-test-01",
            token_environment="FS2_TEST_FLEET_TOKEN",
            max_parallel=3,
            timeout_seconds=60,
            poll_seconds=0.01,
            request_timeout_seconds=2,
        )

    def test_repository_discovery_finds_exact_current_fleet(self) -> None:
        repository_root = MODULE_PATH.parents[2]
        discovered = MODULE.discover_inputs(repository_root)

        self.assertEqual(len(discovered), 10)
        self.assertEqual(
            {item.model_id for item in discovered},
            MODULE.EXPECTED_PRIMARY | MODULE.EXPECTED_SECONDARY,
        )
        self.assertEqual(
            sum(item.kind == "primary-activation-fragment" for item in discovered),
            5,
        )
        self.assertEqual(
            sum(item.kind == "secondary-public-acceptance" for item in discovered),
            5,
        )

    def test_parallel_run_writes_secure_canonical_receipts(self) -> None:
        with TemporaryDirectory() as directory:
            root = self._repository(Path(directory))
            child = FakeChildRunner()
            with (
                mock.patch.dict(
                    os.environ, {"FS2_TEST_FLEET_TOKEN": SECRET}, clear=False
                ),
                mock.patch.object(MODULE.subprocess, "run", side_effect=child),
            ):
                result = MODULE.run_fleet(self._config(root))

            self.assertEqual((result.succeeded, result.failed), (10, 0))
            self.assertGreaterEqual(child.maximum_active, 2)
            self.assertLessEqual(child.maximum_active, 3)
            self.assertEqual(len(child.commands), 10)
            aggregate_bytes = result.aggregate_path.read_bytes()
            self.assertEqual(
                aggregate_bytes,
                MODULE._canonical_json(json.loads(aggregate_bytes), newline=True),
            )
            self.assertNotIn(SECRET.encode(), aggregate_bytes)
            self.assertEqual(stat.S_IMODE(result.aggregate_path.stat().st_mode), 0o600)
            self.assertEqual(
                [item["model_id"] for item in result.aggregate["models"]],
                sorted(MODULE.EXPECTED_PRIMARY | MODULE.EXPECTED_SECONDARY),
            )
            for item in result.aggregate["models"]:
                accounting = item["api_measurements"]["gpu_occupied_idle"]
                self.assertTrue(accounting["available"])
                self.assertEqual(accounting["source_field"], "gpu_accounting")
                self.assertEqual(accounting["value"]["occupied_idle_gpu_seconds"], 4.5)
                receipt = result.aggregate_path.parent / item["receipt"]["path"]
                self.assertEqual(stat.S_IMODE(receipt.stat().st_mode), 0o600)
                self.assertNotIn(SECRET.encode(), receipt.read_bytes())
            for command in child.commands:
                self.assertNotIn(SECRET, command)

    def test_one_child_failure_does_not_cancel_other_models(self) -> None:
        with TemporaryDirectory() as directory:
            root = self._repository(Path(directory))
            child = FakeChildRunner(failed_model="mosaic")
            with (
                mock.patch.dict(
                    os.environ, {"FS2_TEST_FLEET_TOKEN": SECRET}, clear=False
                ),
                mock.patch.object(MODULE.subprocess, "run", side_effect=child),
            ):
                result = MODULE.run_fleet(self._config(root))

            self.assertEqual((result.succeeded, result.failed), (9, 1))
            failed = [
                item
                for item in result.aggregate["models"]
                if item["status"] == "failed"
            ]
            self.assertEqual(
                failed,
                [
                    {
                        "model_id": "mosaic",
                        "input": {
                            "kind": "primary-activation-fragment",
                            "path": PRIMARY_PATHS["mosaic"],
                            "sha256": failed[0]["input"]["sha256"],
                        },
                        "status": "failed",
                        "error_code": "http_submit_503",
                        "api_measurements": None,
                    }
                ],
            )
            self.assertFalse((result.aggregate_path.parent / "mosaic.json").exists())
            self.assertEqual(len(child.commands), 10)

    def test_sensitive_child_receipt_is_rejected_and_removed(self) -> None:
        with TemporaryDirectory() as directory:
            root = self._repository(Path(directory))
            child = FakeChildRunner(inject_sensitive_receipt=True)
            with (
                mock.patch.dict(
                    os.environ, {"FS2_TEST_FLEET_TOKEN": SECRET}, clear=False
                ),
                mock.patch.object(MODULE.subprocess, "run", side_effect=child),
            ):
                result = MODULE.run_fleet(self._config(root))

            self.assertEqual((result.succeeded, result.failed), (0, 10))
            self.assertNotIn(SECRET.encode(), result.aggregate_path.read_bytes())
            self.assertEqual(
                {item["error_code"] for item in result.aggregate["models"]},
                {"receipt_redaction_failed"},
            )
            self.assertEqual(
                sorted(path.name for path in result.aggregate_path.parent.iterdir()),
                ["aggregate.json"],
            )

    def test_missing_input_and_existing_receipt_fail_before_children_start(
        self,
    ) -> None:
        with TemporaryDirectory() as directory:
            root = self._repository(Path(directory))
            missing = root / PRIMARY_PATHS["rfdiffusion"]
            missing.unlink()
            with self.assertRaisesRegex(
                MODULE.FleetAcceptanceError, "acceptance_input_set_incomplete"
            ):
                MODULE.discover_inputs(root)

        with TemporaryDirectory() as directory:
            root = self._repository(Path(directory))
            config = self._config(root)
            existing = config.receipt_root / config.run_id / "mosaic.json"
            write_mode_0600(existing, {"stale": True})
            child = FakeChildRunner()
            with (
                mock.patch.dict(
                    os.environ, {"FS2_TEST_FLEET_TOKEN": SECRET}, clear=False
                ),
                mock.patch.object(MODULE.subprocess, "run", side_effect=child),
                self.assertRaisesRegex(MODULE.FleetAcceptanceError, "receipt_exists"),
            ):
                MODULE.run_fleet(config)
            self.assertEqual(child.commands, [])

    def test_missing_token_environment_fails_before_discovery(self) -> None:
        with TemporaryDirectory() as directory:
            root = self._repository(Path(directory))
            with (
                mock.patch.dict(os.environ, {}, clear=True),
                self.assertRaisesRegex(
                    MODULE.FleetAcceptanceError, "token_environment_missing"
                ),
            ):
                MODULE.run_fleet(self._config(root))


if __name__ == "__main__":
    unittest.main()
