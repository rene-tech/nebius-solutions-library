from __future__ import annotations

import argparse
import copy
import hashlib
import importlib.util
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "full_catalog_baseline.py"
SPEC = importlib.util.spec_from_file_location("fs2_full_catalog_baseline", SCRIPT)
assert SPEC and SPEC.loader
BASELINE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(BASELINE)

DCGM_SCRIPT = ROOT / "capture_dcgm_attribution.py"
DCGM_SPEC = importlib.util.spec_from_file_location(
    "fs2_dcgm_for_baseline_test", DCGM_SCRIPT
)
assert DCGM_SPEC and DCGM_SPEC.loader
DCGM = importlib.util.module_from_spec(DCGM_SPEC)
DCGM_SPEC.loader.exec_module(DCGM)

SOURCE_COMMIT = "5559d0cd6f3be8faf9c93bfdaae9f2531a05c5d1"
SOURCE_TREE = "d6c877510badaf20bf92cc0a6bdc5c71ac066fdf"


def private_json(path: Path, value: object) -> None:
    BASELINE.write_json_new(path.resolve(), value)
    assert (path.stat().st_mode & 0o777) == 0o600


def compatibility_tuple(backend: dict) -> dict:
    content = backend["identity_admission"]["content_digest"]
    return {
        "model_id": backend["model_id"],
        "model_content_digest": content,
        "tokenizer_or_preprocessor_digest": "1" * 64,
        "semantic_oracle_digest": "2" * 64,
        "semantic_request_contract_digest": backend["semantic_contract_digest"],
        "runtime_variant": "nim/exact",
        "runtime_source_identity_digest": "3" * 64,
        "runtime_image_digest": backend["runtime_image_digest"],
        "runtime_argv_digest": "4" * 64,
        "runtime_environment_digest": "5" * 64,
        "execution_identity_digest": "6" * 64,
        "loader_or_engine_format": "nim-cache",
        "host_cpu_architecture": "amd64",
        "host_os_release_digest": "7" * 64,
        "accelerator_pool_id": "preemptible-test",
        "accelerator_pool_receipt_digest": "8" * 64,
        "gpu_vendor": "nvidia",
        "gpu_product": "NVIDIA B300",
        "gpu_chip_type": "B300",
        "gpu_compute_capability": "10.3",
        "gpu_memory_bytes": 309237645312,
        "workload_gpu_count": backend["gpu_count"],
        "gpu_topology": "single-device",
        "gpu_topology_inventory_digest": "9" * 64,
        "allocated_gpu_uuids": ["GPU-test-0001"],
        "mig_mode": "disabled",
        "mig_profile": None,
        "driver_version": "580.173.02",
        "cuda_version": "13.0.3",
        "kernel_release": "6.8.0-test",
        "container_runtime_name": "containerd",
        "container_runtime_version": "2.1.4",
        "checkpoint_tool_digest": "a" * 64,
        "criu_version": "not-installed",
        "artifact_manifest_digest": backend["identity_admission"][
            "artifact_manifest_digest"
        ],
        "artifact_content_digest": content,
        "artifact_bytes": backend["identity_admission"]["expanded_bytes"],
        "storage_class": "network-ssd",
        "storage_mode": "ReadWriteOnce",
        "node_identity_digest": "b" * 64,
        "pvc_identity_digest": "c" * 64,
        "compile_cache_abi": "driver-580.173.02-sm103",
        "capacity_state": "fresh-node-zero-pod",
    }


class FullCatalogBaselineTests(unittest.TestCase):
    def _plan(
        self, directory: Path, *, nim_cache_identities: list[Path] | None = None
    ) -> dict:
        acceptance = directory / "acceptance.json"
        provenance = directory / "terraform.json"
        private_json(
            acceptance,
            {
                "schema": "test/all-model-acceptance/v1",
                "source_commit": SOURCE_COMMIT,
                "result": "PASS",
            },
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
                "plan_hashes": {
                    "infrastructure": "a" * 64,
                    "foundation": "b" * 64,
                    "workloads": "c" * 64,
                },
                "feature_flags": {
                    "challengers_enabled": False,
                    "cold_start_mechanism": "conventional",
                    "hot_model_ids": [],
                },
            },
        )
        return BASELINE.build_plan(
            argparse.Namespace(
                campaign_id="baseline-test",
                source_commit=SOURCE_COMMIT,
                source_tree=SOURCE_TREE,
                acceptance_receipt=acceptance.resolve(),
                terraform_provenance=provenance.resolve(),
                nim_cache_identity=nim_cache_identities or [],
            )
        )

    def _dcgm_cadence_binding(self, directory: Path) -> Path:
        cadence = DCGM._cadence_binding_module()
        profile, profile_digest = cadence._profile_contract(
            DCGM.CADENCE_PROFILE_PATH, True
        )
        monitor = profile["helmValues"]["serviceMonitor"]
        output = {
            "schema": "fs2-serve.nebius.ai/dcgm-attribution-terraform/v1",
            "campaign_enabled": True,
            "attribution_metric_collection_interval": "1s",
            "scrape_interval": "1s",
            "scrape_timeout": "900ms",
            "campaign_metrics": list(DCGM.METRICS),
            "minimum_nominal_window_seconds": 2,
            "missing_sample_policy": "FAIL_CLOSED_NO_ESTIMATE",
        }
        config_identity = {
            "namespace": "fs2-observability",
            "name": "fs2-dcgm-exporter-config",
            "uid": "config-uid-1",
            "resource_version": "101",
        }
        binding = {
            "schema": cadence.SCHEMA,
            "source": {"commit": SOURCE_COMMIT, "tree": SOURCE_TREE},
            "terraform": {
                "saved_plan_sha256": "c" * 64,
                "cadence_profile_sha256": profile_digest,
                "dcgm_attribution_contract": output,
                "dcgm_attribution_contract_sha256": cadence.digest(output),
            },
            "observed": {
                "config_map": {
                    "identity": config_identity,
                    "data_sha256": hashlib.sha256(
                        profile["helmValues"]["config"]["data"].encode("utf-8")
                    ).hexdigest(),
                },
                "service_monitor": {
                    "identity": {
                        "namespace": "fs2-observability",
                        "name": "fs2-dcgm-exporter",
                        "uid": "monitor-uid-1",
                        "resource_version": "102",
                    },
                    "generation": 2,
                    "interval": monitor["interval"],
                    "scrape_timeout": monitor["scrapeTimeout"],
                    "metric_relabelings": monitor["metricRelabelings"],
                    "spec_sha256": "4" * 64,
                },
                "daemon_set": {
                    "identity": {
                        "namespace": "fs2-observability",
                        "name": "fs2-dcgm-exporter",
                        "uid": "daemonset-uid-1",
                        "resource_version": "103",
                    },
                    "generation": 3,
                    "observed_generation": 3,
                    "desired_number_scheduled": 1,
                    "updated_number_scheduled": 1,
                    "number_ready": 1,
                    "exporter_image": cadence.EXPORTER_IMAGE,
                    "config_map_name": config_identity["name"],
                    "config_map_uid": config_identity["uid"],
                    "config_map_resource_version": config_identity["resource_version"],
                    "pod_template_sha256": "5" * 64,
                    "ready_pod_uids": ["exporter-pod-uid-1"],
                },
            },
            "captured_at": "2026-08-28T11:59:59Z",
        }
        binding["receipt_digest"] = cadence.digest(binding)
        path = directory / "dcgm-cadence.json"
        path.write_bytes(cadence.canonical_bytes(binding))
        os.chmod(path, 0o600)
        return path

    def _nim_cache_identity(self, directory: Path, model_id: str) -> Path:
        routes = BASELINE.load_json(BASELINE.ROUTE_PATH)["routes"]
        catalog = BASELINE.load_json(
            BASELINE.FS2_ROOT / f"catalog/runtime/models/{model_id}.json"
        )
        route = routes[model_id]
        files = [
            {
                "path": "profiles/download.complete",
                "bytes": 0,
                "sha256": hashlib.sha256(b"").hexdigest(),
            },
            {
                "path": "profiles/model.cache",
                "bytes": 3,
                "sha256": hashlib.sha256(b"abc").hexdigest(),
            },
        ]
        capture = {
            "started_at": "2026-08-28T12:00:00Z",
            "completed_at": "2026-08-28T12:00:01Z",
            "file_count": 2,
            "expanded_bytes": 3,
            "content_digest": BASELINE.canonical_digest(files),
            "metadata_digest": "d" * 64,
        }
        manifest = {
            "schema": "fs2-serve.nebius.ai/artifact-manifest/v1",
            "model_id": model_id,
            "kind": "nim-cache",
            "source": {
                "uri": f"ngc://{catalog['model']['source']['repository']}",
                "revision": route["model_revision"],
            },
            "content": {
                "digest": BASELINE.canonical_digest(files),
                "expanded_bytes": 3,
                "files": files,
            },
            "license": {
                "id": catalog["model"]["source"]["license"]["id"],
                "state": catalog["model"]["source"]["license"]["state"],
            },
            "entitlement_state": catalog["model"]["source"]["entitlement"]["state"],
            "owner": catalog["cache"]["owner"],
            "retention": "ephemeral-test",
        }
        receipt = {
            "schema": "fs2-serve.nebius.ai/nim-cache-manifest-capture/v1",
            "source": {"commit": SOURCE_COMMIT, "tree": SOURCE_TREE},
            "subject": {
                "model_id": model_id,
                "namespace": "fs2-models",
                "namespace_uid": "namespace-uid",
                "pod_uid": "pod-uid",
                "container": "nim",
                "pvc_name": f"{model_id}-nim-cache",
                "pvc_uid": "pvc-uid",
                "cache_root": "/opt/nim/.cache",
                "runtime_image_digest": route["runtime_image_digest"],
                "capacity_bound_bytes": catalog["cache"]["artifact"][
                    "capacity_bound_bytes"
                ],
            },
            "stability": {
                "method": "two-complete-sha256-passes-with-descriptor-stat-guards",
                "delay_seconds": 1,
                "first": capture,
                "second": {
                    **capture,
                    "started_at": "2026-08-28T12:00:02Z",
                    "completed_at": "2026-08-28T12:00:03Z",
                },
                "stable": True,
            },
            "artifact_manifest": manifest,
            "artifact_manifest_digest": BASELINE.canonical_digest(manifest),
            "captured_at": "2026-08-28T12:00:04Z",
        }
        receipt["receipt_digest"] = BASELINE.canonical_digest(receipt)
        path = directory / f"{model_id}-identity.json"
        private_json(path, receipt)
        return path

    def test_discovers_exact_fifteen_routes_and_sixteen_backends(self) -> None:
        backends = BASELINE.discover_backends()
        self.assertEqual(len(backends), 16)
        self.assertEqual(
            len({item["route_id"] for item in backends if item["route_id"]}), 15
        )
        fallback = [item for item in backends if item["resource_class"] == "cpu"]
        self.assertEqual(
            [item["backend_id"] for item in fallback], ["msa-search-pdb70-fallback"]
        )
        self.assertEqual(
            fallback[0]["runtime_image_digest"],
            "sha256:64bd29bc83915a145ac5d55ec9c6de1178bc2be2f2d9fdc1b789b9b6a5c78136",
        )
        self.assertEqual(
            fallback[0]["identity_relationship"],
            "capability-equivalent-non-alias",
        )

    def test_dcgm_parser_rejects_resealed_semantic_contract_drift(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        args = argparse.Namespace(
            prometheus_url="http://127.0.0.1:19090",
            attempt_id="qwen3-8b-b300--fresh-node-zero-pod--r01",
            namespace="fs2-models",
            pod_uid=["pod-uid-1"],
            node_uid=["node-uid-1"],
            gpu_uuid=["GPU-test-1"],
            expected_gpu_count=1,
            attempt_t0="2026-08-28T12:00:05Z",
            attempt_t1="2026-08-28T12:00:55Z",
            start="2026-08-28T12:00:05Z",
            end="2026-08-28T12:00:55Z",
            cadence_binding_receipt=self._dcgm_cadence_binding(Path(temporary.name)),
        )

        def query(_origin: str, expression: str, _evaluation_time: str) -> list[dict]:
            metric = expression.split("{", 1)[0]
            return [
                {
                    "metric": {
                        "__name__": metric,
                        "namespace": "fs2-models",
                        "pod": "qwen3-8b-b300-abc",
                        "pod_uid": "pod-uid-1",
                        "container": "vllm",
                        "UUID": "GPU-test-1",
                        "gpu": "0",
                    },
                    "values": [[1787918407, "10"], [1787918410, "30"]],
                }
            ]

        with mock.patch.object(DCGM, "_query_range_vector", side_effect=query):
            receipt = DCGM.build_receipt(args)
        self.assertIs(
            BASELINE._validate_dcgm_receipt(receipt, args.attempt_id), receipt
        )
        projection = BASELINE._dcgm_projection(receipt)
        self.assertEqual(projection["proxy_classification"], "NOMINAL_SCRAPE_PROXY")
        self.assertEqual(
            projection["hardware_source_timestamp_state"],
            "UNOBSERVED_HARDWARE_SOURCE_TIMESTAMP",
        )
        plan = self._plan(Path(temporary.name))
        self.assertIs(BASELINE._validate_dcgm_projection(projection, plan), projection)
        wrong_source = copy.deepcopy(projection)
        wrong_source["cadence_provenance"]["source"]["commit"] = "f" * 40
        with self.assertRaisesRegex(
            BASELINE.BaselineError, "passing_attempt_dcgm_source_mismatch"
        ):
            BASELINE._validate_dcgm_projection(wrong_source, plan)

        receipt["query"]["endpoint"] = "/api/v1/query_range"
        unsigned = dict(receipt)
        del unsigned["receipt_digest"]
        receipt["receipt_digest"] = BASELINE.canonical_digest(unsigned)
        with self.assertRaisesRegex(
            BASELINE.BaselineError, "dcgm_receipt_contract_invalid"
        ):
            BASELINE._validate_dcgm_receipt(receipt, args.attempt_id)

    def test_cpu_fallback_runtime_is_live_bound_without_aliasing_pdb70(self) -> None:
        backend = next(
            item
            for item in BASELINE.discover_backends()
            if item["resource_class"] == "cpu"
        )
        image_id = "registry/fallback@" + backend["runtime_image_digest"]
        raw = {
            "identity_relationship": "capability-equivalent-non-alias",
            "exact_pdb70_parity": False,
            "observed_runtime_identities": {
                "pod_uids": ["pod-uid-1"],
                "node_uids": ["node-uid-1"],
            },
            "runtime_identity_observation": {
                "pod": {
                    "name": "msa-search-pdb70-abc",
                    "uid": "pod-uid-1",
                    "node_name": "node-1",
                },
                "deployment_annotations": {},
                "container_image_ids": [
                    {"name": "msa-search-pdb70", "image_id": image_id}
                ],
                "pod_image_ids": [image_id],
                "runtime_argv_digest": "a" * 64,
                "runtime_environment_digest": "b" * 64,
                "node": {
                    "metadata": {
                        "name": "node-1",
                        "uid": "node-uid-1",
                        "labels": {},
                    },
                    "status": {},
                },
            },
        }
        self.assertRegex(
            BASELINE._validate_cpu_fallback_runtime_identity(raw, backend),
            r"^[0-9a-f]{64}$",
        )
        phases = BASELINE._phase_rows(
            "ready-pod-warm",
            {
                "activation-accepted": "2026-08-28T12:00:00Z",
                "semantic-call1-accepted": "2026-08-28T12:00:01Z",
                "semantic-call2-accepted": "2026-08-28T12:00:02Z",
            },
            "cpu",
        )
        by_name = {item["name"]: item for item in phases}
        self.assertEqual(by_name["queue-admission"]["state"], "not-applicable")
        self.assertEqual(by_name["cleanup"]["state"], "not-applicable")
        self.assertEqual(by_name["semantic-call-1"]["state"], "observed")

    def test_cpu_fallback_warm_attempt_uses_direct_semantic_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            plan = self._plan(directory)
            backend = next(
                item for item in plan["backends"] if item["resource_class"] == "cpu"
            )
            image_id = "registry/fallback@" + backend["runtime_image_digest"]
            runtime_identity = {
                "pod": {
                    "name": "msa-search-pdb70-abc",
                    "uid": "pod-uid-1",
                    "node_name": "node-1",
                },
                "deployment_annotations": {},
                "container_image_ids": [
                    {"name": "msa-search-pdb70", "image_id": image_id}
                ],
                "pod_image_ids": [image_id],
                "runtime_argv_digest": "a" * 64,
                "runtime_environment_digest": "b" * 64,
                "node": {
                    "metadata": {
                        "name": "node-1",
                        "uid": "node-uid-1",
                        "labels": {},
                    },
                    "status": {},
                },
            }
            raw = {
                "schema": "fs2-serve.nebius.ai/cpu-fallback-warm-acceptance/v1",
                "model_id": "msa-search-pdb70",
                "identity_relationship": "capability-equivalent-non-alias",
                "exact_pdb70_parity": False,
                "target": {
                    "namespace": "fs2-models",
                    "deployment": backend["deployment"],
                    "service": backend["service"],
                    "expected_floor": 1,
                },
                "clock": {
                    "domain": "12345678-1234-1234-1234-123456789abc",
                    "kind": "linux-monotonic",
                    "started_monotonic_ns": 1,
                },
                "phase_timestamps": {
                    "activation_accepted_at": "2026-08-28T12:00:00Z",
                    "readiness_observed_at": "2026-08-28T11:59:59Z",
                    "semantic_call1_accepted_at": "2026-08-28T12:00:01Z",
                    "semantic_call2_accepted_at": "2026-08-28T12:00:02Z",
                    "return_to_floor_accepted_at": "2026-08-28T12:00:03Z",
                },
                "phase_monotonic_ns": {
                    "activation_accepted": 1_000_000_000,
                    "readiness_observed": None,
                    "semantic_call1_accepted": 2_000_000_000,
                    "semantic_call2_accepted": 3_000_000_000,
                    "return_to_floor_accepted": 4_000_000_000,
                },
                "semantic_calls": [
                    {
                        "ordinal": ordinal,
                        "operation_id": f"direct-warm-call-{ordinal}",
                        "result_sha256": str(ordinal) * 64,
                    }
                    for ordinal in (1, 2)
                ],
                "runtime_identity_observation": runtime_identity,
                "observed_runtime_identities": {
                    "pod_uids": ["pod-uid-1"],
                    "node_uids": ["node-uid-1"],
                },
                "result": "PASS",
                "completed_at": "2026-08-28T12:00:03Z",
            }
            plan_path = directory / "plan.json"
            raw_path = directory / "cpu-warm.json"
            private_json(plan_path, plan)
            private_json(raw_path, raw)
            attempt = BASELINE.record_attempt(
                argparse.Namespace(
                    plan=plan_path,
                    backend_id=backend["backend_id"],
                    capacity_state="ready-pod-warm",
                    ordinal=1,
                    raw_acceptance=raw_path,
                    compatibility_tuple=None,
                    phase_support=[],
                    dcgm_receipt=None,
                )
            )
        self.assertEqual(attempt["result"], "PASS")
        self.assertIsNone(attempt["compatibility_tuple_digest"])
        self.assertRegex(
            attempt["runtime_identity_observation_digest"], r"^[0-9a-f]{64}$"
        )
        self.assertIsNone(attempt["dcgm"])

    def test_plan_keeps_blocked_nim_slots_outside_attempt_denominator(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            plan = self._plan(Path(temporary))
        self.assertEqual(len(plan["backends"]), 16)
        self.assertEqual(len(plan["cells"]), 64)
        counts = {
            state: sum(1 for cell in plan["cells"] if cell["admission"] == state)
            for state in ("admitted", "blocked", "not-applicable")
        }
        self.assertEqual(counts, {"admitted": 49, "blocked": 12, "not-applicable": 3})
        self.assertEqual(
            sum(
                len(cell["expected_attempt_ids"])
                for cell in plan["cells"]
                if cell["admission"] == "admitted"
            ),
            147,
        )
        self.assertEqual(
            sum(
                len(cell["blocked_planned_slots"])
                for cell in plan["cells"]
                if cell["admission"] == "blocked"
            ),
            36,
        )
        self.assertEqual(
            sum(
                len(cell["expected_attempt_ids"])
                for cell in plan["cells"]
                if cell["admission"] == "blocked"
            ),
            0,
        )
        tampered = dict(plan)
        tampered["minimum_attempts_per_admitted_cell"] = 2
        with self.assertRaises(BASELINE.BaselineError):
            BASELINE.validate_plan(tampered)

    def test_live_nim_cache_identity_admits_only_its_four_cells(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            identity = self._nim_cache_identity(directory, "openfold2")
            plan = self._plan(directory, nim_cache_identities=[identity])
        counts = {
            state: sum(1 for cell in plan["cells"] if cell["admission"] == state)
            for state in ("admitted", "blocked", "not-applicable")
        }
        self.assertEqual(counts, {"admitted": 53, "blocked": 8, "not-applicable": 3})
        self.assertEqual(
            sum(len(cell["expected_attempt_ids"]) for cell in plan["cells"]), 159
        )
        self.assertEqual(
            sum(len(cell["blocked_planned_slots"]) for cell in plan["cells"]), 24
        )
        backend = next(
            item for item in plan["backends"] if item["model_id"] == "openfold2"
        )
        self.assertEqual(backend["identity_state"], "complete-live-overlay")
        self.assertEqual(
            backend["identity_admission"]["kind"],
            "live-immutable-nim-cache-manifest",
        )

        tampered = copy.deepcopy(plan)
        cell = next(
            item
            for item in tampered["cells"]
            if item["model_id"] == "msa-search-pdb70"
            and item["capacity_state"] == "ready-pod-warm"
        )
        cell["admission"] = "admitted"
        cell["reason"] = None
        cell["expected_attempt_ids"] = [
            f"{cell['backend_id']}--{cell['capacity_state']}--r{ordinal:02d}"
            for ordinal in range(1, 4)
        ]
        cell["blocked_planned_slots"] = []
        unsigned = dict(tampered)
        del unsigned["plan_digest"]
        tampered["plan_digest"] = BASELINE.canonical_digest(unsigned)
        with self.assertRaisesRegex(
            BASELINE.BaselineError, "plan_cell_admission_invalid"
        ):
            BASELINE.validate_plan(tampered)

        identity_tuple = compatibility_tuple(backend)
        node = {
            "metadata": {
                "name": "node-1",
                "uid": "node-uid-1",
                "labels": {"nvidia.com/gpu.product": "NVIDIA-B300"},
            },
            "status": {
                "conditions": [],
                "nodeInfo": {
                    "architecture": "amd64",
                    "operatingSystem": "linux",
                    "osImage": "test",
                    "kernelVersion": identity_tuple["kernel_release"],
                    "containerRuntimeVersion": "containerd://2.1.4",
                    "kubeletVersion": "v1.35.0",
                },
                "capacity": {"nvidia.com/gpu": "1"},
                "allocatable": {"nvidia.com/gpu": "1"},
            },
        }
        identity_tuple["node_identity_digest"] = BASELINE.canonical_digest(node)
        BASELINE._validate_baseline_tuple(
            identity_tuple,
            BASELINE.load_json(BASELINE.MATRIX_PATH),
            "fresh-node-zero-pod",
            backend,
        )
        identity_tuple["artifact_bytes"] += 1
        with self.assertRaisesRegex(
            BASELINE.BaselineError, "compatibility_overlay_binding_mismatch"
        ):
            BASELINE._validate_baseline_tuple(
                identity_tuple,
                BASELINE.load_json(BASELINE.MATRIX_PATH),
                "fresh-node-zero-pod",
                backend,
            )
        identity_tuple["artifact_bytes"] -= 1
        raw = {
            "operation": {
                "runtime": {
                    "pod_uid": "pod-uid-1",
                    "node_uid": "node-uid-1",
                    "gpu_uuids": ["GPU-test-0001"],
                    "gpu_count": 1,
                }
            },
            "runtime_identity_observation": {
                "pod": {
                    "name": "openfold2-pod",
                    "uid": "pod-uid-1",
                    "node_name": "node-1",
                },
                "deployment_annotations": {
                    "fs2.nebius/runtime-image-digest": identity_tuple[
                        "runtime_image_digest"
                    ],
                    "fs2.nebius/compile-cache-abi": identity_tuple["compile_cache_abi"],
                },
                "container_image_ids": [
                    {
                        "name": "nim",
                        "image_id": "registry/runtime@"
                        + identity_tuple["runtime_image_digest"],
                    }
                ],
                "pod_image_ids": [
                    "registry/runtime@" + identity_tuple["runtime_image_digest"]
                ],
                "runtime_argv_digest": identity_tuple["runtime_argv_digest"],
                "runtime_environment_digest": identity_tuple[
                    "runtime_environment_digest"
                ],
                "node": node,
            },
        }
        self.assertRegex(
            BASELINE._validate_observed_runtime_identity(raw, identity_tuple, backend),
            r"^[0-9a-f]{64}$",
        )
        raw["operation"]["runtime"]["gpu_uuids"] = ["GPU-other"]
        with self.assertRaisesRegex(
            BASELINE.BaselineError, "raw_runtime_gpu_identity_mismatch"
        ):
            BASELINE._validate_observed_runtime_identity(raw, identity_tuple, backend)

    def test_nearest_rank_is_failure_aware_and_withholds_p95(self) -> None:
        self.assertEqual(BASELINE.nearest_rank([5.0, 7.0], 50, 3), 7.0)
        self.assertIsNone(BASELINE.nearest_rank([5.0, 7.0], 95, 3))
        self.assertEqual(BASELINE.nearest_rank(list(range(1, 21)), 95, 20), 19)

    def test_empty_aggregate_preserves_attempt_blocked_and_na_denominators(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            plan = self._plan(directory)
            plan_path = directory / "plan.json"
            attempts = directory / "attempts"
            attempts.mkdir()
            private_json(plan_path, plan)
            packet = BASELINE.aggregate(
                argparse.Namespace(plan=plan_path, attempt_dir=attempts)
            )
        self.assertEqual(packet["attempt_count"], 0)
        self.assertEqual(packet["expected_admitted_attempt_count"], 147)
        self.assertEqual(packet["blocked_cell_count"], 12)
        self.assertEqual(packet["blocked_planned_slot_count"], 36)
        self.assertEqual(packet["not_applicable_cell_count"], 3)
        self.assertEqual(len(packet["missing_attempt_ids"]), 147)
        blocked = [cell for cell in packet["cells"] if cell["admission"] == "blocked"]
        self.assertTrue(blocked)
        self.assertEqual(
            {(cell["attempted"], cell["passed"], cell["failed"]) for cell in blocked},
            {(0, 0, 0)},
        )
        self.assertEqual(packet["status"], "INCOMPLETE")

    def test_private_writer_is_exclusive_mode_0600(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "receipt.json"
            private_json(output, {"value": 1})
            self.assertEqual(os.stat(output).st_mode & 0o777, 0o600)
            with self.assertRaises(BASELINE.BaselineError):
                BASELINE.write_json_new(output.resolve(), {"value": 2})

    def test_phase_support_is_admitted_raw_bound_and_tamper_evident(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            plan = self._plan(directory)
            plan_path = directory / "plan.json"
            raw_path = directory / "provider.json"
            private_json(plan_path, plan)
            private_json(raw_path, {"provider_event": "instance-created"})
            attempt_id = next(
                cell["expected_attempt_ids"][0]
                for cell in plan["cells"]
                if cell["admission"] == "admitted"
            )
            receipt = BASELINE.build_phase_support(
                argparse.Namespace(
                    plan=plan_path,
                    attempt_id=attempt_id,
                    source_kind="provider-capacity-events",
                    raw_receipt=raw_path,
                    event=[
                        "capacity-requested=2026-08-28T12:00:00Z",
                        "provider-instance-created=2026-08-28T12:01:00Z",
                    ],
                )
            )
            self.assertEqual(
                BASELINE._validate_phase_support(receipt, attempt_id), receipt
            )
            tampered = copy.deepcopy(receipt)
            tampered["events"][1]["timestamp"] = "2026-08-28T12:02:00Z"
            with self.assertRaisesRegex(
                BASELINE.BaselineError, "phase_support_digest_mismatch"
            ):
                BASELINE._validate_phase_support(tampered, attempt_id)


if __name__ == "__main__":
    unittest.main()
