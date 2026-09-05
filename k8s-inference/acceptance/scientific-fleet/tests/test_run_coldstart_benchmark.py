from __future__ import annotations

import importlib.util
import json
import os
import stat
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from typing import Any
from unittest import mock
from urllib.parse import parse_qs, urlsplit
from uuid import NAMESPACE_URL, uuid5

MODULE_PATH = Path(__file__).resolve().parents[1] / "run_coldstart_benchmark.py"
SPEC = importlib.util.spec_from_file_location(
    "fs2_scientific_coldstart_benchmark", MODULE_PATH
)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)

INFERENCE_SECRET = "inference-secret-that-must-not-be-written"
ADMIN_SECRET = "admin-secret-that-must-not-be-written"


def measurement(value: float | None, phase: str) -> dict[str, Any]:
    if value is None:
        return {
            "value": None,
            "unit": "seconds",
            "evidence": "unavailable",
            "source": "scientific-controller-events",
            "reason": f"No closed {phase} interval is available.",
        }
    return {
        "value": value,
        "unit": "seconds",
        "evidence": "measured",
        "source": "scientific-controller-events",
        "reason": None,
    }


def public_row(model_id: str, operation_id: str) -> dict[str, Any]:
    return {
        "model_id": model_id,
        "status": "succeeded",
        "operation_identity": {
            "operation_id": operation_id,
            "batch_id": str(uuid5(NAMESPACE_URL, f"batch/{operation_id}")),
            "workload_id": str(uuid5(NAMESPACE_URL, f"workload/{operation_id}")),
        },
        "terminal_state": {
            "operation": "succeeded",
            "batch": "succeeded",
            "result": "succeeded",
            "semantic_validation": "passed",
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
        "api_measurements": {
            "cold_start": {
                "cold_start_seconds": 6.0,
                "runtime": {
                    "pod_uid": "pod-uid",
                    "node_uid": "node-uid",
                    "gpu_uuids": ["GPU-test"],
                    "gpu_count": 1,
                    "preemptible": False,
                },
            },
            "runtime": {
                "runtime_identity": {},
                "timestamps": {
                    "accepted_at": "2026-09-04T12:00:00Z",
                    "available_at": "2026-09-04T12:00:00Z",
                    "activation_started_at": "2026-09-04T12:00:01Z",
                    "ready_at": "2026-09-04T12:00:06Z",
                    "started_at": "2026-09-04T12:00:07Z",
                    "completed_at": "2026-09-04T12:00:20Z",
                    "result_submitted_at": "2026-09-04T12:00:00Z",
                    "result_completed_at": "2026-09-04T12:00:21Z",
                },
                "attempts": [],
            },
            "queue": {},
            "gpu_occupied_idle": {
                "available": False,
                "source_field": None,
                "value": None,
            },
        },
    }


def admin_detail(model_id: str, operation_id: str, pool_id: str) -> dict[str, Any]:
    phase_values = {
        "queue": 1.0,
        "admission": 2.0,
        "image-pull": 3.0,
        "artifact-load": 4.0,
        "restore": 0.5,
        "semantic-warmup": 1.5,
        "active-compute": 13.0,
    }
    return {
        "run": {
            "id": operation_id,
            "status": "succeeded",
            "model": {"model_id": model_id},
            "fast_start": {
                "tier": "model-artifact-local",
                "evidence": "observed",
                "observed_at": "2026-09-04T12:00:07Z",
                "runtime_identity_digest": "f" * 64,
                "reason": "Observed from the exact attempt.",
            },
        },
        "lifecycle_phases": [
            {"phase": phase, "duration": measurement(value, phase)}
            for phase, value in phase_values.items()
        ],
        "stages": [
            {
                "id": "design",
                "resource_class": "gpu",
                "attempts": [
                    {
                        "id": str(uuid5(NAMESPACE_URL, f"attempt/{operation_id}")),
                        "number": 1,
                        "status": "succeeded",
                        "admitted_at": "2026-09-04T12:00:03Z",
                        "started_at": "2026-09-04T12:00:07Z",
                        "completed_at": "2026-09-04T12:00:20Z",
                        "gpu_count": 1,
                        "resolved_pool_id": pool_id,
                        "admitted_resource_flavor": f"inference-{pool_id}",
                        "accelerator_resource_name": "nvidia.com/gpu",
                    }
                ],
            }
        ],
    }


def lifecycle(operation_id: str, *, exact: bool = True) -> dict[str, Any]:
    return {
        "items": [
            {
                "subject": {"operation_id": operation_id},
                "rollup": {
                    "terminal": True,
                    "scheduler_occupied_gpu_seconds": 20.0,
                    "device_allocated_gpu_seconds": 19.5,
                    "active_gpu_seconds": 15.0,
                    "occupied_idle_gpu_seconds": 4.5,
                    "phase_gpu_seconds": {
                        "artifact_load": 2.0,
                        "active_compute": 15.0,
                        "grace_drain": 2.5,
                    },
                    "reconciled": exact,
                    "quality": "measured" if exact else "application_observed",
                    "data_gaps": [] if exact else ["device-allocation-clock"],
                },
            }
        ],
        "total": 1,
    }


def pool(pool_id: str, capacity_type: str) -> dict[str, Any]:
    return {
        "pool_id": pool_id,
        "capacity_type": capacity_type,
        "cache_tier": "SharedFilesystem",
        "startup_scenario": "prepared-node-zero-pod",
        "accelerator": {
            "acceleratorClass": "nvidia-h100-sxm5-80gb",
            "gpuProduct": "NVIDIA H100 80GB HBM3",
            "computeCapability": "9.0",
            "memoryBytes": 85520809984,
        },
        "driver_cuda": {"driverVersion": "580.159.04", "cudaVersion": "13.0"},
        "storage_runtime": {
            "storageClass": "csi-mounted-fs-path-sc",
            "storageMode": "rwx-filesystem",
        },
        "environment_digest": "sha256:" + "1" * 64,
        "valid_until": "2026-09-11T23:59:59Z",
    }


class FakeAdminClient:
    def __init__(
        self,
        operations: dict[str, tuple[str, str]],
        *,
        model_readiness: str = "qualified",
    ) -> None:
        self.operations = operations
        self.model_readiness = model_readiness
        self.deleted = False

    def request(
        self,
        method: str,
        path: str,
        *,
        headers: dict[str, str] | None = None,
        **_: object,
    ) -> Any:
        if method == "POST" and path == "/admin/api/v1/session":
            return SimpleNamespace(
                status=200,
                headers={
                    "set-cookie": "__Host-fs2_admin_session=session-value; Path=/; Secure; HttpOnly; SameSite=strict"
                },
                body=b'{"data":{}}',
            )
        if method == "DELETE" and path == "/admin/api/v1/session":
            assert headers == {"Cookie": "__Host-fs2_admin_session=session-value"}
            self.deleted = True
            return SimpleNamespace(status=204, headers={}, body=b"")
        assert method == "GET"
        assert headers == {"Cookie": "__Host-fs2_admin_session=session-value"}
        if path == "/admin/api/v1/scientific-models":
            items = [
                {
                    "model_id": model_id,
                    "readiness": self.model_readiness,
                    "workload_profile": "published",
                    "batch_supported": True,
                    "backend": {
                        "runtime_image_digest": "sha256:" + "b" * 64,
                        "execution_identity_digest": "f" * 64,
                    },
                    "access": {"state": "not-required"},
                    "caching": {
                        "exact_tier": "model-artifact-local",
                        "image": "verified",
                        "artifacts": "verified",
                        "reference_data": "verified",
                        "runtime_checkpoint": "unavailable",
                        "gpu_snapshot": "unavailable",
                        "reason": "Shared artifacts are exact.",
                    },
                }
                for model_id in sorted(MODULE.EXPECTED_MODELS)
            ]
            return self._json({"data": {"items": items}})
        if path.startswith("/admin/api/v1/scientific-runs/"):
            operation_id = path.rsplit("/", 1)[-1]
            model_id, pool_id = self.operations[operation_id]
            return self._json({"data": admin_detail(model_id, operation_id, pool_id)})
        if path.startswith("/admin/api/v1/telemetry/workloads?"):
            query = parse_qs(urlsplit(path).query)
            operation_id = query["operation_id"][0]
            return self._json({"data": lifecycle(operation_id)})
        raise AssertionError(path)

    @staticmethod
    def _json(value: object) -> Any:
        return SimpleNamespace(status=200, headers={}, body=json.dumps(value).encode())


class ScientificColdStartBenchmarkTest(unittest.TestCase):
    def test_candidate_model_set_is_rejected_by_preflight(self) -> None:
        client = FakeAdminClient({}, model_readiness="candidate")

        with self.assertRaisesRegex(
            MODULE.BenchmarkError, "admin_model_not_qualified"
        ):
            MODULE._model_snapshot(client, "__Host-fs2_admin_session=session-value")

    def test_attempt_preserves_phase_placement_and_exact_lifecycle(self) -> None:
        operation_id = str(uuid5(NAMESPACE_URL, "operation/boltzgen"))
        with TemporaryDirectory() as directory:
            root = Path(directory)
            aggregate = root / "fleet/r01/aggregate.json"
            aggregate.parent.mkdir(parents=True)
            aggregate.write_text("{}\n", encoding="utf-8")
            result = MODULE._attempt(
                repetition=1,
                public_row=public_row("boltzgen", operation_id),
                public_aggregate_path=aggregate,
                run_directory=root,
                admin=MODULE.AdminEvidence(
                    admin_detail("boltzgen", operation_id, "h100-reserved-8x"),
                    None,
                    lifecycle(operation_id),
                    None,
                ),
                model_snapshot={"caching": {"exact_tier": "model-artifact-local"}},
                pools={"h100-reserved-8x": pool("h100-reserved-8x", "regular")},
                reserved_pool_ids=frozenset({"h100-reserved-8x"}),
            )

        self.assertEqual(result["placement"]["classification"], "reserved")
        self.assertEqual(result["measurements"]["queue_wait_seconds"]["value"], 1.0)
        self.assertEqual(result["measurements"]["capacity_wait_seconds"]["value"], 2.0)
        self.assertEqual(result["measurements"]["image_localization_seconds"]["value"], 3.0)
        self.assertEqual(result["measurements"]["artifact_localization_seconds"]["value"], 4.0)
        self.assertIsNone(result["measurements"]["runtime_model_load_seconds"]["value"])
        self.assertEqual(result["measurements"]["compile_warmup_seconds"]["value"], 1.5)
        self.assertEqual(result["measurements"]["time_to_first_semantic_result_seconds"]["value"], 21.0)
        self.assertEqual(result["measurements"]["total_runtime_seconds"]["value"], 13.0)
        self.assertTrue(result["lifecycle_accounting"]["exact"])
        self.assertEqual(
            result["lifecycle_accounting"]["scheduler_occupied_gpu_seconds"]["value"],
            20.0,
        )

    def test_nonreconciled_lifecycle_keeps_values_without_claiming_exactness(self) -> None:
        operation_id = str(uuid5(NAMESPACE_URL, "operation/non-reconciled"))
        result = MODULE._lifecycle_accounting(lifecycle(operation_id, exact=False), None)

        self.assertTrue(result["available"])
        self.assertFalse(result["exact"])
        self.assertFalse(result["reconciled"])
        self.assertEqual(result["quality"], "application_observed")
        self.assertEqual(result["scheduler_occupied_gpu_seconds"]["value"], 20.0)
        self.assertFalse(result["scheduler_occupied_gpu_seconds"]["exact"])
        self.assertEqual(result["data_gaps"], ["device-allocation-clock"])

    def test_statistics_are_split_by_exact_pool_cohort(self) -> None:
        def attempt(repetition: int, pool_id: str) -> dict[str, Any]:
            measurements = {
                metric: measurement(float(repetition), metric)
                for metric in MODULE.STATISTIC_METRICS
            }
            return {
                "repetition": repetition,
                "status": "succeeded",
                "execution_identity": {
                    "execution_identity_sha256": "f" * 64,
                    "runtime_image_digest": "sha256:" + "b" * 64,
                },
                "placement": {
                    "pool_ids": [pool_id],
                    "capacity_types": [
                        "preemptible" if pool_id == "h100-1x" else "regular"
                    ],
                },
                "cache": {
                    "environment_cache_tiers": ["SharedFilesystem"],
                    "run_observation": {"tier": "model-artifact-local", "evidence": "observed"},
                },
                "measurements": measurements,
            }

        cohorts = MODULE._cohorts(
            [
                attempt(1, "h100-reserved-8x"),
                attempt(2, "h100-reserved-8x"),
                attempt(3, "h100-reserved-8x"),
                attempt(4, "h100-1x"),
            ]
        )

        self.assertEqual(len(cohorts), 2)
        reserved = next(
            item for item in cohorts if item["identity"]["pool_ids"] == ["h100-reserved-8x"]
        )
        self.assertTrue(reserved["exploratory_minimum_met"])
        stats = reserved["statistics"]["total_runtime_seconds"]["statistics"]
        self.assertEqual(stats["sample_count"], 3)
        self.assertEqual(stats["p95"], 3.0)

    def test_full_composition_reuses_fleet_runner_and_writes_secure_receipt(self) -> None:
        operations: dict[str, tuple[str, str]] = {}
        fake_admin = FakeAdminClient(operations)
        calls: list[Any] = []

        def fake_fleet(config: Any) -> Any:
            calls.append(config)
            rows = []
            for model_id in sorted(MODULE.EXPECTED_MODELS):
                operation_id = str(uuid5(NAMESPACE_URL, f"{config.run_id}/{model_id}"))
                pool_id = "h100-reserved-8x"
                operations[operation_id] = (model_id, pool_id)
                rows.append(public_row(model_id, operation_id))
            aggregate = {
                "schema": MODULE.FLEET.AGGREGATE_SCHEMA,
                "run_id": config.run_id,
                "endpoint": {"host": "inference.example", "tls": True},
                "summary": {"discovered": 10, "succeeded": 10, "failed": 0},
                "models": rows,
            }
            path = config.receipt_root / config.run_id / "aggregate.json"
            path.parent.mkdir(parents=True, mode=0o700)
            path.write_text(json.dumps(aggregate) + "\n", encoding="utf-8")
            os.chmod(path, 0o600)
            return SimpleNamespace(aggregate=aggregate, aggregate_path=path)

        bindings = [
            {
                "scope": {
                    "projectId": "project-e00rene",
                    "region": "eu-north1",
                    "clusterContext": "k8s-inference-h100",
                },
                "accelerator": pool("h100-reserved-8x", "regular")["accelerator"],
                "driverCuda": pool("h100-reserved-8x", "regular")["driver_cuda"],
                "storageRuntime": pool("h100-reserved-8x", "regular")["storage_runtime"],
                "hostRuntimeDigest": "sha256:" + "2" * 64,
                "environment": {"qualificationDigest": "sha256:" + "1" * 64},
                "members": [{"poolRef": "h100-reserved-8x", "capacityType": "regular"}],
                "cacheTier": "SharedFilesystem",
                "startupScenario": "prepared-node-zero-pod",
                "validUntil": "2026-09-11T23:59:59Z",
            }
        ]

        with TemporaryDirectory() as directory:
            output_root = Path(directory) / "receipts"
            config = MODULE.BenchmarkConfig(
                endpoint="https://inference.example",
                repository_root=MODULE.SOLUTION_ROOT,
                receipt_root=output_root,
                run_id="scientific-coldstart-test",
                environment_qualifications=Path(directory) / "environment.json",
                project_id="project-e00rene",
                region="eu-north1",
                cluster_context="k8s-inference-h100",
                repetitions=3,
                max_parallel=8,
                admin_convergence_seconds=0,
                reserved_pool_ids=("h100-reserved-8x",),
            )
            with (
                mock.patch.dict(
                    os.environ,
                    {
                        "FS2_INFERENCE_TOKEN": INFERENCE_SECRET,
                        "FS2_ADMIN_TOKEN": ADMIN_SECRET,
                    },
                    clear=False,
                ),
                mock.patch.object(MODULE, "_environment_bindings", return_value=(bindings, "3" * 64)),
                mock.patch.object(MODULE, "_source_commit", return_value="4" * 40),
                mock.patch.object(MODULE.PUBLIC, "PublicApiClient", return_value=fake_admin),
                mock.patch.object(MODULE.FLEET, "run_fleet", side_effect=fake_fleet),
            ):
                receipt, output = MODULE.run_benchmark(config)

            body = output.read_bytes()
            self.assertEqual(len(calls), 3)
            self.assertEqual(receipt["summary"]["attempts"], 30)
            self.assertEqual(receipt["summary"]["succeeded"], 30)
            self.assertEqual(receipt["summary"]["exact_lifecycle_attempts"], 30)
            self.assertTrue(receipt["desired_state"]["unchanged"])
            self.assertTrue(fake_admin.deleted)
            self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o600)
            self.assertNotIn(INFERENCE_SECRET.encode(), body)
            self.assertNotIn(ADMIN_SECRET.encode(), body)
            self.assertNotIn(b"session-value", body)


if __name__ == "__main__":
    unittest.main()
