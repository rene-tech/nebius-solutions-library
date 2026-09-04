"""Qualification jobs must keep model and request bytes on distinct roots."""

from __future__ import annotations

import unittest

from qualification import render_job


class QualificationRendererTests(unittest.TestCase):
    def test_checkpoint_and_request_planes_are_distinct(self) -> None:
        generation = "7f34c945e580dbf5ba96596dcd325150f6452f7a76ee06a3784b2891a9d4c03c"
        job = render_job.render(
            name="rfdiffusion-split-root-test",
            namespace="fs2-models",
            image="registry.example/rfdiffusion@sha256:" + "a" * 64,
            accelerator_class="nvidia-h100-sxm5-80gb",
            local_queue="inference-models",
            planes=[],
            generation_planes=[
                ("rfdiffusion-base-checkpoint", "rfdiffusion-base-checkpoint", generation)
            ],
            input_planes=[("targets", "target-config")],
            verifier_config_map="",
            run_claim="run-claim",
            run_sub_path="rfdiffusion/tests",
            request_config_map="request-config",
            cache_level="artifact-local",
            checkpoint_artifact_id="artifact.rfdiffusion.base-ckpt",
            gpu_count=1,
            timeout_seconds=600,
        )
        container = job["spec"]["template"]["spec"]["containers"][0]
        command = container["command"]
        self.assertEqual(
            command[command.index("--artifact-root") + 1],
            "/opt/fs2/artifacts/rfdiffusion-base-checkpoint",
        )
        self.assertEqual(
            command[command.index("--input-artifact-root") + 1],
            "/opt/fs2/inputs",
        )
        mounts = {item["name"]: item["mountPath"] for item in container["volumeMounts"]}
        self.assertEqual(mounts["trees"], "/opt/fs2/artifacts/rfdiffusion-base-checkpoint")
        self.assertEqual(mounts["input-0"], "/opt/fs2/inputs/targets")

    def test_checkpoint_plane_is_required(self) -> None:
        with self.assertRaisesRegex(SystemExit, "checkpoint artifact plane"):
            render_job.render(
                name="missing-checkpoint",
                namespace="fs2-models",
                image="registry.example/rfdiffusion@sha256:" + "a" * 64,
                accelerator_class="nvidia-h100-sxm5-80gb",
                local_queue="",
                planes=[],
                generation_planes=[],
                input_planes=[],
                verifier_config_map="",
                run_claim="run-claim",
                run_sub_path="rfdiffusion/tests",
                request_config_map="request-config",
                cache_level="artifact-local",
                checkpoint_artifact_id="artifact.rfdiffusion.base-ckpt",
                gpu_count=1,
                timeout_seconds=600,
            )


if __name__ == "__main__":
    unittest.main()
