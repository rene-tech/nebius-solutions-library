from __future__ import annotations

import json
import hashlib
import math
import re
import statistics
import sys
import unittest
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import yaml
from jsonschema import Draft202012Validator


COLD_START_ROOT = Path(__file__).resolve().parents[1]
FS2_ROOT = COLD_START_ROOT.parents[1]
CATALOG_ROOT = FS2_ROOT / "catalog/runtime"
sys.path.insert(0, str(COLD_START_ROOT))

from validate_post_acceptance_receipt import (  # noqa: E402
    ReceiptValidationError,
    validate_receipt,
)
IMAGE_PATTERN = re.compile(r"^[^\s:@]+(?:[:][^\s@]+)?@sha256:[0-9a-f]{64}$")
DIGEST_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")


def digest(seed: str) -> str:
    return hashlib.sha256(seed.encode()).hexdigest()


def canonical_digest(value: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=True
        ).encode("ascii")
    ).hexdigest()


def seal_receipt(receipt: dict[str, Any]) -> None:
    unsigned = dict(receipt)
    unsigned.pop("receipt_digest")
    receipt["receipt_digest"] = canonical_digest(unsigned)


def benchmark_receipt() -> dict[str, Any]:
    event_names = [
        "activation-accepted",
        "capacity-requested",
        "provider-instance-created",
        "node-ready",
        "workload-admitted",
        "pod-scheduled",
        "storage-attached",
        "image-or-image-volume-pull-start",
        "image-or-image-volume-pull-end",
        "artifact-localization-start",
        "artifact-localization-verified",
        "runtime-process-start",
        "weight-load-start",
        "weight-load-end",
        "engine-build-or-compile-start",
        "engine-build-or-compile-end",
        "checkpoint-restore-start",
        "checkpoint-restore-end",
        "readiness-accepted",
        "semantic-call1-accepted",
        "semantic-call2-accepted",
        "return-to-zero-accepted",
    ]
    attempts: list[dict[str, Any]] = []
    base = datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc)
    for pair in range(20):
        for arm, call1_base in (("control", 20.0), ("candidate", 10.0)):
            attempt_number = len(attempts)
            t0 = base + timedelta(minutes=attempt_number * 2)
            call1_seconds = call1_base + pair / 10
            call2_seconds = call1_seconds + 2
            call1 = t0 + timedelta(seconds=call1_seconds)
            call2 = t0 + timedelta(seconds=call2_seconds)
            deadline = t0 + timedelta(seconds=60)
            timestamps = {
                "activation-accepted": t0,
                "capacity-requested": t0 + timedelta(seconds=0.1),
                "provider-instance-created": t0 + timedelta(seconds=0.5),
                "node-ready": t0 + timedelta(seconds=1),
                "pod-scheduled": t0 + timedelta(seconds=2),
                "runtime-process-start": t0 + timedelta(seconds=3),
                "readiness-accepted": call1 - timedelta(seconds=0.5),
                "semantic-call1-accepted": call1,
                "semantic-call2-accepted": call2,
                "return-to-zero-accepted": call2 + timedelta(seconds=1),
            }
            attempts.append(
                {
                    "attempt_id": f"attempt-{attempt_number:02d}",
                    "arm": arm,
                    "capacity_state": "fresh-node-zero-pod",
                    "existing_cohort": "new-node",
                    "status": "PASS",
                    "failure_code": None,
                    "t0_utc": t0.isoformat().replace("+00:00", "Z"),
                    "deadline_utc": deadline.isoformat().replace("+00:00", "Z"),
                    "call1_utc": call1.isoformat().replace("+00:00", "Z"),
                    "call2_utc": call2.isoformat().replace("+00:00", "Z"),
                    "t0_to_call1_seconds": call1_seconds,
                    "t0_to_call2_seconds": call2_seconds,
                    "events": [
                        {
                            "name": name,
                            "state": "observed" if name in timestamps else "not-applicable",
                            "timestamp": (
                                timestamps[name].isoformat().replace("+00:00", "Z")
                                if name in timestamps
                                else None
                            ),
                        }
                        for name in event_names
                    ],
                    "semantic_receipt_digest": digest(f"semantic-{attempt_number}"),
                    "cleanup_receipt_digest": digest(f"cleanup-{attempt_number}"),
                    "raw_attempt_receipt_digest": digest(f"raw-{attempt_number}"),
                }
            )

    def aggregate(arm: str) -> dict[str, Any]:
        items = [item for item in attempts if item["arm"] == arm]
        call1 = sorted(float(item["t0_to_call1_seconds"]) for item in items)
        call2 = sorted(float(item["t0_to_call2_seconds"]) for item in items)

        def percentile(values: list[float], quantile: float) -> float:
            return values[math.ceil(quantile * len(items)) - 1]

        def mad(values: list[float]) -> float:
            median = statistics.median(values)
            return float(statistics.median(abs(item - median) for item in values))

        return {
            "attempt_count": len(items),
            "success_count": len(items),
            "failure_count": 0,
            "failure_ranked_p50_t0_to_call1_seconds": percentile(call1, 0.50),
            "failure_ranked_p95_t0_to_call1_seconds": percentile(call1, 0.95),
            "failure_ranked_p50_t0_to_call2_seconds": percentile(call2, 0.50),
            "failure_ranked_p95_t0_to_call2_seconds": percentile(call2, 0.95),
            "call1_median_absolute_deviation_seconds": mad(call1),
            "call2_median_absolute_deviation_seconds": mad(call2),
            "artifact_bytes": 17179869184,
            "peak_gpu_memory_bytes": 8589934592,
            "peak_host_memory_bytes": 34359738368,
            "preparation_seconds": 120.0,
            "restore_seconds": 8.0 if arm == "candidate" else None,
        }

    receipt = {
        "schema": "fs2-serve.nebius.ai/post-acceptance-cold-start-benchmark-receipt/v1alpha1",
        "receipt_digest": digest("receipt"),
        "status": "PASS",
        "benchmark_contract_digest": canonical_digest(
            json.loads(
                (COLD_START_ROOT / "post-acceptance-benchmark-contract.json").read_text(
                    encoding="utf-8"
                )
            )
        ),
        "existing_evidence": {
            "base_schema": "fs2-serve.nebius.ai/qualification-cohort/v4",
            "prepared_node_qualification_receipt_digest": digest("prepared"),
            "new_node_qualification_receipt_digest": digest("new"),
            "preemption_receipt_digest": digest("preemption"),
            "return_to_zero_receipt_digest": digest("return"),
        },
        "compatibility_tuple": {
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
            "accelerator_pool_id": "nebius-b300-preemptible-1x",
            "accelerator_pool_receipt_digest": digest("pool"),
            "gpu_vendor": "nvidia",
            "gpu_product": "B300 SXM6 288GB",
            "gpu_chip_type": "GB300-B300",
            "gpu_compute_capability": "10.3",
            "gpu_memory_bytes": 309237645312,
            "workload_gpu_count": 1,
            "gpu_topology": "single-gpu",
            "gpu_topology_inventory_digest": digest("topology"),
            "allocated_gpu_uuids": ["GPU-test-0001"],
            "mig_mode": "disabled",
            "mig_profile": None,
            "driver_version": "580.95.05",
            "cuda_version": "13.0",
            "kernel_release": "6.8.0-test",
            "container_runtime_name": "containerd",
            "container_runtime_version": "2.1.4",
            "checkpoint_tool_digest": digest("cuda-checkpoint"),
            "criu_version": "4.1",
            "artifact_manifest_digest": digest("artifact"),
            "artifact_content_digest": digest("artifact-content"),
            "artifact_bytes": 17179869184,
            "storage_class": "shared-filesystem",
            "storage_mode": "ReadWriteMany",
            "node_identity_digest": digest("node"),
            "pvc_identity_digest": digest("pvc"),
            "compile_cache_abi": "cuda13-driver580-vllm-exact",
            "capacity_state": "fresh-node-zero-pod",
        },
        "control": {
            "mechanism": "conventional",
            "runtime_tuple_receipt_digest": digest("control-runtime"),
            "artifact_manifest_digest": digest("control-artifact"),
        },
        "candidate": {
            "mechanism": "dynamo-snapshot",
            "runtime_tuple_receipt_digest": digest("candidate-runtime"),
            "artifact_manifest_digest": digest("candidate-artifact"),
        },
        "statistics_policy": {
            "attempt_deadline_seconds": 60,
            "minimum_attempts_per_arm": 20,
            "attempt_order": "strict-control-candidate-alternation",
            "percentile_estimator": "nearest-rank",
            "failure_ordering": "failures-rank-after-all-successful-durations",
            "p95_minimum_attempts": 20,
            "dispersion_statistic": "median-absolute-deviation",
            "minimum_absolute_p95_improvement_seconds": 5,
            "minimum_relative_p95_improvement_fraction": 0.20,
            "minimum_effect_rule": "meet-both-absolute-and-relative-thresholds",
        },
        "attempts": attempts,
        "aggregates": {"control": aggregate("control"), "candidate": aggregate("candidate")},
        "decision": {
            "semantic_equivalence_passed": True,
            "failure_rate_non_regression_passed": True,
            "absolute_effect_passed": True,
            "relative_effect_passed": True,
            "call2_latency_non_regression_passed": True,
            "conventional_fallback_passed": True,
            "preemption_replacement_passed": True,
            "return_to_zero_passed": True,
            "accepted": True,
        },
        "observed_at": "2026-08-28T14:00:00Z",
        "valid_until": "2026-08-29T14:00:00Z",
    }
    compatibility_digest = canonical_digest(receipt["compatibility_tuple"])
    for attempt in receipt["attempts"]:
        attempt["compatibility_tuple_digest"] = compatibility_digest
    seal_receipt(receipt)
    return receipt


def documents(path: Path) -> list[dict[str, Any]]:
    with path.open(encoding="utf-8") as stream:
        return [item for item in yaml.safe_load_all(stream) if item is not None]


def deployment(path: Path) -> dict[str, Any]:
    return next(item for item in documents(path) if item["kind"] == "Deployment")


def named(items: list[dict[str, Any]], name: str) -> dict[str, Any]:
    return next(item for item in items if item["name"] == name)


class ColdStartContractTest(unittest.TestCase):
    maxDiff = None

    @classmethod
    def setUpClass(cls) -> None:
        cls.foundation = documents(COLD_START_ROOT / "foundation.yaml")
        cls.daemonsets = [
            documents(path)[0]
            for path in sorted((COLD_START_ROOT / "keepers").glob("*.yaml"))
        ]
        cls.resources = [*cls.foundation, *cls.daemonsets]

    def test_optional_package_is_namespaced_and_has_exact_resource_set(self) -> None:
        self.assertEqual(
            ["ServiceAccount", "ConfigMap", *("DaemonSet" for _ in range(7))],
            [item["kind"] for item in self.resources],
        )
        self.assertTrue(
            all(item["metadata"]["namespace"] == "fs2-models" for item in self.resources)
        )
        service_account = self.resources[0]
        self.assertFalse(service_account["automountServiceAccountToken"])
        self.assertEqual(
            [
                "foundation.yaml",
                "keepers/boltz2-b300-1x.yaml",
                "keepers/evo2-b300-1x.yaml",
                "keepers/genmol-b300-1x.yaml",
                "keepers/nv-segment-ct-b300-1x.yaml",
                "keepers/sdxl-b300-1x.yaml",
                "keepers/vllm-b300-1x.yaml",
                "keepers/vllm-b300-8x.yaml",
            ],
            yaml.safe_load((COLD_START_ROOT / "kustomization.yaml").read_text())["resources"],
        )

    def test_image_keepers_are_immutable_non_gpu_and_non_privileged(self) -> None:
        expected_images = {
            "registry.example.invalid/k8s-inference/models/vllm-openai@sha256:2286e8533ca8b6bc777594bae30524f1426ba46ca21797524e06df6a94b06635",
            "registry.example.invalid/k8s-inference/models/evo2-runtime@sha256:5bee4a3103f4111a5ff4dc597d2e052b39e1d66c782941b5fb64957bb1ab601c",
            "registry.example.invalid/k8s-inference/models/general-media-runtime@sha256:834b6694b7e096c393193d12306ef9b3f0bb313efa806a9c253f49f1f47281fd",
            "registry.example.invalid/k8s-inference/models/general-media-runtime@sha256:8ea08b1a5eabf0ed9c5193e7f49c5546fcbd8452692bbd4ba13accecd7fc8e07",
            "registry.example.invalid/k8s-inference/models/boltz2-blackwell@sha256:ec4ccb67476f0783d1b756959362318691ef44477e485a62eb4f1c77eff10c46",
            "registry.example.invalid/k8s-inference/models/genmol-blackwell@sha256:c0ce8cab57295b6ba2fc4be17d5f5a78751f76b7e93754309fa353d9c2f54a1f",
        }
        observed_images: set[str] = set()
        for daemonset in self.daemonsets:
            pod = daemonset["spec"]["template"]["spec"]
            self.assertEqual(1, len(pod["containers"]))
            self.assertFalse(pod["automountServiceAccountToken"])
            self.assertEqual("fs2-image-cache", pod["serviceAccountName"])
            self.assertNotIn("volumes", pod)
            self.assertNotIn("imagePullSecrets", pod)
            self.assertNotIn("nebius.com/gpu-name", pod["nodeSelector"])
            self.assertEqual(
                "nvidia-b300-sxm6-288gb",
                pod["nodeSelector"]["accelerator.fs2.nebius/class"],
            )
            self.assertIn(
                pod["nodeSelector"]["accelerator.fs2.nebius/pool-id"],
                {"nebius-b300-preemptible-1x", "nebius-b300-preemptible-8x"},
            )
            self.assertEqual("amd64", pod["nodeSelector"]["kubernetes.io/arch"])
            self.assertEqual("true", pod["nodeSelector"]["workload.fs2.nebius/gpu"])
            opt_in_keys = [
                key
                for key in pod["nodeSelector"]
                if key.startswith("cache.fs2.nebius/image-")
            ]
            self.assertEqual(1, len(opt_in_keys))
            self.assertRegex(pod["nodeSelector"][opt_in_keys[0]], r"^[0-9a-f]{32}$")
            self.assertEqual(
                1,
                daemonset["spec"]["updateStrategy"]["rollingUpdate"][
                    "maxUnavailable"
                ],
            )
            self.assertEqual(
                [
                    {
                        "effect": "NoSchedule",
                        "key": "dedicated",
                        "operator": "Equal",
                        "value": "fs2-inference",
                    }
                ],
                pod["tolerations"],
            )
            for container in pod["containers"]:
                image = container["image"]
                self.assertRegex(image, IMAGE_PATTERN)
                observed_images.add(image)
                image_digest = "sha256:" + image.rsplit("@sha256:", 1)[1]
                annotations = daemonset["metadata"]["annotations"]
                self.assertEqual(
                    image_digest,
                    annotations["fs2-serve.nebius.ai/runtime-image-digest"],
                )
                self.assertRegex(
                    annotations["fs2-serve.nebius.ai/runtime-image-amd64-digest"],
                    DIGEST_PATTERN,
                )
                self.assertEqual("IfNotPresent", container["imagePullPolicy"])
                for resource_set in ("requests", "limits"):
                    self.assertNotIn(
                        "nvidia.com/gpu", container["resources"][resource_set]
                    )
                    self.assertIn(
                        "ephemeral-storage", container["resources"][resource_set]
                    )
                security = container["securityContext"]
                self.assertFalse(security["allowPrivilegeEscalation"])
                self.assertTrue(security["readOnlyRootFilesystem"])
                self.assertEqual(["ALL"], security["capabilities"]["drop"])
        self.assertEqual(expected_images, observed_images)
        serialized = json.dumps(self.resources)
        self.assertNotIn("hostPath", serialized)
        self.assertNotIn("privileged", serialized)
        self.assertNotIn("fs2-runtime-registry", serialized)

    def test_image_keepalive_covers_download_on_start_models(self) -> None:
        covered: set[str] = set()
        for daemonset in self.daemonsets:
            covered.update(
                daemonset["metadata"]["annotations"][
                    "fs2-serve.nebius.ai/models"
                ].split(",")
            )
        self.assertEqual(
            {
                "boltz2",
                "evo2-40b",
                "genmol",
                "glm-5-2-fp8",
                "nv-reason-cxr-3b",
                "nv-segment-ct",
                "qwen3-8b",
                "sdxl",
            },
            covered,
        )
        for daemonset in self.daemonsets:
            self.assertEqual(
                "explicit-image-opt-in-does-not-scale-from-zero",
                daemonset["metadata"]["annotations"][
                    "fs2-serve.nebius.ai/scheduling"
                ],
            )

    def test_cache_contract_reuses_only_reviewed_storage_boundaries(self) -> None:
        contract = next(
            item for item in self.resources if item["kind"] == "ConfigMap"
        )["data"]
        self.assertEqual("fs2-cache", contract["shared-cache-pvc"])
        self.assertEqual(
            "/mnt/fs2-serve-cache/models/{model_id}/sha256/{content_digest}",
            contract["shared-content-path"],
        )
        self.assertEqual(
            "/var/lib/fs2-serve/cache/models/{model_id}/sha256/{content_digest}",
            contract["local-content-path"],
        )
        self.assertEqual("reviewed-local-pv-pvc-only", contract["local-nvme-policy"])
        self.assertEqual("forbidden", contract["raw-disk-formatting"])
        self.assertEqual("forbidden", contract["host-path"])
        self.assertEqual(
            "protected-retain-provider-block-pvc", contract["qwen-first-cohort"]
        )
        self.assertEqual(
            "node-service-account-registry-reader", contract["registry-auth"]
        )
        self.assertEqual("driver-580.173.02-sm103", contract["compile-cache-abi"])
        self.assertEqual("2", contract["compile-cache-retention-target-generations"])
        self.assertEqual("supervised-scale-zero-only", contract["compile-cache-gc"])

    def test_catalog_acquisition_plan_still_owns_all_model_writers(self) -> None:
        contract = json.loads(
            (CATALOG_ROOT / "contracts" / "artifact-acquisition.json").read_text()
        )
        models = sorted(
            path.stem for path in (CATALOG_ROOT / "models").glob("*.json")
        )
        self.assertEqual(models, sorted(contract["plans"]))
        self.assertEqual(15, len(models))
        qwen = contract["plans"]["qwen3-8b"]
        self.assertEqual(
            "atomic-content-addressed-provider-block-pvc", qwen["publication"]
        )
        self.assertNotIn("fs2-models/shared-cache-pvc", qwen["required_prerequisites"])
        for model_id, plan in contract["plans"].items():
            record = json.loads(
                (CATALOG_ROOT / "models" / f"{model_id}.json").read_text()
            )
            self.assertTrue(record["cache"]["pre_pull_image"])
            if plan["publication"] == "atomic-content-addressed-sfs":
                self.assertIn(
                    "fs2-models/shared-cache-pvc", plan["required_prerequisites"]
                )

    def test_qwen_uses_a_restart_safe_provider_block_cold_cache(self) -> None:
        path = FS2_ROOT / "models" / "general-media" / "k8s" / "qwen3-8b.yaml"
        resources = documents(path)
        claim = next(
            resource
            for resource in resources
            if resource["kind"] == "PersistentVolumeClaim"
        )
        self.assertEqual("qwen3-8b-cache", claim["metadata"]["name"])
        self.assertEqual("compute-csi-default-sc", claim["spec"]["storageClassName"])
        self.assertEqual("64Gi", claim["spec"]["resources"]["requests"]["storage"])

        pod = deployment(path)["spec"]["template"]["spec"]
        model_volume = named(pod["volumes"], "model")
        self.assertEqual(
            "qwen3-8b-cache",
            model_volume["persistentVolumeClaim"]["claimName"],
        )
        config = next(
            resource for resource in resources if resource["kind"] == "ConfigMap"
        )
        localizer = config["data"]["localize.py"]
        self.assertIn('p.name != "localization-receipt.json"', localizer)
        self.assertIn("snapshot_download(", localizer)

    def test_general_media_compile_caches_use_existing_pvcs_and_exact_keys(self) -> None:
        manifests = {
            "nv-reason-cxr-3b": (
                "nv-reason-cxr-3b.yaml",
                "vllm",
                "2286e8533ca8b6bc777594bae30524f1426ba46ca21797524e06df6a94b06635",
                "8122f69972fb3af557c0011b72fe66c61da49c60d30f0affaa09deb0ec315a4c",
            ),
            "nv-segment-ct": (
                "nv-segment-ct.yaml",
                "server",
                "834b6694b7e096c393193d12306ef9b3f0bb313efa806a9c253f49f1f47281fd",
                "08e8ed72f8bb0702dc9a8c0c3b0edefc23a6e538e6955357da62c0c2ccfede89",
            ),
            "sdxl": (
                "sdxl.yaml",
                "server",
                "8ea08b1a5eabf0ed9c5193e7f49c5546fcbd8452692bbd4ba13accecd7fc8e07",
                "8699099d13a60f34bd19470fa56abd7a8614260b09838bd61a876d5b9d080c81",
            ),
            "evo2-40b": (
                "evo2-40b.yaml",
                "model",
                "5bee4a3103f4111a5ff4dc597d2e052b39e1d66c782941b5fb64957bb1ab601c",
                "b45c6237bc56e0c7484e6402b12cb765f1241d6714403c8e36debb6577dd14fa",
            ),
        }
        root = FS2_ROOT / "models" / "general-media" / "k8s"
        for model_id, (
            filename,
            container_name,
            image_digest,
            content_digest,
        ) in manifests.items():
            item = deployment(root / filename)
            self.assertEqual(1, item["spec"]["replicas"])
            self.assertEqual("Recreate", item["spec"]["strategy"]["type"])
            pod = item["spec"]["template"]["spec"]
            model_volume = named(pod["volumes"], "model-cache")
            self.assertIn("persistentVolumeClaim", model_volume)
            init = named(pod["initContainers"], "prepare-runtime-cache")
            container = named(pod["containers"], container_name)
            self.assertEqual(container["image"], init["image"])
            root_path = (
                f"/model-cache/.fs2/runtime/{image_digest}/{content_digest}/"
                "driver-580.173.02-sm103"
            )
            self.assertEqual(
                root_path,
                named(init["env"], "FS2_RUNTIME_CACHE_ROOT")["value"],
            )
            self.assertEqual(
                f"sha256:{content_digest}",
                item["metadata"]["annotations"]["fs2.nebius/model-content-digest"],
            )
            self.assertEqual(
                "driver-580.173.02-sm103",
                item["metadata"]["annotations"]["fs2.nebius/compile-cache-abi"],
            )
            for resource_set in ("requests", "limits"):
                self.assertNotIn("nvidia.com/gpu", init["resources"][resource_set])
                self.assertIn("ephemeral-storage", init["resources"][resource_set])
            self.assertFalse(init["securityContext"]["allowPrivilegeEscalation"])
            self.assertTrue(init["securityContext"]["readOnlyRootFilesystem"])
            self.assertEqual(
                ["ALL"], init["securityContext"]["capabilities"]["drop"]
            )
            env = {
                entry["name"]: entry.get("value")
                for entry in container.get("env", [])
            }
            self.assertEqual("/model-cache", env.get("HF_HOME", "/model-cache"))
            self.assertNotIn("HF_HUB_CACHE", env)
            self.assertNotIn("HF_XET_CACHE", env)
            for name in (
                "CUDA_CACHE_PATH",
                "TORCH_EXTENSIONS_DIR",
                "TORCHINDUCTOR_CACHE_DIR",
                "TRITON_CACHE_DIR",
            ):
                self.assertTrue(env[name].startswith(root_path + "/"), (model_id, name))
            if model_id == "evo2-40b":
                self.assertNotIn("HOME", env)
                self.assertNotIn("XDG_CACHE_HOME", env)
                self.assertNotIn(
                    "runtime-cache", [volume["name"] for volume in pod["volumes"]]
                )
            else:
                runtime_cache = named(pod["volumes"], "runtime-cache")
                self.assertIn("sizeLimit", runtime_cache["emptyDir"])
                self.assertEqual("/runtime-cache/home", env["HOME"])
                self.assertEqual("/runtime-cache/xdg", env["XDG_CACHE_HOME"])
                self.assertIn(
                    "/runtime-cache",
                    [mount["mountPath"] for mount in container["volumeMounts"]],
                )
            if model_id == "nv-reason-cxr-3b":
                self.assertTrue(env["VLLM_CACHE_ROOT"].startswith(root_path + "/"))

    def test_glm_cold_cache_is_staged_in_parallel_to_local_ephemeral_storage(
        self,
    ) -> None:
        path = FS2_ROOT / "models" / "general-media" / "k8s" / "glm-5-2-fp8.yaml"
        item = deployment(path)
        resources = documents(path)
        pod = item["spec"]["template"]["spec"]
        init = named(pod["initContainers"], "localize-model")
        container = named(pod["containers"], "vllm")

        cold_cache = named(pod["volumes"], "cold-cache")
        self.assertEqual(
            "glm-5-2-fp8-cache",
            cold_cache["persistentVolumeClaim"]["claimName"],
        )
        self.assertEqual("1Ti", named(pod["volumes"], "model-cache")["emptyDir"]["sizeLimit"])
        self.assertEqual(
            "256Gi", named(pod["volumes"], "runtime-cache")["emptyDir"]["sizeLimit"]
        )
        self.assertEqual("768Gi", init["resources"]["requests"]["ephemeral-storage"])
        self.assertEqual("1Ti", init["resources"]["limits"]["ephemeral-storage"])
        self.assertNotIn("nvidia.com/gpu", init["resources"]["requests"])
        self.assertNotIn("nvidia.com/gpu", init["resources"]["limits"])

        init_env = {entry["name"]: entry["value"] for entry in init["env"]}
        root_path = (
            "/runtime-cache/.fs2/runtime/"
            "2286e8533ca8b6bc777594bae30524f1426ba46ca21797524e06df6a94b06635/"
            "20fe040f62612ae480971f0cf3433004062a186ff01af6d69127e6dd888bd795/"
            "driver-580.173.02-sm103"
        )
        self.assertEqual(root_path, init_env["FS2_RUNTIME_CACHE_ROOT"])
        self.assertEqual("32", init_env["FS2_LOCALIZE_WORKERS"])
        self.assertEqual(
            {"/cold-cache", "/model-cache", "/opt/fs2-localizer", "/runtime-cache"},
            {mount["mountPath"] for mount in init["volumeMounts"]},
        )

        localizer = next(
            resource
            for resource in resources
            if resource["kind"] == "ConfigMap"
            and resource["metadata"]["name"] == "glm-5-2-fp8-localizer"
        )["data"]["localize.py"]
        self.assertIn("ThreadPoolExecutor(max_workers=workers)", localizer)
        self.assertIn("755_663_676_164", localizer)
        self.assertIn("snapshot_download(", localizer)
        self.assertIn('target": "kubelet-ephemeral-local-nvme"', localizer)

        self.assertEqual("/model-cache", container["args"][0])
        env = {entry["name"]: entry["value"] for entry in container["env"]}
        self.assertEqual("1", env["HF_HUB_OFFLINE"])
        self.assertEqual("1", env["TRANSFORMERS_OFFLINE"])
        for name in (
            "CUDA_CACHE_PATH",
            "TORCH_EXTENSIONS_DIR",
            "TORCHINDUCTOR_CACHE_DIR",
            "TRITON_CACHE_DIR",
            "VLLM_CACHE_ROOT",
        ):
            self.assertTrue(env[name].startswith(root_path + "/"), name)
        model_mount = next(
            mount for mount in container["volumeMounts"] if mount["name"] == "model-cache"
        )
        self.assertTrue(model_mount["readOnly"])

    def test_bionemo_fallbacks_preserve_hf_layout_and_persist_compile_cache(self) -> None:
        manifests = {
            "boltz2": (
                "boltz2/kubernetes.yaml",
                "boltz2",
                "ec4ccb67476f0783d1b756959362318691ef44477e485a62eb4f1c77eff10c46",
                "9459dd0c80992f21d07e70ae7d54c318e66a9d5202d6e849a134957b0740d82a",
            ),
            "genmol": (
                "genmol/kubernetes.yaml",
                "genmol",
                "c0ce8cab57295b6ba2fc4be17d5f5a78751f76b7e93754309fa353d9c2f54a1f",
                "7ef31ef5652574b6c108231a0d67d6d871b4df3f780e43c5a4dcc2491bc8d44a",
            ),
        }
        root = FS2_ROOT / "models" / "bionemo"
        for model_id, (
            filename,
            container_name,
            image_digest,
            model_key,
        ) in manifests.items():
            item = deployment(root / filename)
            self.assertEqual(1, item["spec"]["replicas"])
            self.assertEqual("Recreate", item["spec"]["strategy"]["type"])
            pod = item["spec"]["template"]["spec"]
            init = named(pod["initContainers"], "prepare-runtime-cache")
            container = named(pod["containers"], container_name)
            self.assertEqual(container["image"], init["image"])
            cache_root = (
                f"/models/.fs2/runtime/{image_digest}/{model_key}/"
                "driver-580.173.02-sm103"
            )
            self.assertEqual(
                cache_root,
                named(init["env"], "FS2_RUNTIME_CACHE_ROOT")["value"],
            )
            env = {entry["name"]: entry.get("value") for entry in container["env"]}
            self.assertEqual(
                "driver-580.173.02-sm103",
                item["metadata"]["annotations"]["fs2.nebius/compile-cache-abi"],
            )
            self.assertEqual(
                "sha256:" + model_key,
                item["metadata"]["annotations"][
                    "fs2.nebius/model-content-digest"
                ],
            )
            for resource_set in ("requests", "limits"):
                self.assertNotIn("nvidia.com/gpu", init["resources"][resource_set])
                self.assertIn("ephemeral-storage", init["resources"][resource_set])
            self.assertFalse(init["securityContext"]["allowPrivilegeEscalation"])
            self.assertTrue(init["securityContext"]["readOnlyRootFilesystem"])
            self.assertEqual(
                ["ALL"], init["securityContext"]["capabilities"]["drop"]
            )
            self.assertEqual("/models/huggingface", env["HF_HOME"])
            self.assertEqual("/models/huggingface/hub", env["HF_HUB_CACHE"])
            for name in (
                "CUDA_CACHE_PATH",
                "HOME",
                "TORCH_EXTENSIONS_DIR",
                "TORCHINDUCTOR_CACHE_DIR",
                "TRITON_CACHE_DIR",
                "XDG_CACHE_HOME",
            ):
                self.assertTrue(env[name].startswith(cache_root + "/"), (model_id, name))

    def test_post_acceptance_benchmark_contract_is_fail_closed(self) -> None:
        contract = json.loads(
            (COLD_START_ROOT / "post-acceptance-benchmark-contract.json").read_text()
        )
        self.assertEqual(
            "fs2-serve.nebius.ai/v1alpha1", contract["api_version"]
        )
        self.assertEqual("planning-only", contract["metadata"]["authority"])
        self.assertFalse(contract["metadata"]["hardware_support_authority"])

        spec = contract["spec"]
        self.assertTrue(spec["admission"]["requires_model_route_acceptance"])
        self.assertTrue(spec["admission"]["requires_exact_runtime_variant"])
        self.assertTrue(spec["admission"]["fail_closed_on_tuple_mismatch"])
        self.assertEqual("conventional", spec["admission"]["production_fallback"])
        self.assertEqual(
            "fs2-serve.nebius.ai/qualification-cohort/v4",
            spec["admission"]["extends_existing_signed_evidence"],
        )
        self.assertTrue(spec["admission"]["does_not_create_route_authority"])

        capacity_states = {item["id"]: item for item in spec["capacity_states"]}
        self.assertEqual(
            {
                "ready-pod-warm",
                "resident-sleep",
                "prepared-node-zero-pod",
                "fresh-node-zero-pod",
                "preemption-replacement",
                "durable-cache-loss-fallback",
            },
            set(capacity_states),
        )
        self.assertFalse(capacity_states["ready-pod-warm"]["cold_start_class"])
        self.assertFalse(capacity_states["resident-sleep"]["cold_start_class"])
        self.assertFalse(
            capacity_states["preemption-replacement"]["local_cache_may_be_present"]
        )

        self.assertEqual(
            "durable-activation-request-accepted", spec["clock"]["t0"]
        )
        self.assertEqual(
            "first-unretried-semantic-response-accepted", spec["clock"]["t1"]
        )
        self.assertEqual("t0-to-call1", spec["clock"]["product_latency_metric"])
        self.assertEqual(
            "t0-to-call2", spec["clock"]["promotion_qualification_metric"]
        )
        self.assertTrue(spec["clock"]["both_semantic_calls_required_for_success"])
        self.assertIn("provider-instance-created", spec["clock"]["required_events"])
        self.assertIn("semantic-call1-accepted", spec["clock"]["required_events"])
        self.assertIn("semantic-call2-accepted", spec["clock"]["required_events"])
        self.assertTrue(spec["clock"]["retries_are_separate_attempts"])

        mappings = spec["existing_evidence_mapping"]
        self.assertEqual("prepared-node", mappings["prepared-node-zero-pod"])
        self.assertEqual("new-node", mappings["fresh-node-zero-pod"])
        self.assertEqual(
            "new-node-plus-preemption-receipt", mappings["preemption-replacement"]
        )

        self.assertEqual(3, spec["cohorts"]["exploratory_minimum_attempts_per_cell"])
        self.assertEqual(20, spec["cohorts"]["promotion_minimum_attempts_per_cell"])
        self.assertTrue(spec["cohorts"]["retain_every_failure"])
        self.assertTrue(spec["cohorts"]["raw_attempt_receipt_required"])
        self.assertEqual("nearest-rank", spec["cohorts"]["percentile_estimator"])
        self.assertEqual(
            "failures-rank-after-all-successful-durations",
            spec["cohorts"]["failure_ordering"],
        )
        self.assertEqual(20, spec["cohorts"]["p95_withheld_below_attempts"])

        required_tuple = set(spec["compatibility_tuple"]["required_fields"])
        self.assertTrue(
            {
                "model_content_digest",
                "semantic_oracle_digest",
                "semantic_request_contract_digest",
                "runtime_variant",
                "runtime_source_identity_digest",
                "runtime_image_digest",
                "runtime_environment_digest",
                "execution_identity_digest",
                "loader_or_engine_format",
                "gpu_chip_type",
                "gpu_compute_capability",
                "workload_gpu_count",
                "gpu_topology",
                "gpu_topology_inventory_digest",
                "allocated_gpu_uuids",
                "mig_mode",
                "mig_profile",
                "driver_version",
                "cuda_version",
                "kernel_release",
                "container_runtime_version",
                "checkpoint_tool_digest",
                "criu_version",
                "artifact_manifest_digest",
                "artifact_content_digest",
                "artifact_bytes",
                "node_identity_digest",
                "pvc_identity_digest",
                "compile_cache_abi",
                "capacity_state",
            }
            <= required_tuple
        )
        for key in (
            "cross_chip_restore_default",
            "cross_driver_restore_default",
            "cross_topology_restore_default",
            "cross_mig_profile_restore_default",
        ):
            self.assertEqual("denied", spec["compatibility_tuple"][key])

        mechanisms = {item["id"]: item for item in spec["mechanisms"]}
        self.assertTrue(mechanisms["conventional"]["render_enabled_by_default"])
        self.assertTrue(
            all(
                not item["render_enabled_by_default"]
                for name, item in mechanisms.items()
                if name != "conventional"
            )
        )
        self.assertEqual(
            "supported-with-relocalization-after-node-ready",
            mechanisms["local-nvme-localization"]["elasticity"]["zero_hot_nodes"],
        )
        self.assertFalse(mechanisms["vllm-sleep-level-1"]["production_candidate"])
        self.assertEqual(
            "not-supported",
            mechanisms["vllm-sleep-level-1"]["elasticity"]["zero_hot_nodes"],
        )
        self.assertFalse(mechanisms["cuda-criu-snapshot"]["production_candidate"])
        self.assertEqual(
            "must-pass-same-class-replacement-cell",
            mechanisms["cuda-criu-snapshot"]["elasticity"]["replacement_recovery"],
        )
        self.assertEqual(
            "conditional-on-durable-checkpoint-access",
            mechanisms["dynamo-snapshot"]["elasticity"]["zero_hot_nodes"],
        )
        self.assertIn(
            "dynamo-snapshot",
            mechanisms["dynamo-gpu-memory-service"]["incompatible_with"],
        )

        acceptance = spec["acceptance"]
        self.assertTrue(acceptance["same_capacity_state_control_required"])
        self.assertTrue(acceptance["same_storage_state_control_required"])
        self.assertTrue(acceptance["semantic_equivalence_required"])
        self.assertTrue(acceptance["p95_t0_to_call2_must_not_regress"])
        self.assertTrue(acceptance["failure_ranked_percentiles_required"])
        self.assertTrue(acceptance["attempt_deadline_and_timeout_failure_required"])
        self.assertTrue(acceptance["preemption_replacement_cell_required"])
        self.assertTrue(acceptance["conventional_fallback_must_pass"])
        self.assertTrue(acceptance["no_promotion_from_historical_or_different_hardware"])

        rendering = spec["rendering"]
        self.assertTrue(rendering["terraform"]["feature_flags_default_false"])
        self.assertTrue(rendering["terraform"]["typed_accelerator_pool_contract_required"])
        self.assertTrue(rendering["helm"]["single_crd_owner_required"])
        self.assertTrue(rendering["helm"]["secrets_by_reference_only"])
        self.assertFalse(rendering["observability"]["gpu_uuid_in_metric_labels"])
        self.assertFalse(rendering["observability"]["secret_material_in_receipts"])

        prerequisites = spec["platform_prerequisites"]
        image_volume = prerequisites["native_oci_image_volume"]
        self.assertEqual("1.35", image_volume["current_fs2_kubernetes_version"])
        self.assertEqual("beta-enabled-by-default", image_volume["current_feature_state"])
        self.assertEqual("2.1", image_volume["containerd_minimum_for_kubernetes_1_35"])
        dynamo = prerequisites["dynamo_snapshot"]
        self.assertEqual("amd64", dynamo["node_architecture"])
        self.assertEqual("580.xx", dynamo["minimum_driver_single_gpu"])
        self.assertEqual("13", dynamo["b300_cuda_major"])
        self.assertEqual("disabled", dynamo["multi_gpu"])

        deferred = spec["deferred_snapshot_custody_acceptance"]
        self.assertTrue(deferred["blocks_snapshot_production_promotion"])
        self.assertEqual(
            "fs2-snapshot-artifact-custody-hardening",
            deferred["implementation_task"],
        )

    def test_post_acceptance_receipt_schema_and_executable_validator(self) -> None:
        schema = json.loads(
            (COLD_START_ROOT / "post-acceptance-benchmark-receipt.schema.json").read_text()
        )
        Draft202012Validator.check_schema(schema)
        receipt = benchmark_receipt()
        validate_receipt(receipt, schema)

        unknown = deepcopy(receipt)
        unknown["compatibility_tuple"]["unreviewed_alias"] = "sm103"
        with self.assertRaisesRegex(ReceiptValidationError, "Additional properties"):
            validate_receipt(unknown, schema)

        missing_identity = deepcopy(receipt)
        del missing_identity["compatibility_tuple"]["semantic_oracle_digest"]
        with self.assertRaisesRegex(ReceiptValidationError, "semantic_oracle_digest"):
            validate_receipt(missing_identity, schema)

        wrong_mapping = deepcopy(receipt)
        wrong_mapping["attempts"][0]["existing_cohort"] = "prepared-node"
        with self.assertRaisesRegex(ReceiptValidationError, "wrong existing cohort"):
            validate_receipt(wrong_mapping, schema)

        wrong_tuple_binding = deepcopy(receipt)
        wrong_tuple_binding["attempts"][0]["compatibility_tuple_digest"] = digest(
            "another-tuple"
        )
        with self.assertRaisesRegex(
            ReceiptValidationError, "compatibility tuple digest differs"
        ):
            validate_receipt(wrong_tuple_binding, schema)

        wrong_contract = deepcopy(receipt)
        wrong_contract["benchmark_contract_digest"] = digest("another-contract")
        with self.assertRaisesRegex(ReceiptValidationError, "source contract"):
            validate_receipt(wrong_contract, schema)

        no_call2 = deepcopy(receipt)
        no_call2["attempts"][0]["call2_utc"] = None
        no_call2["attempts"][0]["t0_to_call2_seconds"] = None
        no_call2["attempts"][0]["semantic_receipt_digest"] = None
        for event in no_call2["attempts"][0]["events"]:
            if event["name"] == "semantic-call2-accepted":
                event["state"] = "not-applicable"
                event["timestamp"] = None
        with self.assertRaisesRegex(ReceiptValidationError, "semantic-call2-accepted"):
            validate_receipt(no_call2, schema)

        aggregate_drift = deepcopy(receipt)
        aggregate_drift["aggregates"]["candidate"][
            "failure_ranked_p95_t0_to_call1_seconds"
        ] += 1
        with self.assertRaisesRegex(ReceiptValidationError, "does not match raw attempts"):
            validate_receipt(aggregate_drift, schema)

        order_drift = deepcopy(receipt)
        order_drift["attempts"][0], order_drift["attempts"][1] = (
            order_drift["attempts"][1],
            order_drift["attempts"][0],
        )
        with self.assertRaisesRegex(ReceiptValidationError, "strictly alternate"):
            validate_receipt(order_drift, schema)

        deadline_drift = deepcopy(receipt)
        deadline_drift["attempts"][0]["deadline_utc"] = "2026-08-28T12:00:59Z"
        with self.assertRaisesRegex(ReceiptValidationError, "deadline differs"):
            validate_receipt(deadline_drift, schema)

        phase_order_drift = deepcopy(receipt)
        for event in phase_order_drift["attempts"][0]["events"]:
            if event["name"] == "readiness-accepted":
                event["timestamp"] = "2026-08-28T12:00:21Z"
        with self.assertRaisesRegex(ReceiptValidationError, "events are out of order"):
            validate_receipt(phase_order_drift, schema)

        digest_drift = deepcopy(receipt)
        digest_drift["observed_at"] = "2026-08-28T14:00:01Z"
        with self.assertRaisesRegex(ReceiptValidationError, "receipt digest differs"):
            validate_receipt(digest_drift, schema)

        failure_ranked = deepcopy(receipt)
        for index in (1, 3):
            attempt = failure_ranked["attempts"][index]
            attempt["status"] = "FAIL"
            attempt["failure_code"] = "deadline-exceeded"
            attempt["call1_utc"] = None
            attempt["call2_utc"] = None
            attempt["t0_to_call1_seconds"] = None
            attempt["t0_to_call2_seconds"] = None
            attempt["semantic_receipt_digest"] = None
            for event in attempt["events"]:
                if event["name"] in {
                    "semantic-call1-accepted",
                    "semantic-call2-accepted",
                }:
                    event["state"] = "not-applicable"
                    event["timestamp"] = None
        candidate_successes = [
            item
            for item in failure_ranked["attempts"]
            if item["arm"] == "candidate" and item["status"] == "PASS"
        ]
        candidate_aggregate = failure_ranked["aggregates"]["candidate"]
        candidate_aggregate.update(
            {"attempt_count": 20, "success_count": 18, "failure_count": 2}
        )
        for call in ("call1", "call2"):
            values = sorted(
                float(item[f"t0_to_{call}_seconds"])
                for item in candidate_successes
            )
            median = statistics.median(values)
            candidate_aggregate[
                f"failure_ranked_p50_t0_to_{call}_seconds"
            ] = values[9]
            candidate_aggregate[f"failure_ranked_p95_t0_to_{call}_seconds"] = None
            candidate_aggregate[
                f"{call}_median_absolute_deviation_seconds"
            ] = float(statistics.median(abs(item - median) for item in values))
        failure_ranked["status"] = "FAIL"
        failure_ranked["decision"].update(
            {
                "failure_rate_non_regression_passed": False,
                "absolute_effect_passed": False,
                "relative_effect_passed": False,
                "call2_latency_non_regression_passed": False,
                "accepted": False,
            }
        )
        seal_receipt(failure_ranked)
        validate_receipt(failure_ranked, schema)
        self.assertIsNone(
            candidate_aggregate["failure_ranked_p95_t0_to_call1_seconds"]
        )

    def test_post_acceptance_plan_links_only_planning_authority(self) -> None:
        plan = (COLD_START_ROOT / "POST_ACCEPTANCE_COLD_START_PLAN.md").read_text()
        self.assertRegex(plan, r"planning\s+authority only")
        self.assertRegex(plan, r"All 15 enable only\s+`conventional`")
        self.assertRegex(plan, r"one KEDA `ScaledObject` for each")
        self.assertIn("promotion_minimum_attempts_per_cell", (
            COLD_START_ROOT / "post-acceptance-benchmark-contract.json"
        ).read_text())
        self.assertIn("same chip type", plan)
        self.assertIn("GPU UUID belongs in bounded receipts", plan)
        self.assertRegex(
            plan, r"Neither new pilot\s+starts before its exact route is accepted"
        )


if __name__ == "__main__":
    unittest.main()
