from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


build_images = load_module("build_images", ROOT / "build_images.py")
artifact_gate = load_module("artifact_gate", ROOT / "runtime" / "artifact_gate.py")
rfdiffusion_runner = load_module(
    "rfdiffusion_run_upstream", ROOT / "runtime" / "rfdiffusion_run_upstream.py"
)
pyrosetta_patch = load_module(
    "patch_bindcraft_pyrosetta", ROOT / "runtime" / "patch_bindcraft_pyrosetta.py"
)
bindcraft_runner = load_module(
    "bindcraft_runtime_entrypoint", ROOT / "runtime" / "bindcraft_runtime_entrypoint.py"
)


class ImageLockTests(unittest.TestCase):
    def test_lock_has_exact_distinct_sources_and_targets(self) -> None:
        lock = build_images.load_lock()
        by_id = {image["id"]: image for image in lock["images"]}
        self.assertEqual(
            by_id["bindcraft-academic"]["source"]["revision"],
            "7cd4ace1b7407adf66a50dfefa47de2270f5e4a9",
        )
        self.assertEqual(
            by_id["rfdiffusion"]["source"]["revision"],
            "9273ef67335acaf91df0150473a274759229cdf6",
        )
        self.assertEqual(
            by_id["proteinmpnn"]["source"]["revision"],
            "8907e6671bfbfc92303b5f79c4b5e6ce47cdef57",
        )
        fallback = by_id["freebindcraft-open-fallback"]
        self.assertFalse(fallback["equivalent_to_requested"])
        self.assertIn("non-equivalent", fallback["relationship"])
        self.assertEqual(len({image["target"] for image in lock["images"]}), 4)
        self.assertEqual(lock["adapter_commit"], "3475ce0ee8efdf2d3ccbcc65651ab11fe7cb34fe")

    def test_all_dockerfiles_pin_base_and_exclude_weight_downloads(self) -> None:
        lock = build_images.load_lock()
        base = lock["base"]["reference"]
        for image in lock["images"]:
            text = (ROOT / image["dockerfile"]).read_text(encoding="utf-8")
            self.assertIn(f"FROM {base}", text)
            self.assertIn(image["source"]["revision"], text)
            self.assertIn(image["source"]["archive_sha256"], text)
            self.assertIn('ai.nebius.fs2.runtime.weights="external"', text)
            self.assertNotIn("Base_ckpt.pt", text)
            self.assertNotIn("alphafold_params_2022-12-06.tar", text)

    def test_pyrosetta_is_runtime_only_and_never_a_build_input(self) -> None:
        dockerfile = (ROOT / "Dockerfile.bindcraft").read_text(encoding="utf-8").lower()
        self.assertNotIn("pip install pyrosetta", dockerfile)
        self.assertNotIn("pyrosetta.org", dockerfile)
        self.assertNotIn("secret", dockerfile)
        entrypoint = (ROOT / "runtime" / "runtime_entrypoint.py").read_text(encoding="utf-8")
        self.assertIn("/opt/fs2/academic/pyrosetta-bindcraft/site-packages", entrypoint)
        self.assertNotIn("FS2_PYROSETTA_WHEEL", entrypoint)
        self.assertNotIn("pip", entrypoint)
        wrapper = (ROOT / "runtime" / "bindcraft_runtime_entrypoint.py").read_text(encoding="utf-8")
        self.assertIn("tenant-private preinstalled PyRosetta tree", wrapper)
        self.assertNotIn("FS2_PYROSETTA_WHEEL", wrapper)

    def test_rfdiffusion_is_cuda12_with_matching_dgl_wheel(self) -> None:
        lock = build_images.load_lock()
        self.assertEqual(lock["base"]["cuda"], "12.1")
        text = (ROOT / "Dockerfile.rfdiffusion").read_text(encoding="utf-8")
        self.assertIn("dgl-2.3.0%2Bcu121-cp310", text)
        self.assertIn(lock["shared_sources"]["dgl_cuda121_wheel"]["sha256"], text)
        nvrtc = lock["shared_sources"]["nvidia_cuda_nvrtc_cuda121_wheel"]
        requirements = (ROOT / "requirements.rfdiffusion.txt").read_text(encoding="utf-8")
        self.assertIn(nvrtc["url"], requirements)
        self.assertIn("#sha256=" + nvrtc["sha256"], requirements)
        self.assertIn("pandas==2.2.3", requirements)
        self.assertIn("pydantic==2.9.2", requirements)
        self.assertIn("site-packages/nvidia/cuda_nvrtc/lib", text)
        runner = (ROOT / "runtime" / "rfdiffusion_run_upstream.py").read_text(encoding="utf-8")
        self.assertIn("hydra.run.dir=/tmp/fs2-hydra", runner)
        self.assertIn("hydra.job.chdir=False", runner)
        self.assertNotIn("cuda11", text.lower())

    def test_bindcraft_uses_locked_cuda12_jaxlib_and_hopper_compatible_cusparse(self) -> None:
        lock = build_images.load_lock()
        requirements = (ROOT / "requirements.bindcraft.txt").read_text(encoding="utf-8")
        wheel = lock["shared_sources"]["jaxlib_cuda12_cudnn89_wheel"]
        self.assertIn(wheel["url"], requirements)
        self.assertIn("#sha256=" + wheel["sha256"], requirements)
        self.assertNotIn("jax[cuda12]", requirements)
        self.assertNotIn("nvidia-cublas", requirements)
        for component in (
            "nvidia_cusparse_cuda121_wheel",
            "nvidia_nvjitlink_cuda121_wheel",
            "nvidia_cuda_nvcc_cuda121_wheel",
        ):
            pinned = lock["shared_sources"][component]
            self.assertIn(pinned["url"], requirements)
            self.assertIn("#sha256=" + pinned["sha256"], requirements)
        for name in ("Dockerfile.bindcraft", "Dockerfile.freebindcraft"):
            dockerfile = (ROOT / name).read_text(encoding="utf-8")
            self.assertIn("site-packages/nvidia/cusparse/lib", dockerfile)
            self.assertIn("site-packages/nvidia/nvjitlink/lib", dockerfile)
            self.assertIn("site-packages/torch/lib", dockerfile)
            self.assertIn("ln -sfn", dockerfile)

    def test_native_bindcraft_patches_exact_pyrosetta_2026_interface(self) -> None:
        dockerfile = (ROOT / "Dockerfile.bindcraft").read_text(encoding="utf-8")
        self.assertIn("libgfortran5=${LIBGFORTRAN_VERSION}", dockerfile)
        self.assertIn("patch_bindcraft_pyrosetta.py", dockerfile)
        with tempfile.TemporaryDirectory() as name:
            source = Path(name) / "pyrosetta_utils.py"
            source.write_text(
                pyrosetta_patch.IMPORT + "\n" + pyrosetta_patch.CALL + "\n",
                encoding="utf-8",
            )
            with mock.patch.object(sys, "argv", ["patch", str(source)]):
                pyrosetta_patch.main()
            patched = source.read_text(encoding="utf-8")
            self.assertIn(pyrosetta_patch.PATCHED_IMPORT, patched)
            self.assertIn(pyrosetta_patch.PATCHED_CALL, patched)

    def test_rebuild_tags_preserve_source_revision_without_overwriting_failed_canary(self) -> None:
        lock = build_images.load_lock()
        by_id = {image["id"]: image for image in lock["images"]}
        for image_id in ("bindcraft-academic", "freebindcraft-open-fallback"):
            image = by_id[image_id]
            expected = {"bindcraft-academic": "-cuda121-r11", "freebindcraft-open-fallback": "-cuda121-r6", "rfdiffusion": "-cuda121-r6"}.get(image_id, "")
            self.assertEqual(image["build_tag_suffix"], expected)
            self.assertTrue(image["target"].endswith(image["source"]["revision"] + expected))
            self.assertIn("@sha256:", image["supersedes"])

    def test_corrected_adapter_interfaces_are_consumed_as_build_context(self) -> None:
        self.assertEqual(
            build_images.ADAPTER_CONTEXT,
            build_images.REPOSITORY_ROOT / "models" / "structure" / "runtime",
        )
        self.assertTrue((build_images.ADAPTER_CONTEXT / "rfdiffusion/bin/rfdiffusion-batch").is_file())
        self.assertTrue((build_images.ADAPTER_CONTEXT / "bindcraft-native/bin/bindcraft-batch").is_file())
        bindcraft = (ROOT / "Dockerfile.bindcraft").read_text(encoding="utf-8")
        rfdiffusion = (ROOT / "Dockerfile.rfdiffusion").read_text(encoding="utf-8")
        self.assertIn("bindcraft-native/bin/bindcraft-batch", bindcraft)
        self.assertNotIn("verify-academic-access", bindcraft)
        self.assertIn("rfdiffusion/bin/rfdiffusion-batch", rfdiffusion)
        self.assertIn("3475ce0ee8efdf2d3ccbcc65651ab11fe7cb34fe", bindcraft)
        self.assertIn("3475ce0ee8efdf2d3ccbcc65651ab11fe7cb34fe", rfdiffusion)

    def test_typed_rfdiffusion_runner_maps_generated_and_motif_contigs(self) -> None:
        self.assertEqual(
            rfdiffusion_runner.typed_contigs([
                {"kind": "motif", "chain": "A", "start": 1, "end": 10},
                {"kind": "generated", "minimum_length": 66, "maximum_length": 66},
            ]),
            "A1-10/0 66-66",
        )
        with self.assertRaisesRegex(SystemExit, "bounds are invalid"):
            rfdiffusion_runner.typed_contigs([
                {"kind": "generated", "minimum_length": 80, "maximum_length": 70}
            ])

    def test_one_shot_gpu_smoke_bypasses_only_jax_shutdown_hooks(self) -> None:
        entrypoint = (ROOT / "runtime" / "runtime_entrypoint.py").read_text(encoding="utf-8")
        self.assertIn("sys.stdout.flush()", entrypoint)
        self.assertIn("os._exit(0)", entrypoint)
        self.assertIn("os.execvp(sys.argv[1]", entrypoint)

    def test_colabdesign_proteinmpnn_weights_are_external(self) -> None:
        entrypoint = (ROOT / "runtime" / "runtime_entrypoint.py").read_text(encoding="utf-8")
        self.assertIn('".pkl"', entrypoint)
        for name in ("Dockerfile.bindcraft", "Dockerfile.freebindcraft"):
            text = (ROOT / name).read_text(encoding="utf-8")
            self.assertIn("colabdesign/mpnn/weights_soluble", text)
            self.assertIn("colabdesign/mpnn/weights", text)

    def test_native_semantic_manifest_uses_canonical_private_tree_without_fs_group(self) -> None:
        manifest = (ROOT / "kubernetes" / "bindcraft-semantic-pod.yaml").read_text(encoding="utf-8")
        self.assertIn("mountPath: /opt/fs2/academic/pyrosetta-bindcraft/site-packages", manifest)
        self.assertIn("subPath: pyrosetta-bindcraft/site-packages", manifest)
        self.assertIn("supplementalGroups: [65532]", manifest)
        self.assertNotIn("fsGroup:", manifest)
        self.assertNotIn("academic-access-gate", manifest)
        self.assertNotIn("access-receipt", manifest)

    def test_h100_evidence_records_full_bindcraft_path_and_cleanup(self) -> None:
        evidence = json.loads((ROOT / "evidence" / "h100-semantic-validation.json").read_text())
        runs = {run["model"]: run for run in evidence["runs"]}
        self.assertEqual(set(runs), {"rfdiffusion", "proteinmpnn", "bindcraft-native-pyrosetta"})
        bindcraft = runs["bindcraft-native-pyrosetta"]
        self.assertEqual(bindcraft["semantic_workflow"]["result"], "passed")
        self.assertTrue(bindcraft["semantic_workflow"]["pyrosetta_relaxation_and_scoring"])
        self.assertEqual(bindcraft["semantic_workflow"]["accepted_candidates"], 1)
        self.assertEqual(bindcraft["independent_adapter_validation"]["status"], "passed")
        self.assertIsNone(bindcraft["pod"]["security_context"]["fs_group"])
        self.assertEqual(bindcraft["pod"]["security_context"]["supplemental_groups"], [65532])
        self.assertEqual(
            bindcraft["private_pyrosetta"]["tree_manifest_sha256"],
            "a93d68e198c81cbb87926e012dff6b50a73e99d9a41261e65f73d264c792aa8d",
        )
        self.assertFalse(bindcraft["private_pyrosetta"]["per_request_receipt_gate"])
        self.assertEqual(bindcraft["resource_measurements"]["cgroup_oom_kill_events"], 0)
        self.assertTrue(bindcraft["cleanup"]["pod_deleted"])
        self.assertEqual(bindcraft["cleanup"]["task_owned_pods_or_jobs_remaining"], 0)

    def test_bindcraft_wrapper_uses_shared_newline_canonicalization(self) -> None:
        value = {"z": 1, "a": "two"}
        self.assertEqual(bindcraft_runner._canonical_bytes(value), b'{"a":"two","z":1}\n')

    def test_existing_target_is_never_overwritten(self) -> None:
        image = build_images.load_lock()["images"][0]
        with mock.patch.object(build_images, "inspect_target", return_value="sha256:" + "1" * 64):
            with self.assertRaisesRegex(build_images.BuildError, "refusing to overwrite"):
                build_images.ensure_absent(image)

    def test_gpu_smoke_runs_real_torch_and_jax_kernels_without_preallocation(self) -> None:
        entrypoint = (ROOT / "runtime" / "runtime_entrypoint.py").read_text(encoding="utf-8")
        self.assertIn('XLA_PYTHON_CLIENT_PREALLOCATE", "false"', entrypoint)
        self.assertIn('torch.ones(1, device="cuda")', entrypoint)
        self.assertIn('jax.devices("gpu")', entrypoint)

    def test_partial_publication_preserves_existing_receipts(self) -> None:
        lock = build_images.load_lock()
        existing = {
            "schema": "fs2.nebius.ai/cancer-runtime-image-publication/v1",
            "images": [{"id": lock["images"][0]["id"], "digest": "sha256:" + "1" * 64}],
        }
        with tempfile.TemporaryDirectory() as name:
            receipt_path = Path(name) / "receipt.json"
            receipt_path.write_text(json.dumps(existing), encoding="utf-8")
            second = {"id": lock["images"][1]["id"], "digest": "sha256:" + "2" * 64}
            with mock.patch.object(build_images, "RECEIPT_PATH", receipt_path):
                build_images.write_receipt(lock, [second])
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
            self.assertEqual([record["id"] for record in receipt["images"]], [
                lock["images"][0]["id"],
                lock["images"][1]["id"],
            ])

    def test_publisher_requires_attestation_capable_builder(self) -> None:
        with mock.patch.object(
            build_images,
            "run",
            return_value=mock.Mock(returncode=0, stdout="Name: test\nDriver: docker\n"),
        ):
            with self.assertRaisesRegex(build_images.BuildError, "cannot emit OCI attestations"):
                build_images.ensure_publish_builder()

    def test_published_verifier_requires_sbom_and_provenance_predicates(self) -> None:
        image = build_images.load_lock()["images"][0]
        with mock.patch.object(build_images, "inspect_target", return_value="sha256:" + "1" * 64), \
             mock.patch.object(build_images, "run", return_value=mock.Mock(stdout="pulled")), \
             mock.patch.object(build_images, "smoke", return_value=[]), \
             mock.patch.object(
                 build_images,
                 "raw_manifest",
                 return_value=("2" * 64, 1, ["https://spdx.dev/Document"]),
             ):
            with self.assertRaisesRegex(build_images.BuildError, "SLSA provenance"):
                build_images.verify_published(build_images.load_lock(), image)


class ArtifactGateTests(unittest.TestCase):
    def manifest(self, root: Path, *, digest: str | None = None, relative: str = "weights.bin") -> Path:
        artifact = root / "weights.bin"
        artifact.write_bytes(b"immutable-weights")
        value = {
            "schema": artifact_gate.SCHEMA,
            "artifact_kind": "test-weights",
            "source_revision": "a" * 40,
            "files": [
                {
                    "path": relative,
                    "sha256": digest or hashlib.sha256(artifact.read_bytes()).hexdigest(),
                    "size_bytes": artifact.stat().st_size,
                }
            ],
        }
        manifest = root / "manifest.json"
        manifest.write_text(json.dumps(value), encoding="utf-8")
        return manifest

    def test_manifest_admits_exact_external_content(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            result = artifact_gate.verify_manifest(
                root,
                self.manifest(root),
                expected_kind="test-weights",
                expected_source_revision="a" * 40,
            )
            self.assertEqual(result["files"], 1)
            self.assertEqual(result["bytes"], len(b"immutable-weights"))

    def test_manifest_rejects_hash_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            with self.assertRaisesRegex(artifact_gate.ArtifactGateError, "does not match"):
                artifact_gate.verify_manifest(
                    root,
                    self.manifest(root, digest="0" * 64),
                    expected_kind="test-weights",
                    expected_source_revision="a" * 40,
                )

    def test_manifest_rejects_escape_and_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            manifest = self.manifest(root, relative="../weights.bin")
            with self.assertRaisesRegex(artifact_gate.ArtifactGateError, "unsafe"):
                artifact_gate.verify_manifest(
                    root,
                    manifest,
                    expected_kind="test-weights",
                    expected_source_revision="a" * 40,
                )
            (root / "link.bin").symlink_to(root / "weights.bin")
            value = json.loads(manifest.read_text())
            value["files"][0]["path"] = "link.bin"
            manifest.write_text(json.dumps(value))
            with self.assertRaisesRegex(artifact_gate.ArtifactGateError, "symlink"):
                artifact_gate.verify_manifest(
                    root,
                    manifest,
                    expected_kind="test-weights",
                    expected_source_revision="a" * 40,
                )

    def test_environment_gate_fails_closed(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(artifact_gate.ArtifactGateError, "incomplete"):
                artifact_gate.verify_from_environment()


if __name__ == "__main__":
    unittest.main()
