import argparse
import contextlib
import importlib.util
import io
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("medical_media_probe", Path(__file__).with_name("probe.py"))
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)


class ManifestTests(unittest.TestCase):
    def render(self, model, action="create", image=None, node=None):
        with tempfile.TemporaryDirectory() as directory:
            args = argparse.Namespace(kubeconfig="/not-used", context="k8s-inference-h100", model=model,
                repetition=2, output=Path(directory), action=action, image=image, node=node)
            with patch.object(probe.subprocess, "run", return_value=SimpleNamespace(stdout="", returncode=0)), contextlib.redirect_stdout(io.StringIO()):
                probe.run(args)
            return json.loads((Path(directory) / "pod-manifest.json").read_text()), json.loads((Path(directory) / "pvc-manifest.json").read_text())

    def test_three_runtime_templates_resolve_portable_abi(self):
        for model in ("sdxl", "nv-segment-ct", "nv-reason-cxr-3b"):
            with self.subTest(model=model):
                pod, pvc = self.render(model)
                self.assertEqual(pvc["spec"]["storageClassName"], "csi-mounted-fs-path-sc")
                self.assertEqual(pvc["spec"]["accessModes"], ["ReadWriteMany"])
                self.assertEqual(pod["spec"]["nodeSelector"]["accelerator.fs2.nebius/class"], "nvidia-h100-sxm5-80gb")
                self.assertNotIn("deployment-profile-abi-v1", json.dumps(pod))
                self.assertNotIn("sm103", json.dumps(pod))
                self.assertIn("driver-580.159.04-sm90", json.dumps(pod))

    def test_evo_rejects_unpinned_image(self):
        with self.assertRaisesRegex(ValueError, "immutable"):
            self.render("evo2-40b")

    def test_evo_has_two_gpu_visibility_and_own_block_storage(self):
        pod, pvc = self.render("evo2-40b", image=probe.REGISTRY + "/evo2-runtime@sha256:" + "a" * 64)
        self.assertEqual(pvc["spec"]["storageClassName"], "compute-csi-default-sc")
        self.assertEqual(pvc["spec"]["accessModes"], ["ReadWriteOnce"])
        main = next(c for c in pod["spec"]["containers"] if c["name"] == "model")
        self.assertEqual(main["resources"]["limits"]["nvidia.com/gpu"], "2")
        self.assertNotIn("CUDA_VISIBLE_DEVICES", {e["name"] for e in main["env"]})
        self.assertIn("evo2_serve.py", main["command"][-1])

    def test_staging_has_no_gpu_request(self):
        pod, pvc = self.render("evo2-40b", action="stage")
        self.assertNotIn("initContainers", pod["spec"])
        for container in pod["spec"]["containers"]:
            self.assertNotIn("nvidia.com/gpu", container["resources"]["limits"])
        self.assertEqual(pvc["spec"]["resources"]["requests"]["storage"], "192Gi")

    def test_mount_holder_is_bounded_read_only_without_prewarming_or_gpu(self):
        pod, _ = self.render("evo2-40b", action="hold-cache", node="qualified-node")
        self.assertEqual(pod["spec"]["activeDeadlineSeconds"], 7200)
        self.assertEqual(pod["spec"]["nodeSelector"]["kubernetes.io/hostname"], "qualified-node")
        self.assertNotIn("initContainers", pod["spec"])
        holder = pod["spec"]["containers"][0]
        self.assertNotIn("nvidia.com/gpu", holder["resources"]["limits"])
        self.assertTrue(holder["volumeMounts"][0]["readOnly"])
        self.assertTrue(pod["spec"]["volumes"][0]["persistentVolumeClaim"]["readOnly"])
        self.assertNotIn("open(", holder["command"][-1])
        self.assertIn("time.sleep(7200)", holder["command"][-1])

    def test_release_owned_cache_is_consumed_without_pvc_apply(self):
        with tempfile.TemporaryDirectory() as directory:
            args = argparse.Namespace(kubeconfig="/not-used", context="k8s-inference-h100", model="evo2-40b",
                repetition=9, output=Path(directory), action="create", node="qualified-node",
                image=probe.REGISTRY + "/evo2-runtime@sha256:" + "a" * 64,
                existing_cache_pvc="evo2-40b-cache-rwx-ecc3e914", cache_cohort="shared-filesystem-after-copy-and-hash")
            pvc = {"metadata": {"name": args.existing_cache_pvc}, "spec": {"accessModes": ["ReadWriteMany"]}, "status": {"phase": "Bound"}}
            with patch.object(probe.subprocess, "run", return_value=SimpleNamespace(stdout=json.dumps(pvc), returncode=0)) as run, contextlib.redirect_stdout(io.StringIO()):
                probe.run(args)
            pod = json.loads((Path(directory) / "pod-manifest.json").read_text())
            self.assertFalse(any("apply" in call.args[0] for call in run.call_args_list))
            self.assertEqual(pod["metadata"]["annotations"]["fs2.nebius/cache-cohort"], args.cache_cohort)
            self.assertTrue(all(v["persistentVolumeClaim"]["claimName"] == args.existing_cache_pvc
                for v in pod["spec"]["volumes"] if "persistentVolumeClaim" in v))


if __name__ == "__main__":
    unittest.main()
