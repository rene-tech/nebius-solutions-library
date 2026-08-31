from __future__ import annotations

import re
import unittest
from pathlib import Path


ADDONS = Path(__file__).resolve().parents[1]


class AddonContractTest(unittest.TestCase):
    def test_required_versions_are_pinned(self) -> None:
        lock = (ADDONS / "lock.env").read_text(encoding="utf-8")
        expected = {
            "GATEWAY_API_VERSION": "v1.5.1",
            "ENVOY_GATEWAY_VERSION": "v1.8.3",
            "CERT_MANAGER_VERSION": "v1.21.1",
            "KEDA_VERSION": "2.20.2",
            "KUEUE_VERSION": "0.17.8",
            "KSERVE_VERSION": "v0.20.0",
        }
        for key, value in expected.items():
            self.assertRegex(lock, rf"(?m)^{key}={re.escape(value)}$")
        hashes = re.findall(r"(?m)^[A-Z0-9_]+_SHA256=([0-9a-f]+)$", lock)
        self.assertGreaterEqual(len(hashes), 7)
        self.assertTrue(all(len(value) == 64 for value in hashes))

    def test_base_install_does_not_own_application_or_storage_resources(self) -> None:
        base = "\n".join(
            path.read_text(encoding="utf-8")
            for path in sorted((ADDONS / "manifests").glob("*.yaml"))
        )
        for kind in ("StorageClass", "Gateway", "HTTPRoute", "ScaledObject", "InferenceService"):
            self.assertNotIn(f"kind: {kind}\n", base)
        self.assertIn("kind: GatewayClass\n", base)

    def test_install_order_and_standard_mode(self) -> None:
        script = (ADDONS / "scripts/install.sh").read_text(encoding="utf-8")
        markers = [
            "$cache_dir/$GATEWAY_API_FILE",
            "upgrade --install envoy-gateway",
            "upgrade --install cert-manager",
            "upgrade --install keda",
            "upgrade --install kueue",
            "upgrade --install kserve-crd",
            "upgrade --install kserve ",
        ]
        positions = [script.index(marker) for marker in markers]
        self.assertEqual(positions, sorted(positions))
        values = (ADDONS / "values/kserve.yaml").read_text(encoding="utf-8")
        self.assertIn("deploymentMode: Standard", values)
        self.assertIn("disableIngressCreation: true", values)
        self.assertIn("localmodel:\n    enabled: false", values)

    def test_smoke_is_non_gpu_internal_and_digest_pinned(self) -> None:
        smoke = (ADDONS / "smoke/resources.yaml").read_text(encoding="utf-8")
        self.assertIn("type: ClusterIP", smoke)
        self.assertNotIn("nvidia.com/gpu", smoke)
        self.assertNotIn("kind: PersistentVolumeClaim", smoke)
        images = re.findall(r"(?m)^\s+image:\s+(\S+)$", smoke)
        self.assertGreaterEqual(len(images), 4)
        self.assertTrue(all("@sha256:" in image for image in images))


if __name__ == "__main__":
    unittest.main()
