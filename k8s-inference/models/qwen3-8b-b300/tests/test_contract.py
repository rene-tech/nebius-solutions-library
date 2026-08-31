from __future__ import annotations

import json
import subprocess
import unittest
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
MODEL_REVISION = "b968826d9c46dd6066d109eabc6255188de91218"
IMAGE_DIGEST = "sha256:2286e8533ca8b6bc777594bae30524f1426ba46ca21797524e06df6a94b06635"


class RuntimeContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        rendered = subprocess.run(
            ["kubectl", "kustomize", str(ROOT)],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        cls.objects = list(yaml.safe_load_all(rendered))
        cls.by_kind_name = {
            (item["kind"], item["metadata"]["name"]): item for item in cls.objects
        }

    def test_model_lock_is_complete_and_exact(self) -> None:
        lock = json.loads((ROOT / "model.lock.json").read_text())
        model = lock["model"]
        self.assertEqual(MODEL_REVISION, model["revision"])
        self.assertEqual("bfloat16", model["dtype"])
        self.assertIsNone(model["quantization"])
        self.assertEqual(15, len(model["files"]))
        self.assertEqual(
            model["total_size_bytes"], sum(item["size"] for item in model["files"])
        )
        weights = [item for item in model["files"] if item["path"].endswith("safetensors")]
        self.assertEqual(5, len(weights))
        self.assertEqual(model["weight_size_bytes"], sum(item["size"] for item in weights))
        for item in model["files"]:
            self.assertRegex(item["sha256"], r"^[0-9a-f]{64}$")

    def test_runtime_is_digest_pinned_to_one_gpu_and_preemptible_b300(self) -> None:
        deployment = self.by_kind_name[("Deployment", "qwen3-8b-b300")]
        template = deployment["spec"]["template"]
        spec = template["spec"]
        self.assertFalse(spec["automountServiceAccountToken"])
        self.assertTrue(spec["securityContext"]["runAsNonRoot"])
        self.assertEqual(1000, spec["securityContext"]["runAsUser"])
        self.assertEqual("Recreate", deployment["spec"]["strategy"]["type"])
        self.assertEqual("preemptible", spec["nodeSelector"]["capacity.fs2.nebius/type"])
        self.assertEqual("b300-8x", spec["nodeSelector"]["capacity.fs2.nebius/preset"])
        self.assertEqual("B300", spec["nodeSelector"]["nebius.com/gpu-name"])
        for container in [*spec["initContainers"], *spec["containers"]]:
            self.assertTrue(container["image"].endswith(f"@{IMAGE_DIGEST}"))
            self.assertNotIn(":latest", container["image"])
        runtime = spec["containers"][0]
        self.assertEqual("1", runtime["resources"]["requests"]["nvidia.com/gpu"])
        self.assertEqual("1", runtime["resources"]["limits"]["nvidia.com/gpu"])
        args = runtime["args"]
        self.assertIn("bfloat16", args)
        self.assertIn("32768", args)
        self.assertIn("--enable-prefix-caching", args)
        self.assertNotIn("--quantization", args)
        self.assertNotEqual(runtime["readinessProbe"], runtime["livenessProbe"])

    def test_internal_service_and_namespace_boundaries(self) -> None:
        service = self.by_kind_name[("Service", "qwen3-8b-b300")]
        self.assertEqual("ClusterIP", service["spec"]["type"])
        namespace = self.by_kind_name[("Namespace", "fs2-models")]
        self.assertEqual(
            "baseline", namespace["metadata"]["labels"]["pod-security.kubernetes.io/enforce"]
        )
        quota = self.by_kind_name[("ResourceQuota", "qwen3-8b-b300")]
        self.assertEqual("1", quota["spec"]["hard"]["requests.nvidia.com/gpu"])
        policy = self.by_kind_name[("NetworkPolicy", "qwen3-8b-b300")]
        self.assertEqual(["Ingress", "Egress"], policy["spec"]["policyTypes"])


if __name__ == "__main__":
    unittest.main()
