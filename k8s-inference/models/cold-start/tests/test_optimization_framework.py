from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import sys
import tempfile
import unittest
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import patch

import yaml
from jsonschema import Draft202012Validator, FormatChecker


ROOT = Path(__file__).resolve().parents[1]
FS2_ROOT = ROOT.parents[1]
SPEC = importlib.util.spec_from_file_location(
    "fs2_cold_start_framework_test", ROOT / "cold_start_framework.py"
)
assert SPEC and SPEC.loader
FRAMEWORK = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = FRAMEWORK
SPEC.loader.exec_module(FRAMEWORK)
sys.modules["cold_start_framework"] = FRAMEWORK

RUNNER_SPEC = importlib.util.spec_from_file_location(
    "fs2_cold_start_runner_test", ROOT / "run_disposable_benchmark.py"
)
assert RUNNER_SPEC and RUNNER_SPEC.loader
RUNNER = importlib.util.module_from_spec(RUNNER_SPEC)
sys.modules[RUNNER_SPEC.name] = RUNNER
RUNNER_SPEC.loader.exec_module(RUNNER)


def digest(label: str) -> str:
    return hashlib.sha256(label.encode()).hexdigest()


def referenced_json_value(reference: str) -> Any:
    relative, separator, pointer = reference.partition("#")
    if not separator or not pointer.startswith("/"):
        raise AssertionError(f"invalid JSON source reference: {reference}")
    value = json.loads((ROOT / relative).read_text(encoding="utf-8"))
    for raw_part in pointer[1:].split("/"):
        part = raw_part.replace("~1", "/").replace("~0", "~")
        value = value[int(part)] if isinstance(value, list) else value[part]
    return value


def compatibility_tuple(*, mig: bool = False, gpu_uuid: str = "GPU-test-0001") -> dict:
    return {
        "model_id": "qwen3-8b",
        "model_content_digest": digest("model"),
        "tokenizer_or_preprocessor_digest": digest("tokenizer"),
        "semantic_oracle_digest": digest("oracle"),
        "semantic_request_contract_digest": digest("requests"),
        "runtime_variant": "qwen3-8b/vllm/exact",
        "runtime_source_identity_digest": digest("runtime-source"),
        "runtime_image_digest": "sha256:" + digest("image"),
        "runtime_argv_digest": digest("argv"),
        "runtime_environment_digest": digest("environment"),
        "execution_identity_digest": digest("execution"),
        "loader_or_engine_format": "safetensors",
        "host_cpu_architecture": "amd64",
        "host_os_release_digest": digest("os-release"),
        "accelerator_pool_id": "generic-preemptible-pool",
        "accelerator_pool_receipt_digest": digest("pool"),
        "gpu_vendor": "nvidia",
        "gpu_product": "test-product",
        "gpu_chip_type": "test-chip",
        "gpu_compute_capability": "10.3",
        "gpu_memory_bytes": 309237645312,
        "workload_gpu_count": 1,
        "gpu_topology": "single-device",
        "gpu_topology_inventory_digest": digest("topology"),
        "allocated_gpu_uuids": [gpu_uuid],
        "mig_mode": "enabled" if mig else "disabled",
        "mig_profile": "1g.34gb" if mig else None,
        "driver_version": "580.173.02",
        "cuda_version": "13.0.3",
        "kernel_release": "6.8.0-test",
        "container_runtime_name": "containerd",
        "container_runtime_version": "2.1.4",
        "checkpoint_tool_digest": digest("checkpoint-tool"),
        "criu_version": "4.1",
        "artifact_manifest_digest": digest("artifact-manifest"),
        "artifact_content_digest": digest("artifact-content"),
        "artifact_bytes": 17179869184,
        "storage_class": "shared-filesystem",
        "storage_mode": "ReadWriteMany",
        "node_identity_digest": digest("node"),
        "pvc_identity_digest": digest("pvc"),
        "compile_cache_abi": "cuda13-driver580-vllm-exact",
        "capacity_state": "fresh-node-zero-pod",
    }


def matrix_bound_compatibility_tuple(
    matrix: dict[str, Any],
    model_id: str = "qwen3-8b",
    *,
    fabricated_content_digest: str | None = None,
) -> dict[str, Any]:
    value = compatibility_tuple()
    identity = matrix["deployment_identity_contract"]["models"][model_id]
    annotation = identity["annotations"].get("fs2.nebius/model-content-digest")
    content_digest = (
        annotation.removeprefix("sha256:")
        if annotation is not None
        else fabricated_content_digest or digest("fabricated-content")
    )
    value.update(
        {
            "model_id": model_id,
            "model_content_digest": content_digest,
            "artifact_content_digest": content_digest,
            "runtime_image_digest": identity["annotations"][
                "fs2.nebius/runtime-image-digest"
            ],
            "compile_cache_abi": identity["annotations"][
                "fs2.nebius/compile-cache-abi"
            ],
        }
    )
    return value


class OptimizationFrameworkTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.matrix = FRAMEWORK.load_json(ROOT / "cold-start-optimization-matrix.json")

    def test_matrix_closes_all_sixteen_catalog_models_and_priority_evidence(self) -> None:
        FRAMEWORK.validate_matrix(self.matrix)
        self.assertEqual(16, len(self.matrix["models"]))
        self.assertEqual(
            ["evo2-40b", "glm-5-2-fp8"],
            [item["model_id"] for item in self.matrix["baseline_evidence"]["observations"]],
        )
        self.assertEqual(
            [3092, 2955],
            [item["pod_created_to_ready_seconds"] for item in self.matrix["baseline_evidence"]["observations"]],
        )
        for model in self.matrix["models"]:
            self.assertEqual("active", model["capabilities"]["conventional"]["state"])
            self.assertNotEqual("active", model["capabilities"]["cuda-criu-snapshot"]["state"])
            self.assertNotEqual("active", model["capabilities"]["dynamo-snapshot"]["state"])

    def test_cosmos_conventional_baseline_is_storage_bound_and_unmeasured(self) -> None:
        cosmos = FRAMEWORK.matrix_model(self.matrix, "cosmos3-nano")
        routes = FRAMEWORK.load_json(
            FS2_ROOT / "components/control-plane/contracts/all-models-live-services.json"
        )
        identity = self.matrix["deployment_identity_contract"]["models"][
            "cosmos3-nano"
        ]
        self.assertEqual(1, cosmos["gpu_count"])
        self.assertEqual(
            "provider-block-pvc",
            routes["routes"]["cosmos3-nano"]["storage_mode"],
        )
        self.assertEqual("blocked", identity["state"])
        self.assertEqual(
            ["fs2.nebius/compile-cache-abi"], identity["missing_annotations"]
        )
        self.assertEqual("active", cosmos["capabilities"]["conventional"]["state"])
        self.assertIn(
            "no FS2 cold-start",
            cosmos["capabilities"]["conventional"]["claim"],
        )
        self.assertEqual("conventional", cosmos["next_experiment"]["mechanism"])
        self.assertNotIn(
            "cosmos3-nano",
            {
                observation["model_id"]
                for observation in self.matrix["baseline_evidence"]["observations"]
            },
        )
        for mechanism in (
            "shared-cache",
            "local-nvme",
            "oci-image-volume",
            "oci-modelcar",
            "cuda-criu-snapshot",
            "dynamo-snapshot",
        ):
            self.assertEqual("blocked", cosmos["capabilities"][mechanism]["state"])

    def test_deployment_identity_contract_matches_all_sixteen_immutable_manifests(self) -> None:
        contract = self.matrix["deployment_identity_contract"]
        required = set(contract["required_annotations"])
        matrix_models = {item["model_id"]: item for item in self.matrix["models"]}
        self.assertEqual(set(matrix_models), set(contract["models"]))
        self.assertEqual(
            "foundation.yaml#ConfigMap/fs2-cold-start-cache-contract/data/compile-cache-abi",
            contract["compile_cache_abi_source"],
        )
        foundation = [
            item
            for item in yaml.safe_load_all(
                (ROOT / "foundation.yaml").read_text(encoding="utf-8")
            )
            if item is not None
        ]
        cache_contract = next(
            item
            for item in foundation
            if item["kind"] == "ConfigMap"
            and item["metadata"]["name"] == "fs2-cold-start-cache-contract"
        )
        self.assertEqual(
            cache_contract["data"]["compile-cache-abi"],
            contract["compile_cache_abi"],
        )

        profiles = json.loads(
            (FS2_ROOT / "catalog/profiles/model-profiles.json").read_text(
                encoding="utf-8"
            )
        )
        deployments: dict[str, dict] = {}
        for relative in profiles["profiles"]["full_catalog"]["manifest_paths"]:
            for item in yaml.safe_load_all(
                (FS2_ROOT / relative).read_text(encoding="utf-8")
            ):
                if item is not None and item.get("kind") == "Deployment":
                    name = item["metadata"]["name"]
                    self.assertNotIn(name, deployments)
                    deployments[name] = item

        complete: set[str] = set()
        blocked: set[str] = set()
        for model_id, identity in contract["models"].items():
            model = matrix_models[model_id]
            deployment = deployments[model["deployment"]]
            expected = identity["annotations"]
            metadata_annotations = deployment["metadata"].get("annotations", {})
            pod_annotations = deployment["spec"]["template"]["metadata"].get(
                "annotations", {}
            )
            self.assertEqual(
                expected,
                {
                    key: value
                    for key, value in metadata_annotations.items()
                    if key in required
                },
                model_id,
            )
            self.assertEqual(
                expected,
                {key: value for key, value in pod_annotations.items() if key in required},
                model_id,
            )
            containers = {
                item["name"]: item["image"]
                for item in deployment["spec"]["template"]["spec"]["containers"]
            }
            for container_name in model["primary_containers"]:
                self.assertIn(
                    "@" + expected["fs2.nebius/runtime-image-digest"],
                    containers[container_name],
                    (model_id, container_name),
                )

            if identity["state"] == "complete":
                complete.add(model_id)
                self.assertEqual(required, set(expected))
                self.assertEqual(
                    contract["compile_cache_abi"],
                    expected["fs2.nebius/compile-cache-abi"],
                    model_id,
                )
                self.assertEqual([], identity["missing_annotations"])
                source_digest = referenced_json_value(identity["model_content_source"])
                self.assertEqual(
                    "sha256:" + source_digest,
                    expected["fs2.nebius/model-content-digest"],
                    model_id,
                )
                self.assertIsNone(identity["blocker"])
                self.assertIsNone(identity["blocker_source"])
            else:
                blocked.add(model_id)
                if model_id == "cosmos3-nano":
                    self.assertEqual(
                        ["fs2.nebius/compile-cache-abi"],
                        identity["missing_annotations"],
                    )
                    self.assertIn("fs2.nebius/model-content-digest", expected)
                    self.assertNotIn("fs2.nebius/compile-cache-abi", expected)
                    self.assertEqual(
                        "gpu-family-specific-compile-cache-abi-unbound",
                        identity["blocker"],
                    )
                    self.assertEqual(
                        "../general-media/k8s/cosmos3-nano.yaml",
                        identity["blocker_source"],
                    )
                else:
                    self.assertEqual(
                        ["fs2.nebius/model-content-digest"],
                        identity["missing_annotations"],
                    )
                    self.assertNotIn("fs2.nebius/model-content-digest", expected)
                    blocker = referenced_json_value(identity["blocker_source"])
                    self.assertEqual("unresolved", blocker["state"], model_id)
                    self.assertIsNone(blocker["manifest_digest"], model_id)
                    self.assertIsInstance(identity["blocker"], str)

        self.assertEqual(12, len(complete))
        self.assertEqual(
            {"cosmos3-nano", "msa-search-pdb70", "openfold2", "openfold3"},
            blocked,
        )

    def test_identity_and_runtime_marker_partitions_fail_closed(self) -> None:
        marker_contract = self.matrix["runtime_marker_contract"]
        self.assertEqual("denied", marker_contract["default"])
        self.assertEqual(
            {"evo2-40b"}, set(marker_contract["source_instrumented_models"])
        )
        self.assertEqual(
            {item["model_id"] for item in self.matrix["models"]} - {"evo2-40b"},
            set(marker_contract["unqualified_models"]),
        )
        for model_id in marker_contract["unqualified_models"]:
            with self.assertRaisesRegex(
                FRAMEWORK.ColdStartContractError,
                "runtime_markers_unqualified:" + model_id,
            ):
                FRAMEWORK.build_phase_observation(
                    self.matrix,
                    model_id=model_id,
                    mechanism="conventional",
                    pod={},
                    events=[],
                    node={},
                    external_events={},
                    runtime_markers={"weight-load-start": "2026-08-28T12:00:00Z"},
                )
        with self.assertRaisesRegex(
            FRAMEWORK.ColdStartContractError, "runtime_marker_name_unreviewed"
        ):
            FRAMEWORK.build_phase_observation(
                self.matrix,
                model_id="evo2-40b",
                mechanism="conventional",
                pod={},
                events=[],
                node={},
                external_events={},
                runtime_markers={"third-party-ready": "2026-08-28T12:00:00Z"},
            )

        invalid_identity = deepcopy(self.matrix)
        invalid_identity["deployment_identity_contract"]["models"]["boltz2"][
            "model_content_source"
        ] = None
        with self.assertRaisesRegex(
            FRAMEWORK.ColdStartContractError, "matrix_complete_identity_invalid"
        ):
            FRAMEWORK.validate_matrix(invalid_identity)
        invalid_markers = deepcopy(self.matrix)
        invalid_markers["runtime_marker_contract"]["unqualified_models"].append(
            "evo2-40b"
        )
        with self.assertRaisesRegex(
            FRAMEWORK.ColdStartContractError,
            "matrix_runtime_marker_partition_invalid",
        ):
            FRAMEWORK.validate_matrix(invalid_markers)

    def test_model_content_sources_reject_pointer_and_path_failures(self) -> None:
        cases = {
            "matrix_json_pointer_invalid": (
                "../bionemo/boltz2/artifact-manifest.json#/content/~2digest"
            ),
            "matrix_json_pointer_not_found": (
                "../bionemo/boltz2/artifact-manifest.json#/content/does-not-exist"
            ),
            "matrix_json_pointer_non_scalar": (
                "../bionemo/boltz2/artifact-manifest.json#/content"
            ),
            "matrix_source_path_outside_fs2": (
                "../../../../../../etc/passwd#/content/digest"
            ),
            "matrix_model_content_source_digest_invalid": (
                "../bionemo/boltz2/artifact-manifest.json#/content/expanded_bytes"
            ),
        }
        for error_code, source in cases.items():
            with self.subTest(error_code=error_code):
                mutated = deepcopy(self.matrix)
                mutated["deployment_identity_contract"]["models"]["boltz2"][
                    "model_content_source"
                ] = source
                with self.assertRaisesRegex(
                    FRAMEWORK.ColdStartContractError,
                    "^" + error_code + "$",
                ):
                    FRAMEWORK.validate_matrix(mutated)

    def test_model_content_and_compile_cache_sources_are_exactly_bound(self) -> None:
        changed_digest = deepcopy(self.matrix)
        changed_digest["deployment_identity_contract"]["models"]["boltz2"][
            "annotations"
        ]["fs2.nebius/model-content-digest"] = "sha256:" + digest(
            "changed-content"
        )
        with self.assertRaisesRegex(
            FRAMEWORK.ColdStartContractError,
            "^matrix_model_content_digest_mismatch$",
        ):
            FRAMEWORK.validate_matrix(changed_digest)

        changed_compile_source = deepcopy(self.matrix)
        changed_compile_source["deployment_identity_contract"][
            "compile_cache_abi_source"
        ] = (
            "foundation.yaml#ConfigMap/fs2-cold-start-cache-contract/"
            "data/runtime-image-policy"
        )
        with self.assertRaisesRegex(
            FRAMEWORK.ColdStartContractError,
            "^matrix_compile_cache_abi_source_mismatch$",
        ):
            FRAMEWORK.validate_matrix(changed_compile_source)

    def test_complete_identity_binds_every_content_and_runtime_tuple_field(self) -> None:
        identity_tuple = matrix_bound_compatibility_tuple(self.matrix)
        self.assertRegex(
            FRAMEWORK.validate_deployment_identity_binding(
                self.matrix,
                model_id="qwen3-8b",
                compatibility_tuple=identity_tuple,
            ),
            r"^[0-9a-f]{64}$",
        )
        mutations = {
            "model_content_digest": "deployment_identity_model_content_mismatch",
            "artifact_content_digest": "deployment_identity_artifact_content_mismatch",
            "runtime_image_digest": "deployment_identity_runtime_image_mismatch",
            "compile_cache_abi": "deployment_identity_compile_cache_abi_mismatch",
        }
        for field, error_code in mutations.items():
            with self.subTest(field=field):
                mutated = deepcopy(identity_tuple)
                mutated[field] = (
                    "sha256:" + digest("wrong-runtime")
                    if field == "runtime_image_digest"
                    else digest("wrong-content")
                    if field.endswith("content_digest")
                    else "wrong-compile-cache-abi"
                )
                with self.assertRaisesRegex(
                    FRAMEWORK.ColdStartContractError,
                    "^" + error_code + "$",
                ):
                    FRAMEWORK.validate_deployment_identity_binding(
                        self.matrix,
                        model_id="qwen3-8b",
                        compatibility_tuple=mutated,
                    )

    def test_fabricated_blocked_identities_fail_every_runner_gate(self) -> None:
        blocked_ids = ("msa-search-pdb70", "openfold2", "openfold3")
        for model_id in blocked_ids:
            identity_tuple = matrix_bound_compatibility_tuple(
                self.matrix,
                model_id,
                fabricated_content_digest=digest("fabricated-" + model_id),
            )
            fabricated_observation = {
                "deployment_annotations": {
                    "fs2.nebius/model-content-digest": "sha256:"
                    + identity_tuple["model_content_digest"],
                    "fs2.nebius/runtime-image-digest": identity_tuple[
                        "runtime_image_digest"
                    ],
                    "fs2.nebius/compile-cache-abi": identity_tuple[
                        "compile_cache_abi"
                    ],
                },
                "pod_image_ids": [
                    "registry.invalid/runtime@"
                    + identity_tuple["runtime_image_digest"]
                ],
                "runtime_argv_digest": identity_tuple["runtime_argv_digest"],
                "runtime_environment_digest": identity_tuple[
                    "runtime_environment_digest"
                ],
                "node": {
                    "metadata": {"uid": "node-test"},
                    "status": {
                        "nodeInfo": {
                            "kernelVersion": identity_tuple["kernel_release"],
                            "containerRuntimeVersion": "containerd://2.1.4",
                        }
                    },
                },
            }
            model = dict(FRAMEWORK.matrix_model(self.matrix, model_id))
            for arm, mechanism in (
                ("control", "conventional"),
                ("candidate", "shared-cache"),
            ):
                with self.subTest(model_id=model_id, arm=arm):
                    args = argparse.Namespace(
                        arm=arm,
                        mechanism=mechanism,
                        model_id=model_id,
                    )
                    with self.assertRaisesRegex(
                        FRAMEWORK.ColdStartContractError,
                        "^deployment_identity_not_complete$",
                    ):
                        RUNNER._validate_mechanism(
                            args,
                            model,
                            identity_tuple,
                            matrix=self.matrix,
                        )
            with self.subTest(model_id=model_id, gate="observed-identity"):
                with self.assertRaisesRegex(
                    FRAMEWORK.ColdStartContractError,
                    "^deployment_identity_not_complete$",
                ):
                    RUNNER._validate_observed_identity(
                        identity_tuple,
                        fabricated_observation,
                        matrix=self.matrix,
                    )

        qwen_tuple = matrix_bound_compatibility_tuple(self.matrix)
        with self.assertRaisesRegex(
            FRAMEWORK.ColdStartContractError,
            "^deployment_identity_matrix_required$",
        ):
            RUNNER._validate_mechanism(
                argparse.Namespace(
                    arm="control",
                    mechanism="conventional",
                    model_id="qwen3-8b",
                ),
                dict(FRAMEWORK.matrix_model(self.matrix, "qwen3-8b")),
                qwen_tuple,
            )

    def test_evo_phase_observation_separates_artifact_weight_compile_and_calls(self) -> None:
        pod = {
            "metadata": {"uid": "pod-evo", "creationTimestamp": "2026-08-28T12:00:00Z"},
            "status": {
                "conditions": [
                    {"type": "PodScheduled", "status": "True", "lastTransitionTime": "2026-08-28T12:00:10Z"},
                    {"type": "Ready", "status": "True", "lastTransitionTime": "2026-08-28T12:10:00Z"},
                ],
                "initContainerStatuses": [
                    {"name": "prepare-runtime-cache", "state": {"terminated": {"startedAt": "2026-08-28T12:01:00Z", "finishedAt": "2026-08-28T12:01:01Z"}}},
                    {"name": "materialize-checkpoint", "state": {"terminated": {"startedAt": "2026-08-28T12:01:02Z", "finishedAt": "2026-08-28T12:08:00Z"}}},
                ],
                "containerStatuses": [
                    {"name": "model", "state": {"running": {"startedAt": "2026-08-28T12:08:01Z"}}},
                    {"name": "relay", "state": {"running": {"startedAt": "2026-08-28T12:08:02Z"}}},
                ],
            },
        }
        node = {
            "metadata": {"uid": "node-evo"},
            "status": {"conditions": [{"type": "Ready", "status": "True", "lastTransitionTime": "2026-08-28T11:59:50Z"}]},
        }
        events = [
            {"reason": "Pulling", "eventTime": "2026-08-28T12:00:11Z"},
            {"reason": "Pulled", "eventTime": "2026-08-28T12:00:59Z"},
        ]
        external = {
            "activation-accepted": "2026-08-28T11:59:40Z",
            "semantic-call1-accepted": "2026-08-28T12:10:10Z",
            "semantic-call2-accepted": "2026-08-28T12:10:12Z",
            "return-to-zero-accepted": "2026-08-28T12:11:00Z",
        }
        markers = {
            "weight-load-start": "2026-08-28T12:08:03Z",
            "weight-load-end": "2026-08-28T12:09:30Z",
            "engine-build-or-compile-start": "2026-08-28T12:09:31Z",
            "engine-build-or-compile-end": "2026-08-28T12:09:59Z",
        }
        observation = FRAMEWORK.build_phase_observation(
            self.matrix,
            model_id="evo2-40b",
            mechanism="conventional",
            pod=pod,
            events=events,
            node=node,
            external_events=external,
            runtime_markers=markers,
        )
        self.assertTrue(observation["complete_for_promotion"])
        self.assertEqual([], observation["missing_required_events"])
        self.assertEqual(
            FRAMEWORK.CANONICAL_EVENTS,
            tuple(item["name"] for item in observation["events"]),
        )
        schema = FRAMEWORK.load_json(ROOT / "startup-phase-observation.schema.json")
        self.assertEqual(
            [],
            list(
                Draft202012Validator(
                    schema, format_checker=FormatChecker()
                ).iter_errors(observation)
            ),
        )
        invalid_external = dict(external)
        invalid_external["semantic-call2-accepted"] = "2026-08-28T12:10:09Z"
        with self.assertRaisesRegex(
            FRAMEWORK.ColdStartContractError,
            "phase_observation_event_order_invalid",
        ):
            FRAMEWORK.build_phase_observation(
                self.matrix,
                model_id="evo2-40b",
                mechanism="conventional",
                pod=pod,
                events=events,
                node=node,
                external_events=invalid_external,
                runtime_markers=markers,
            )

    def test_missing_vllm_weight_and_compile_markers_fail_closed(self) -> None:
        pod = {
            "metadata": {"uid": "pod-glm"},
            "status": {
                "conditions": [
                    {"type": "PodScheduled", "status": "True", "lastTransitionTime": "2026-08-28T12:00:00Z"},
                    {"type": "Ready", "status": "True", "lastTransitionTime": "2026-08-28T12:30:00Z"},
                ],
                "initContainerStatuses": [
                    {"name": "prepare-runtime-cache", "state": {"terminated": {"startedAt": "2026-08-28T12:00:05Z", "finishedAt": "2026-08-28T12:00:06Z"}}}
                ],
                "containerStatuses": [
                    {"name": "vllm", "state": {"running": {"startedAt": "2026-08-28T12:00:07Z"}}}
                ],
            },
        }
        node = {"metadata": {"uid": "node-glm"}, "status": {"conditions": [{"type": "Ready", "status": "True", "lastTransitionTime": "2026-08-28T11:59:00Z"}]}}
        observation = FRAMEWORK.build_phase_observation(
            self.matrix,
            model_id="glm-5-2-fp8",
            mechanism="conventional",
            pod=pod,
            events=[{"reason": "Pulled", "eventTime": "2026-08-28T12:00:04Z"}],
            node=node,
            external_events={
                "activation-accepted": "2026-08-28T11:58:00Z",
                "semantic-call1-accepted": "2026-08-28T12:30:10Z",
                "semantic-call2-accepted": "2026-08-28T12:30:12Z",
                "return-to-zero-accepted": "2026-08-28T12:31:00Z",
            },
            runtime_markers={},
        )
        self.assertFalse(observation["complete_for_promotion"])
        self.assertEqual(
            {
                "weight-load-start",
                "weight-load-end",
                "engine-build-or-compile-start",
                "engine-build-or-compile-end",
            },
            set(observation["missing_required_events"]),
        )

    def test_snapshot_gate_denies_missing_qualification_and_cross_partition(self) -> None:
        donor = compatibility_tuple()
        target = compatibility_tuple(gpu_uuid="GPU-test-0002")
        denied = FRAMEWORK.evaluate_snapshot_eligibility(
            self.matrix,
            model_id="qwen3-8b",
            mechanism="cuda-criu-snapshot",
            donor=donor,
            target=target,
            qualification=None,
            now=datetime(2026, 8, 28, 15, 0, tzinfo=UTC),
        )
        self.assertFalse(denied["eligible_for_isolated_experiment"])
        self.assertIn("qualification_receipt_missing", denied["reason_codes"])

        cross_partition = compatibility_tuple(mig=True, gpu_uuid="GPU-test-0002")
        cross = FRAMEWORK.evaluate_snapshot_eligibility(
            self.matrix,
            model_id="qwen3-8b",
            mechanism="cuda-criu-snapshot",
            donor=donor,
            target=cross_partition,
            qualification=None,
            now=datetime(2026, 8, 28, 15, 0, tzinfo=UTC),
        )
        self.assertFalse(cross["eligible_for_isolated_experiment"])
        self.assertIn("cross_partition_restore_denied", cross["reason_codes"])

    def test_full_gpu_and_mig_use_distinct_exact_resource_receipts(self) -> None:
        now = datetime(2026, 8, 28, 15, 0, tzinfo=UTC)
        donor = compatibility_tuple()
        target = compatibility_tuple(gpu_uuid="GPU-test-0002")
        full_qualification = {
            "schema": "fs2-serve.nebius.ai/snapshot-experiment-qualification/v1",
            "status": "PASS",
            "mechanism": "cuda-criu-snapshot",
            "model_id": "qwen3-8b",
            "qualification_scope": "full-gpu",
            "donor_tuple_digest": FRAMEWORK.canonical_digest(donor),
            "target_tuple_digest": FRAMEWORK.canonical_digest(target),
            "resource_identity": {
                "resource_name": "nvidia.com/gpu",
                "donor_device_uuids": donor["allocated_gpu_uuids"],
                "target_device_uuids": target["allocated_gpu_uuids"],
                "gpu_topology_inventory_digest": target["gpu_topology_inventory_digest"],
                "donor_node_identity_digest": donor["node_identity_digest"],
                "target_node_identity_digest": target["node_identity_digest"],
                "donor_pvc_identity_digest": donor["pvc_identity_digest"],
                "target_pvc_identity_digest": target["pvc_identity_digest"],
            },
            "semantic_equivalence_passed": True,
            "conventional_fallback_passed": True,
            "observed_at": "2026-08-28T14:00:00Z",
            "valid_until": "2026-08-29T14:00:00Z",
        }
        full = FRAMEWORK.evaluate_snapshot_eligibility(
            self.matrix,
            model_id="qwen3-8b",
            mechanism="cuda-criu-snapshot",
            donor=donor,
            target=target,
            qualification=full_qualification,
            now=now,
        )
        self.assertTrue(full["eligible_for_isolated_experiment"])
        self.assertEqual("denied", full["production_promotion"])

        mig_donor = compatibility_tuple(mig=True)
        mig_target = compatibility_tuple(mig=True, gpu_uuid="GPU-test-0002")
        mig_qualification = {
            **full_qualification,
            "qualification_scope": "mig",
            "donor_tuple_digest": FRAMEWORK.canonical_digest(mig_donor),
            "target_tuple_digest": FRAMEWORK.canonical_digest(mig_target),
            "resource_identity": {
                "discovered_extended_resource_name": "nvidia.com/mig-1g.34gb",
                "mig_profile": "1g.34gb",
                "donor_mig_device_uuids": ["MIG-donor"],
                "target_mig_device_uuids": ["MIG-target"],
                "donor_gpu_instance_ids": ["gi-1"],
                "target_gpu_instance_ids": ["gi-2"],
                "donor_compute_instance_ids": ["ci-1"],
                "target_compute_instance_ids": ["ci-2"],
                "donor_node_identity_digest": mig_donor["node_identity_digest"],
                "target_node_identity_digest": mig_target["node_identity_digest"],
                "donor_pvc_identity_digest": mig_donor["pvc_identity_digest"],
                "target_pvc_identity_digest": mig_target["pvc_identity_digest"],
            },
        }
        mig_result = FRAMEWORK.evaluate_snapshot_eligibility(
            self.matrix,
            model_id="qwen3-8b",
            mechanism="cuda-criu-snapshot",
            donor=mig_donor,
            target=mig_target,
            qualification=mig_qualification,
            now=now,
        )
        self.assertTrue(mig_result["eligible_for_isolated_experiment"])
        self.assertEqual("mig", mig_result["partition"])

        wrong_mig_resource = deepcopy(mig_qualification)
        wrong_mig_resource["resource_identity"][
            "discovered_extended_resource_name"
        ] = "nvidia.com/gpu"
        denied_mig = FRAMEWORK.evaluate_snapshot_eligibility(
            self.matrix,
            model_id="qwen3-8b",
            mechanism="cuda-criu-snapshot",
            donor=mig_donor,
            target=mig_target,
            qualification=wrong_mig_resource,
            now=now,
        )
        self.assertFalse(denied_mig["eligible_for_isolated_experiment"])
        self.assertIn("mig_resource_name_invalid", denied_mig["reason_codes"])

    def test_evo_runtime_emits_exact_startup_markers(self) -> None:
        stage = (ROOT.parent / "general-media/evo2_stage.py").read_text(encoding="utf-8")
        serve = (ROOT.parent / "general-media/evo2_serve.py").read_text(encoding="utf-8")
        for name in ("artifact-localization-start", "artifact-localization-verified"):
            self.assertIn(name, stage)
        for name in (
            "weight-load-start",
            "weight-load-end",
            "engine-build-or-compile-start",
            "engine-build-or-compile-end",
        ):
            self.assertIn(name, serve)

    def test_live_identity_must_match_runtime_image_argv_environment_and_node(self) -> None:
        identity_tuple = matrix_bound_compatibility_tuple(self.matrix)
        observation = {
            "deployment_annotations": {
                "fs2.nebius/model-content-digest": "sha256:"
                + identity_tuple["model_content_digest"],
                "fs2.nebius/runtime-image-digest": identity_tuple[
                    "runtime_image_digest"
                ],
                "fs2.nebius/compile-cache-abi": identity_tuple["compile_cache_abi"],
            },
            "pod_image_ids": [
                "registry.example/runtime@" + identity_tuple["runtime_image_digest"]
            ],
            "runtime_argv_digest": identity_tuple["runtime_argv_digest"],
            "runtime_environment_digest": identity_tuple[
                "runtime_environment_digest"
            ],
            "node": {
                "metadata": {"uid": "node-test"},
                "status": {
                    "nodeInfo": {
                        "kernelVersion": identity_tuple["kernel_release"],
                        "containerRuntimeVersion": "containerd://2.1.4",
                    }
                },
            },
        }
        self.assertRegex(
            RUNNER._validate_observed_identity(
                identity_tuple,
                observation,
                matrix=self.matrix,
            ),
            r"^[0-9a-f]{64}$",
        )
        observation["runtime_environment_digest"] = digest("wrong-environment")
        with self.assertRaisesRegex(
            FRAMEWORK.ColdStartContractError, "observed_runtime_environment_mismatch"
        ):
            RUNNER._validate_observed_identity(
                identity_tuple,
                observation,
                matrix=self.matrix,
            )

    def test_runner_denies_retained_and_prohibited_clusters_before_execution(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            run_root = Path(temporary)
            kubeconfig = run_root / "kubeconfig"
            kubeconfig.write_text("apiVersion: v1\n", encoding="utf-8")
            os.chmod(kubeconfig, 0o600)
            for cluster_id in sorted(FRAMEWORK.PROTECTED_CLUSTER_IDS):
                args = argparse.Namespace(
                    run_root=run_root,
                    kubeconfig=kubeconfig,
                    run_id="r123456",
                    context="fs2-disposable-r123456",
                    cluster_id=cluster_id,
                    source_commit="0" * 40,
                    experiment_id="qwen-control-r1",
                    attempt_ordinal=1,
                    arm="control",
                    previous_attempt_digest=None,
                )
                with self.assertRaisesRegex(
                    FRAMEWORK.ColdStartContractError, "protected_cluster_denied"
                ):
                    RUNNER._validate_target(args)

    def test_attempt_chain_reopens_exact_predecessor_and_never_replaces_output(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            run_root = Path(temporary)
            os.chmod(run_root, 0o700)
            output_dir = run_root / "cold-start-benchmark/qwen-control-r1"
            output_dir.mkdir(parents=True, mode=0o700)
            previous_path = output_dir / "attempt-001.json"
            previous = {
                "schema": "fs2-serve.nebius.ai/terraform-cold-start-attempt/v1",
                "experiment_id": "qwen-control-r1",
                "attempt_ordinal": 1,
                "arm": "control",
                "source_commit": "1" * 40,
                "cluster_id": "mk8scluster-review1",
                "run_id": "r123456",
                "model_id": "qwen3-8b",
                "result": "PASS",
            }
            previous["receipt_digest"] = FRAMEWORK.canonical_digest(previous)
            previous_path.write_text(json.dumps(previous), encoding="utf-8")
            os.chmod(previous_path, 0o600)
            args = argparse.Namespace(
                run_root=run_root,
                output=output_dir / "attempt-002.json",
                experiment_id="qwen-control-r1",
                attempt_ordinal=2,
                previous_attempt_digest=previous["receipt_digest"],
                source_commit="1" * 40,
                cluster_id="mk8scluster-review1",
                run_id="r123456",
                model_id="qwen3-8b",
            )
            RUNNER._validate_attempt_chain(args)

            wrong_digest = deepcopy(args)
            wrong_digest.previous_attempt_digest = digest("wrong-predecessor")
            with self.assertRaisesRegex(
                FRAMEWORK.ColdStartContractError, "previous_attempt_digest_mismatch"
            ):
                RUNNER._validate_attempt_chain(wrong_digest)

            RUNNER._write_attempt_receipt(args.output, {"result": "PASS"})
            with self.assertRaisesRegex(
                FRAMEWORK.ColdStartContractError, "attempt_receipt_already_exists"
            ):
                RUNNER._write_attempt_receipt(args.output, {"result": "FAIL"})
            self.assertEqual(
                {"result": "PASS"},
                json.loads(args.output.read_text(encoding="utf-8")),
            )

    def test_closed_promotion_reuses_spike_validator(self) -> None:
        import test_contract  # type: ignore[import-not-found]

        receipt = test_contract.benchmark_receipt()
        FRAMEWORK.validate_closed_promotion_receipt(receipt)
        invalid = deepcopy(receipt)
        invalid["decision"]["accepted"] = False
        with self.assertRaisesRegex(
            FRAMEWORK.ColdStartContractError, "promotion_receipt_invalid"
        ):
            FRAMEWORK.validate_closed_promotion_receipt(invalid)

        with tempfile.TemporaryDirectory() as temporary:
            receipt_path = Path(temporary) / "promotion.json"
            receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
            with patch.object(
                sys,
                "argv",
                [
                    "cold_start_framework.py",
                    "validate-promotion",
                    "--receipt",
                    str(receipt_path),
                ],
            ):
                self.assertEqual(0, FRAMEWORK._main())


if __name__ == "__main__":
    unittest.main()
