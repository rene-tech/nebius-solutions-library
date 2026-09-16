"""Real admission-chain tests for SAI-09 on an ISOLATED kind cluster.

These tests prove the enforcement the manifests promise, through the real
API server admission chain: `kubectl debug`-style ephemeral-container
injection is governed (a mutable/foreign image is denied at the
pods/ephemeralcontainers subresource) while an allow-listed digest-pinned
debug image still injects, so debugging keeps working under governance.

Isolation contract: a throwaway kind cluster with its own kubeconfig in a
temporary directory. The developer's kubeconfig, contexts, and every live
cluster are never touched; the cluster is deleted afterwards. The tests skip
when kind or kubectl is unavailable.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import time
import unittest
import uuid
from pathlib import Path

DEPLOY_ROOT = Path(__file__).resolve().parents[1]
POLICY_PATH = DEPLOY_ROOT / "security" / "image-provenance" / "policy.yaml"

REGISTRY_PREFIX = "cr.fixture.invalid/"
PLATFORM_PREFIX = "cr.fixture.invalid/fs2-platform/"
PLATFORM_DIGEST = "sha256:" + "a" * 64
DEBUG_IMAGE = "cr.fixture.invalid/fs2-tools/debug@sha256:" + "b" * 64
EVIL_IMAGE = "evil.invalid/debug:latest"


@unittest.skipUnless(
    shutil.which("kind") and shutil.which("kubectl"),
    "kind and kubectl are required for real admission tests",
)
class KindAdmissionTest(unittest.TestCase):
    """Ephemeral-container injection is admission-governed, not disabled."""

    cluster = f"fs2-sai09-admission-{uuid.uuid4().hex[:8]}"

    @classmethod
    def setUpClass(cls) -> None:
        cls._holder = tempfile.TemporaryDirectory()
        cls.kubeconfig = str(Path(cls._holder.name) / "kubeconfig")
        cls._run(
            "kind",
            "create",
            "cluster",
            "--name",
            cls.cluster,
            "--kubeconfig",
            cls.kubeconfig,
            "--wait",
            "120s",
            timeout=600,
        )
        cls._kubectl("create", "namespace", "fs2-system")
        cls._kubectl("apply", "-f", str(POLICY_PATH))
        cls._kubectl(
            "-n",
            "fs2-system",
            "create",
            "configmap",
            "fs2-image-provenance-allowlist",
            f"--from-literal=registry-prefixes={REGISTRY_PREFIX}",
            f"--from-literal=platform-repository-prefix={PLATFORM_PREFIX}",
            f"--from-literal=platform-digests={PLATFORM_DIGEST}",
            "--from-literal=deploy-principals=kubernetes-admin",
        )
        cls._await_policy_active()
        pod = {
            "apiVersion": "v1",
            "kind": "Pod",
            "metadata": {"name": "sai09-debug-target", "namespace": "fs2-system"},
            "spec": {
                "restartPolicy": "Never",
                "containers": [
                    {
                        "name": "app",
                        "image": PLATFORM_PREFIX + "control-plane@" + PLATFORM_DIGEST,
                        "command": ["sleep", "600"],
                    }
                ],
            },
        }
        cls._kubectl("apply", "-f", "-", input_text=json.dumps(pod))

    @classmethod
    def tearDownClass(cls) -> None:
        subprocess.run(
            ["kind", "delete", "cluster", "--name", cls.cluster],
            capture_output=True,
            text=True,
            timeout=300,
            check=False,
        )
        cls._holder.cleanup()

    @classmethod
    def _run(cls, *command: str, input_text: str | None = None, timeout: int = 120):
        environment = dict(os.environ, KUBECONFIG=cls.kubeconfig)
        return subprocess.run(
            list(command),
            capture_output=True,
            text=True,
            input=input_text,
            timeout=timeout,
            env=environment,
        )

    @classmethod
    def _kubectl(cls, *arguments: str, input_text: str | None = None):
        result = cls._run("kubectl", *arguments, input_text=input_text)
        if result.returncode != 0:
            raise AssertionError(
                f"kubectl {' '.join(arguments)} failed: {result.stderr}"
            )
        return result

    @classmethod
    def _await_policy_active(cls) -> None:
        # VAP/param propagation is eventually consistent: poll until an
        # unpinned probe pod is actually DENIED before running assertions.
        probe = {
            "apiVersion": "v1",
            "kind": "Pod",
            "metadata": {"name": "sai09-activation-probe", "namespace": "fs2-system"},
            "spec": {
                "restartPolicy": "Never",
                "containers": [{"name": "probe", "image": "evil.invalid/x:latest"}],
            },
        }
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            result = cls._run(
                "kubectl", "apply", "-f", "-", input_text=json.dumps(probe)
            )
            if result.returncode != 0 and "SAI-09" in result.stderr:
                return
            if result.returncode == 0:
                cls._run(
                    "kubectl",
                    "-n",
                    "fs2-system",
                    "delete",
                    "pod",
                    "sai09-activation-probe",
                    "--ignore-not-found",
                )
            time.sleep(3)
        raise AssertionError("admission policy never became active in kind")

    def ephemeral_patch(self, image: str):
        patch = {
            "spec": {
                "ephemeralContainers": [
                    {
                        "name": "debugger-" + uuid.uuid4().hex[:6],
                        "image": image,
                        "command": ["sh"],
                        "stdin": True,
                        "tty": True,
                    }
                ]
            }
        }
        return self._run(
            "kubectl",
            "-n",
            "fs2-system",
            "patch",
            "pod",
            "sai09-debug-target",
            "--subresource=ephemeralcontainers",
            "--type=strategic",
            "-p",
            json.dumps(patch),
        )

    def test_pinned_allowlisted_debug_injection_is_admitted(self) -> None:
        # The governed debug workflow keeps working: digest-pinned image from
        # an allow-listed registry prefix, outside the platform repository.
        result = self.ephemeral_patch(DEBUG_IMAGE)
        self.assertEqual(
            result.returncode, 0,
            f"pinned allow-listed debug injection was denied: {result.stderr}",
        )
        pod = json.loads(
            self._kubectl(
                "-n", "fs2-system", "get", "pod", "sai09-debug-target", "-o", "json"
            ).stdout
        )
        images = [
            container["image"]
            for container in pod["spec"].get("ephemeralContainers", [])
        ]
        self.assertIn(DEBUG_IMAGE, images)

    def test_mutable_foreign_debug_injection_is_denied(self) -> None:
        # kubectl debug with evil.invalid/debug:latest must fail admission at
        # the pods/ephemeralcontainers subresource: unpinned AND foreign.
        result = self.ephemeral_patch(EVIL_IMAGE)
        self.assertNotEqual(
            result.returncode, 0,
            "mutable foreign debug injection was ADMITTED; the subresource "
            "is not governed",
        )
        self.assertIn("SAI-09", result.stderr)


if __name__ == "__main__":
    unittest.main()
