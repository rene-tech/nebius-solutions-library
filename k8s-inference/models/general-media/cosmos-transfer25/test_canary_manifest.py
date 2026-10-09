import os
import unittest
from pathlib import Path
from unittest import mock

import yaml

DOCUMENTS = list(yaml.safe_load_all(Path(__file__).with_name("canary-20260920.yaml").read_text()))
POD = next(row for row in DOCUMENTS if row["kind"] == "Pod")
CONTAINER = POD["spec"]["containers"][0]
SOURCE = compile(CONTAINER["command"][2], "canary-startup", "exec")


class CanaryManifestTests(unittest.TestCase):
    def test_normalizes_only_process_environment_preserving_upstream_launch(self):
        with mock.patch("os.environ", {"NGC_API_KEY": "nvapi-synthetic\n"}), mock.patch("os.execv") as launch:
            exec(SOURCE, {})  # noqa: S102 - fixed checked-in launcher, mocked execv
            self.assertEqual(os.environ["NGC_API_KEY"], "nvapi-synthetic")
            launch.assert_called_once_with(
                "/opt/nvidia/nvidia_entrypoint.sh",
                ["/opt/nvidia/nvidia_entrypoint.sh", "/bin/bash", "-c", "$SERVER_START_SCRIPT_PATH"],
            )

    def test_invalid_credentials_fail_before_launch_without_echoing_value(self):
        for credential in ("", "\n", "nvapi-synthetic\ninternal", "nvapi-synthetic\x00", "nvapi-synthetic\u00e9"):
            with (
                self.subTest(credential_kind=repr(credential)),
                mock.patch("os.environ", {"NGC_API_KEY": credential}),
                mock.patch("os.execv") as launch,
            ):
                with self.assertRaises(SystemExit) as error:
                    exec(SOURCE, {})  # noqa: S102 - fixed checked-in launcher, mocked execv
                self.assertEqual(str(error.exception), "NGC credential format is invalid; value suppressed")
                launch.assert_not_called()

    def test_bounded_private_single_gpu_profile(self):
        self.assertEqual(POD["spec"]["activeDeadlineSeconds"], 7200)
        self.assertEqual(CONTAINER["resources"]["limits"]["nvidia.com/gpu"], "1")
        self.assertEqual(CONTAINER["resources"]["requests"]["nvidia.com/gpu"], "1")
        self.assertIn("kubernetes.io/hostname", POD["spec"]["nodeSelector"])
        self.assertNotIn("Service", {row["kind"] for row in DOCUMENTS})
        self.assertIn("@sha256:", CONTAINER["image"])
        env = {row["name"]: row for row in CONTAINER["env"]}
        self.assertEqual(
            env["NIM_MODEL_PROFILE"]["value"], "e74ebba119c8a196dca12cac66aa1b5323291048a855fe02ceb6b664f334c672"
        )
        self.assertIn("secretKeyRef", env["NGC_API_KEY"]["valueFrom"])


if __name__ == "__main__":
    unittest.main()
