from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import sys
import tempfile
import types
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
freebindcraft_runner = load_module(
    "freebindcraft_runtime_entrypoint", ROOT / "runtime" / "freebindcraft_runtime_entrypoint.py"
)
tree_identity = load_module("tree_identity", ROOT / "runtime" / "tree_identity.py")
renderer = load_module("render_semantic_job", ROOT / "qualification" / "render_semantic_job.py")


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
        self.assertIn("importlib.metadata.distribution(\"pyrosetta\")", wrapper)
        self.assertIn("PYROSETTA_EXPECTED_VERSION", wrapper)
        self.assertIn("version_api_status", wrapper)

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
            expected = {"bindcraft-academic": "-cuda121-r16", "freebindcraft-open-fallback": "-cuda121-r9", "rfdiffusion": "-cuda121-r6"}.get(image_id, "")
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
        self.assertTrue((build_images.ADAPTER_CONTEXT / "bindcraft-open/bin/freebindcraft-batch").is_file())
        bindcraft = (ROOT / "Dockerfile.bindcraft").read_text(encoding="utf-8")
        freebindcraft = (ROOT / "Dockerfile.freebindcraft").read_text(encoding="utf-8")
        rfdiffusion = (ROOT / "Dockerfile.rfdiffusion").read_text(encoding="utf-8")
        self.assertIn("bindcraft-native/bin/bindcraft-batch", bindcraft)
        self.assertNotIn("verify-academic-access", bindcraft)
        self.assertIn("bindcraft-open/bin/freebindcraft-batch", freebindcraft)
        self.assertIn("runtime/freebindcraft_runtime_entrypoint.py", freebindcraft)
        self.assertIn("rfdiffusion/bin/rfdiffusion-batch", rfdiffusion)
        self.assertIn("3475ce0ee8efdf2d3ccbcc65651ab11fe7cb34fe", bindcraft)
        self.assertIn("3475ce0ee8efdf2d3ccbcc65651ab11fe7cb34fe", freebindcraft)
        self.assertIn("3475ce0ee8efdf2d3ccbcc65651ab11fe7cb34fe", rfdiffusion)

    def test_open_bindcraft_has_pinned_freesasa_and_real_scoring_contract(self) -> None:
        lock = build_images.load_lock()
        dockerfile = (ROOT / "Dockerfile.freebindcraft").read_text(encoding="utf-8")
        for component in (
            "freesasa_2_2_1_sdist",
            "cython_3_0_12_cp310_wheel",
            "openmm_8_2_0_cuda12_py310_conda",
            "libstdcxx_ng_12_4_0_conda",
            "libgcc_ng_12_4_0_conda",
        ):
            pinned = lock["shared_sources"][component]
            self.assertIn(pinned["url"], dockerfile)
            self.assertIn(pinned["sha256"], dockerfile)
        self.assertEqual(freebindcraft_runner.BACKEND_ID, "freebindcraft-v1-0-5")
        self.assertEqual(
            freebindcraft_runner.SOURCE_REVISION,
            "28c43fc48942eebd7918f504e9812c5c17bb3411",
        )
        source = (ROOT / "runtime" / "freebindcraft_runtime_entrypoint.py").read_text(encoding="utf-8")
        self.assertIn('sasa_engine="freesasa"', source)
        self.assertIn('getPlatformByName("CUDA")', source)
        self.assertIn('"scoring_engine": "openmm-freesasa"', source)
        self.assertIn("--no-pyrosetta", source)
        self.assertIn("FS2 hard trajectory bound", source)
        self.assertIn("request[\"parameters\"][\"max_trajectories_per_shard\"]", source)
        self.assertIn("DAlphaBall.gcc", dockerfile)
        self.assertIn("lib/plugins/libOpenMMCUDA.so", dockerfile)

    def test_open_bindcraft_enforces_request_trajectory_bound(self) -> None:
        generic_utils = types.ModuleType("functions.generic_utils")
        generic_utils.check_n_trajectories = mock.Mock(return_value=False)
        functions = types.ModuleType("functions")
        functions.generic_utils = generic_utils
        functions.check_n_trajectories = generic_utils.check_n_trajectories
        numpy = types.ModuleType("numpy")
        numpy.random = types.SimpleNamespace(randint=mock.Mock())
        observed: list[bool] = []

        def upstream(_: str, *, run_name: str) -> None:
            self.assertEqual(run_name, "__main__")
            observed.extend([
                functions.check_n_trajectories({}, {}),
                functions.check_n_trajectories({}, {}),
            ])

        with mock.patch.dict(sys.modules, {
            "functions": functions,
            "functions.generic_utils": generic_utils,
            "numpy": numpy,
        }), mock.patch.object(freebindcraft_runner.runpy, "run_path", side_effect=upstream):
            freebindcraft_runner._run_upstream(
                Path("settings.json"), Path("filters.json"), Path("advanced.json"), 42, 1,
            )

        self.assertEqual(observed, [False, True])
        self.assertIs(functions.check_n_trajectories, generic_utils.check_n_trajectories)

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

    def test_native_semantic_run_is_generated_rather_than_hand_written(self) -> None:
        # The hand-written native Pod was removed: it pinned a superseded digest
        # and created an empty colabdesign weights_soluble package, so it looked
        # like a run against the soluble MPNN tree while reading no weights.
        self.assertFalse((ROOT / "kubernetes" / "bindcraft-semantic-pod.yaml").exists())
        self.assertTrue((ROOT / "qualification" / "render_semantic_job.py").is_file())

    def test_h100_evidence_records_full_bindcraft_paths_and_cleanup(self) -> None:
        evidence = json.loads((ROOT / "evidence" / "h100-semantic-validation.json").read_text())
        runs = {run["model"]: run for run in evidence["runs"]}
        self.assertEqual(set(runs), {
            "rfdiffusion",
            "proteinmpnn",
            "bindcraft-native-pyrosetta",
            "freebindcraft-open-fallback",
        })
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

        fallback = runs["freebindcraft-open-fallback"]
        self.assertEqual(fallback["relationship"], "derived-open-non-equivalent-fallback")
        self.assertEqual(fallback["interface"]["wrapper_path"], "/opt/fs2/bin/freebindcraft-batch")
        self.assertEqual(
            fallback["interface"]["runtime_runner_path"],
            "/opt/fs2/freebindcraft/runtime_entrypoint.py",
        )
        self.assertEqual(fallback["semantic_workflow"]["result"], "passed")
        self.assertFalse(fallback["semantic_workflow"]["pyrosetta_relaxation_and_scoring"])
        self.assertTrue(fallback["semantic_workflow"]["openmm_cuda_scoring"])
        self.assertTrue(fallback["semantic_workflow"]["freesasa_interface_scoring"])
        self.assertEqual(fallback["semantic_workflow"]["accepted_candidates"], 1)
        self.assertEqual(fallback["independent_adapter_validation"]["status"], "passed")
        self.assertEqual(fallback["lane_constraints"]["pyrosetta"], "forbidden")
        self.assertEqual(fallback["resource_measurements"]["cgroup_oom_kill_events"], 0)
        self.assertTrue(fallback["cleanup"]["pod_deleted"])
        self.assertEqual(fallback["cleanup"]["task_owned_pods_or_jobs_remaining"], 0)

    def test_open_semantic_manifest_is_digest_pinned_and_has_no_academic_mount(self) -> None:
        manifest = (ROOT / "kubernetes" / "freebindcraft-semantic-pod.yaml").read_text(
            encoding="utf-8"
        )
        digest = "sha256:6d44aba5780c2b74985db037045e06e732f4e867795d33a6313c5faa95bd9e30"
        self.assertIn("freebindcraft@" + digest, manifest)
        self.assertIn("capacity.fs2.nebius/source: capacity-block", manifest)
        self.assertNotIn("pyrosetta-bindcraft", manifest)
        self.assertNotIn("supplementalGroups", manifest)
        self.assertNotIn("fsGroup:", manifest)

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


class TreeIdentityTests(unittest.TestCase):
    def nested(self, root: Path) -> None:
        (root / "pkg").mkdir()
        (root / "pkg" / "__init__.py").write_bytes(b"x = 1\n")
        (root / "pkg" / "big.so").write_bytes(b"\x00\x01\x02" * 1024)
        (root / "top_level.txt").write_bytes(b"pkg\n")

    def flat(self, root: Path) -> None:
        (root / "__init__.py").write_bytes(b"")
        (root / "v_48_020.pkl").write_bytes(b"weights" * 512)

    def test_tree_manifest_reproduces_the_installer_identity(self) -> None:
        installer = load_module(
            "install_tree", ROOT.parents[3] / "academic-assets" / "scripts" / "install_tree.py"
        )
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            self.nested(root)
            mine = tree_identity.tree_manifest(root)
            theirs = installer.tree_manifest(root)
            self.assertEqual(tree_identity.TREE_MANIFEST_ALGORITHM, installer.TREE_MANIFEST_ALGORITHM)
            self.assertEqual(mine["tree_manifest_sha256"], theirs["tree_manifest_sha256"])
            self.assertEqual(mine["tree_total_bytes"], theirs["tree_total_bytes"])
            self.assertEqual(mine["file_count"], theirs["file_count"])

    def test_verify_tree_rejects_a_single_changed_byte(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            self.nested(root)
            expected = tree_identity.tree_manifest(root)["tree_manifest_sha256"]
            receipt = tree_identity.verify_tree(
                root, artifact_id="licensed-tree", expected_tree_manifest_sha256=expected
            )
            self.assertEqual(receipt["verification"], "full-content-tree-manifest")
            self.assertEqual(receipt["file_count"], 3)
            (root / "pkg" / "__init__.py").write_bytes(b"x = 2\n")
            with self.assertRaisesRegex(tree_identity.TreeIdentityError, "not the immutable tree"):
                tree_identity.verify_tree(
                    root, artifact_id="licensed-tree", expected_tree_manifest_sha256=expected
                )

    def test_flat_inventory_binds_content_and_refuses_unsafe_trees(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            self.flat(root)
            expected = tree_identity.flat_tree_inventory(root)["inventory_sha256"]
            receipt = tree_identity.verify_flat_tree(
                root, artifact_id="mpnn-weights", expected_inventory_sha256=expected
            )
            self.assertEqual(receipt["entry_count"], 2)
            (root / "v_48_020.pkl").write_bytes(b"swapped" * 512)
            with self.assertRaisesRegex(tree_identity.TreeIdentityError, "not the immutable tree"):
                tree_identity.verify_flat_tree(
                    root, artifact_id="mpnn-weights", expected_inventory_sha256=expected
                )
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            self.flat(root)
            (root / "nested").mkdir()
            with self.assertRaisesRegex(tree_identity.TreeIdentityError, "only regular files"):
                tree_identity.flat_tree_inventory(root)

    def test_identity_digests_must_be_lowercase_sha256(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            self.flat(root)
            with self.assertRaisesRegex(tree_identity.TreeIdentityError, "lowercase SHA-256"):
                tree_identity.verify_flat_tree(
                    root, artifact_id="mpnn-weights", expected_inventory_sha256="ABC"
                )


class ExternalTreeAdmissionTests(unittest.TestCase):
    def build(self, root: Path) -> dict[str, Path]:
        roots: dict[str, Path] = {}
        licensed = root / "site-packages"
        (licensed / "pyrosetta").mkdir(parents=True)
        (licensed / "pyrosetta" / "__init__.py").write_bytes(b"# licensed\n")
        roots[bindcraft_runner.PYROSETTA_ROLE] = licensed
        for role in (
            bindcraft_runner.AF2_PARAMS_ROLE,
            bindcraft_runner.MPNN_VANILLA_ROLE,
            bindcraft_runner.MPNN_SOLUBLE_ROLE,
        ):
            flat = root / role
            flat.mkdir()
            (flat / "__init__.py").write_bytes(b"")
            (flat / "payload.bin").write_bytes(role.encode())
            roots[role] = flat
        return roots

    GENERATION = "sha256:" + "c" * 64

    def declaration(
        self,
        root: Path,
        roots: dict[str, Path],
        *,
        pyrosetta_digest: str | None = None,
        generation: str | None = None,
    ) -> Path:
        trees = []
        for role, tree_root in roots.items():
            if role in bindcraft_runner.NESTED_TREE_ROLES:
                digest = pyrosetta_digest or bindcraft_runner.PYROSETTA_TREE_MANIFEST_SHA256
            else:
                digest = tree_identity.flat_tree_inventory(tree_root)["inventory_sha256"]
            trees.append({"role": role, "artifact_id": role, "root": str(tree_root), "sha256": digest})
        path = root / "external-trees.json"
        path.write_text(
            json.dumps({
                "schema": bindcraft_runner.EXTERNAL_TREE_ADMISSION_SCHEMA,
                "generation": self.GENERATION if generation is None else generation,
                "trees": trees,
            }),
            encoding="utf-8",
        )
        return path

    def marker(self, root: Path, *, generation: str | None = None) -> dict[str, object]:
        path = root / "runtime-localization.json"
        value: dict[str, object] = {"schema": "fs2.nebius.ai/runtime-localization-marker/v1"}
        if generation is not False:
            value["generation"] = self.GENERATION if generation is None else generation
        path.write_text(json.dumps(value), encoding="utf-8")
        return bindcraft_runner._localization_marker(str(path))

    def fake_packages(self, roots: dict[str, Path]) -> dict[str, types.ModuleType]:
        modules: dict[str, types.ModuleType] = {}
        for role, module_name in bindcraft_runner.MPNN_PACKAGE_BY_ROLE.items():
            module = types.ModuleType(module_name)
            module.__file__ = str(roots[role] / "__init__.py")
            modules[module_name] = module
        return modules

    def admit(self, path: Path, roots: dict[str, Path], marker: dict[str, object] | None = None):
        resolved = self.marker(path.parent) if marker is None else marker
        with mock.patch.dict(os.environ, {"FS2_BINDCRAFT_EXTERNAL_TREES": str(path)}):
            with mock.patch.dict(sys.modules, self.fake_packages(roots)):
                return bindcraft_runner._admit_external_trees(resolved)

    def pinned(self, roots: dict[str, Path]):
        """Pin the image to the fixture's licensed tree.

        The real pin names the 3.29 GB PyRosetta tree, which no unit test can
        materialize, so the pin is redirected at the small fixture tree. The pin
        itself is exercised by test_admission_rejects_a_foreign_licensed_tree_identity.
        """

        identity = tree_identity.tree_manifest(roots[bindcraft_runner.PYROSETTA_ROLE])
        return mock.patch.object(
            bindcraft_runner, "PYROSETTA_TREE_MANIFEST_SHA256", identity["tree_manifest_sha256"]
        )

    def test_admission_verifies_all_four_trees_by_content(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            roots = self.build(root)
            with self.pinned(roots):
                receipt = self.admit(self.declaration(root, roots), roots)
            self.assertEqual(set(receipt["trees"]), bindcraft_runner.REQUIRED_TREE_ROLES)
            self.assertEqual(
                receipt["trees"][bindcraft_runner.MPNN_SOLUBLE_ROLE]["package"],
                "colabdesign.mpnn.weights_soluble",
            )
            self.assertGreater(receipt["verified_bytes"], 0)

    def test_admission_rejects_a_foreign_licensed_tree_identity(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            roots = self.build(root)
            path = self.declaration(root, roots, pyrosetta_digest="0" * 64)
            with self.assertRaisesRegex(bindcraft_runner.ContractError, "licensed tree this image"):
                self.admit(path, roots)

    def test_admission_rejects_a_missing_role_and_a_swapped_tree(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            roots = self.build(root)
            path = self.declaration(root, roots)
            value = json.loads(path.read_text())
            value["trees"] = [
                tree for tree in value["trees"] if tree["role"] != bindcraft_runner.MPNN_VANILLA_ROLE
            ]
            path.write_text(json.dumps(value), encoding="utf-8")
            with self.assertRaisesRegex(bindcraft_runner.ContractError, "missing roles"):
                self.admit(path, roots)
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            roots = self.build(root)
            with self.pinned(roots):
                path = self.declaration(root, roots)
                (roots[bindcraft_runner.MPNN_SOLUBLE_ROLE] / "payload.bin").write_bytes(b"swapped")
                with self.assertRaisesRegex(bindcraft_runner.ContractError, "not the immutable tree"):
                    self.admit(path, roots)

    def test_admission_rejects_weights_that_the_model_would_not_load(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            roots = self.build(root)
            elsewhere = dict(roots)
            elsewhere[bindcraft_runner.MPNN_VANILLA_ROLE] = root / "unrelated"
            (root / "unrelated").mkdir()
            (root / "unrelated" / "__init__.py").write_bytes(b"")
            with self.pinned(roots):
                path = self.declaration(root, roots)
                with self.assertRaisesRegex(bindcraft_runner.ContractError, "does not resolve"):
                    self.admit(path, elsewhere)

    def test_admission_declaration_is_mandatory(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            marker = self.marker(root)
            with mock.patch.dict(
                os.environ, {"FS2_BINDCRAFT_EXTERNAL_TREES": str(root / "absent.json")}
            ):
                with self.assertRaisesRegex(bindcraft_runner.ContractError, "unavailable"):
                    bindcraft_runner._admit_external_trees(marker)

    def test_a_generation_that_rolled_after_scheduling_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            roots = self.build(root)
            with self.pinned(roots):
                path = self.declaration(root, roots, generation="sha256:" + "d" * 64)
                with self.assertRaisesRegex(
                    bindcraft_runner.ContractError, "not the generation this run was scheduled"
                ):
                    self.admit(path, roots)

    def test_a_marker_without_a_generation_still_admits_and_is_recorded(self) -> None:
        # The controller owns the marker's schema, so a marker that names no
        # generation is not an error; it just cannot corroborate the trees.
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            roots = self.build(root)
            with self.pinned(roots):
                receipt = self.admit(
                    self.declaration(root, roots), roots, self.marker(root, generation=False)
                )
            self.assertEqual(receipt["localization_generation"], self.GENERATION)


class LocalizationMarkerTests(unittest.TestCase):
    def write(self, root: Path, value: object) -> Path:
        path = root / "runtime-localization.json"
        path.write_text(json.dumps(value), encoding="utf-8")
        return path

    def test_marker_is_read_hashed_and_recorded(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            path = self.write(root, {"generation": "sha256:" + "e" * 64})
            marker = bindcraft_runner._localization_marker(str(path))
            self.assertEqual(marker["path"], str(path))
            self.assertEqual(marker["sha256"], hashlib.sha256(path.read_bytes()).hexdigest())
            self.assertEqual(marker["generation"], "sha256:" + "e" * 64)

    def test_marker_must_be_an_absolute_path_to_a_readable_json_object(self) -> None:
        with self.assertRaisesRegex(bindcraft_runner.ContractError, "absolute path"):
            bindcraft_runner._localization_marker("relative/runtime-localization.json")
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            with self.assertRaisesRegex(bindcraft_runner.ContractError, "unavailable"):
                bindcraft_runner._localization_marker(str(root / "absent.json"))
            broken = root / "runtime-localization.json"
            broken.write_text("{not json", encoding="utf-8")
            with self.assertRaisesRegex(bindcraft_runner.ContractError, "unreadable"):
                bindcraft_runner._localization_marker(str(broken))
            with self.assertRaisesRegex(bindcraft_runner.ContractError, "one JSON object"):
                bindcraft_runner._localization_marker(str(self.write(root, ["a"])))

    def test_marker_argv_and_environment_must_agree(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            path = self.write(root, {"generation": "g1"})
            with mock.patch.dict(
                os.environ, {bindcraft_runner.RUNTIME_LOCALIZATION_MARKER_ENV: str(root / "other.json")}
            ):
                with self.assertRaisesRegex(bindcraft_runner.ContractError, "argv and environment"):
                    bindcraft_runner._localization_marker(str(path))
            with mock.patch.dict(
                os.environ, {bindcraft_runner.RUNTIME_LOCALIZATION_MARKER_ENV: str(path)}
            ):
                self.assertEqual(bindcraft_runner._localization_marker(str(path))["generation"], "g1")

    def test_both_subcommands_accept_the_marker(self) -> None:
        parser = bindcraft_runner.parser()
        run = parser.parse_args([
            "run-trajectory", "--backend-id", "b", "--request", "r", "--input-manifest", "m",
            "--settings-template", "s", "--settings-sha256", "d", "--filters", "f",
            "--filters-sha256", "d", "--output", "o", "--shard-index", "0", "--seed", "1",
            "--pyrosetta-required", "--runtime-localization-marker", "/w/.fs2/runtime-localization.json",
        ])
        self.assertEqual(run.runtime_localization_marker, "/w/.fs2/runtime-localization.json")
        combine = parser.parse_args([
            "aggregate", "--backend-id", "b", "--request", "r", "--input-manifest", "m",
            "--shards", "s", "--expected-shards", "1", "--staging-manifest", "t",
            "--output-manifest", "o", "--atomic-rename",
            "--runtime-localization-marker", "/w/.fs2/runtime-localization.json",
        ])
        self.assertEqual(combine.runtime_localization_marker, "/w/.fs2/runtime-localization.json")


class NativeDesignEvidenceTests(unittest.TestCase):
    def row(self, **overrides: object) -> dict[str, str]:
        value = {
            "Average_i_pTM": "0.71",
            "Average_pLDDT": "0.86",
            "Average_dG": "-42.5",
            "Average_ShapeComplementarity": "0.64",
            "Average_n_InterfaceResidues": "3",
            "Average_dSASA": "1450.25",
            "Average_Binder_Energy_Score": "-18.75",
            "Average_Binder_RMSD": "1.2",
        }
        value.update({key: str(item) for key, item in overrides.items()})
        return value

    def test_statistics_use_the_columns_the_production_filters_threshold(self) -> None:
        # Every statistic this runtime records must be a column upstream writes
        # and settings_filters/default_filters.json thresholds by its exact
        # averaged name, so a filtered design's evidence is the filtered number.
        for column in bindcraft_runner.FINAL_STAT_COLUMNS.values():
            self.assertTrue(column.startswith("Average_"), column)
        self.assertEqual(
            bindcraft_runner.FINAL_STAT_COLUMNS["interface_residue_count"],
            "Average_n_InterfaceResidues",
        )
        self.assertEqual(
            bindcraft_runner.FINAL_STAT_COLUMNS["buried_interface_area"], "Average_dSASA"
        )

    def test_missing_statistic_is_rejected_instead_of_defaulted_to_zero(self) -> None:
        row = self.row()
        self.assertEqual(bindcraft_runner._statistic(row, "buried_interface_area"), 1450.25)
        for absent in ("Average_InterfaceResidues", "Average_BuriedSASA"):
            self.assertNotIn(absent, bindcraft_runner.FINAL_STAT_COLUMNS.values())
        del row["Average_dSASA"]
        with self.assertRaisesRegex(bindcraft_runner.ContractError, "no 'Average_dSASA' column"):
            bindcraft_runner._statistic(row, "buried_interface_area")

    def test_non_numeric_and_infinite_statistics_are_rejected(self) -> None:
        with self.assertRaisesRegex(bindcraft_runner.ContractError, "not a number"):
            bindcraft_runner._statistic(self.row(Average_dSASA="n/a"), "buried_interface_area")
        with self.assertRaisesRegex(bindcraft_runner.ContractError, "not finite"):
            bindcraft_runner._statistic(self.row(Average_dSASA="inf"), "buried_interface_area")

    def atom_record(
        self, serial: int, residue: str, chain: str, number: int, x: float
    ) -> str:
        """One fixed-column PDB ATOM record; coordinates must land in 31-54."""

        return (
            f"ATOM  {serial:5d}  CA  {residue:3s} {chain:1s}{number:4d}    "
            f"{x:8.3f}{0.0:8.3f}{0.0:8.3f}{1.0:6.2f}{0.0:6.2f}          C"
        )

    def complex_atoms(self, *, separation: float) -> list[dict[str, object]]:
        lines = [
            self.atom_record(1, "ALA", "A", 56, 0.0),
            self.atom_record(2, "GLY", "A", 99, 50.0),
            self.atom_record(3, "LEU", "B", 1, separation),
        ]
        with tempfile.TemporaryDirectory() as name:
            path = Path(name) / "complex.pdb"
            path.write_text("\n".join(lines) + "\n", encoding="ascii")
            atoms, binder = bindcraft_runner._atoms(path)
        self.assertEqual(len(atoms), 3)
        self.assertEqual(len(binder), 1)
        return atoms

    def test_hotspot_geometry_measures_real_contact_on_the_accepted_complex(self) -> None:
        parameters = {"hotspots": [{"chain": "A", "residue": 56}]}
        geometry = bindcraft_runner._hotspot_geometry(
            self.complex_atoms(separation=3.5), parameters
        )
        self.assertEqual(geometry["contacted"], 1)
        self.assertEqual(geometry["requested"][0]["closest_binder_atom_angstrom"], 3.5)
        self.assertEqual(
            geometry["contact_cutoff_angstrom"], bindcraft_runner.HOTSPOT_CONTACT_ANGSTROM
        )

    def test_hotspot_geometry_rejects_a_binder_that_missed_the_requested_site(self) -> None:
        parameters = {"hotspots": [{"chain": "A", "residue": 56}]}
        with self.assertRaisesRegex(bindcraft_runner.ContractError, "no requested hotspot"):
            bindcraft_runner._hotspot_geometry(self.complex_atoms(separation=12.0), parameters)

    def test_hotspot_geometry_rejects_a_hotspot_absent_from_the_structure(self) -> None:
        parameters = {"hotspots": [{"chain": "A", "residue": 1234}]}
        with self.assertRaisesRegex(bindcraft_runner.ContractError, "absent from the accepted"):
            bindcraft_runner._hotspot_geometry(self.complex_atoms(separation=3.5), parameters)

    def test_design_depth_and_mpnn_breadth_come_from_the_pinned_template(self) -> None:
        template = {
            "num_recycles_design": 1,
            "num_recycles_validation": 3,
            "num_seqs": 20,
            "max_mpnn_sequences": 2,
            "mpnn_weights": "soluble",
        }
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(
                bindcraft_runner._overridable(
                    template, "num_recycles_validation", "FS2_BINDCRAFT_VALIDATION_RECYCLES", int
                ),
                3,
            )
            self.assertEqual(
                bindcraft_runner._overridable(
                    template, "num_seqs", "FS2_BINDCRAFT_MPNN_SEQUENCES", int
                ),
                20,
            )
        with mock.patch.dict(os.environ, {"FS2_BINDCRAFT_MPNN_SEQUENCES": "4"}):
            self.assertEqual(
                bindcraft_runner._overridable(
                    template, "num_seqs", "FS2_BINDCRAFT_MPNN_SEQUENCES", int
                ),
                4,
            )
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(bindcraft_runner.ContractError, "has no 'absent'"):
                bindcraft_runner._overridable(template, "absent", "FS2_ABSENT", int)

    def test_settings_bind_the_admitted_af2_root_and_typed_trajectory_bound(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            template = root / "advanced.json"
            template.write_text(
                json.dumps({
                    "num_recycles_design": 1, "num_recycles_validation": 3, "num_seqs": 20,
                    "max_mpnn_sequences": 2, "model_path": "v_48_020", "mpnn_weights": "soluble",
                    "af_params_dir": "", "max_trajectories": False, "optimise_beta": True,
                    "enable_rejection_check": True,
                }),
                encoding="utf-8",
            )
            request = {
                "parameters": {
                    "_shard_index": 0, "target_chains": ["A"],
                    "hotspots": [{"chain": "A", "residue": 56}],
                    "binder_length": {"minimum": 60, "maximum": 75},
                    "accepted_designs_per_shard": 1, "max_trajectories_per_shard": 30,
                }
            }
            output = root / "out"
            output.mkdir()
            with mock.patch.dict(os.environ, {}, clear=True):
                _, _, resolved = bindcraft_runner._settings(
                    request, root / "target.pdb", output, template, "/sha256/af2"
                )
            self.assertEqual(resolved["af_params_dir"], "/sha256/af2")
            self.assertEqual(resolved["max_trajectories"], 30)
            self.assertEqual(resolved["num_recycles_validation"], 3)
            self.assertEqual(resolved["num_seqs"], 20)
            self.assertFalse(resolved["optimise_beta"])
            settings = json.loads((output / "target-settings.json").read_text())
            self.assertEqual(settings["target_hotspot_residues"], "56")
            self.assertEqual(settings["number_of_final_designs"], 1)


class SemanticJobRenderTests(unittest.TestCase):
    def handoff(self, root: Path, **overrides: object) -> Path:
        trees = []
        for role in sorted(bindcraft_runner.REQUIRED_TREE_ROLES):
            digest = (
                bindcraft_runner.PYROSETTA_TREE_MANIFEST_SHA256
                if role == bindcraft_runner.PYROSETTA_ROLE
                else hashlib.sha256(role.encode()).hexdigest()
            )
            trees.append({
                "role": role,
                "artifact_id": role,
                "sub_path": f"sha256/{hashlib.sha256(role.encode()).hexdigest()}",
                "sha256": digest,
            })
        value: dict[str, object] = {
            "schema": renderer.HANDOFF_SCHEMA,
            "claim": "academic-assets-runtime-rwx",
            "generation": "sha256:" + "c" * 64,
            "trees": trees,
        }
        value.update(overrides)
        path = root / "handoff.json"
        path.write_text(json.dumps(value), encoding="utf-8")
        return path

    def render(self, path: Path, **overrides: object):
        argv = [
            "--handoff", str(path),
            "--image", "cr.eu-north1.nebius.cloud/x/fs2-models/bindcraft@sha256:" + "b" * 64,
            "--run-id", "r14acceptance",
            "--job-name", "fs2-bindcraft-r14-acceptance",
        ]
        for key, item in overrides.items():
            argv += ["--" + key.replace("_", "-"), str(item)]
        args = renderer.parser().parse_args(argv)
        if args.hotspot is None:
            args.hotspot = [56]
        return renderer.render(args)

    def stages(self, job: dict) -> tuple[dict, dict]:
        spec = job["spec"]["template"]["spec"]
        return spec["initContainers"][0], spec["containers"][0]

    def test_both_stages_enter_the_image_through_the_outer_entrypoint(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            _, job = self.render(self.handoff(Path(name)))
            design, aggregate = self.stages(job)
            prefix = ["python", "/opt/fs2/runtime_entrypoint.py", "/opt/fs2/bin/bindcraft-batch"]
            self.assertEqual(design["command"][:4], prefix + ["run-trajectory"])
            self.assertEqual(aggregate["command"][:4], prefix + ["aggregate"])
            self.assertIn("--pyrosetta-required", design["command"])
            self.assertIn("--atomic-rename", aggregate["command"])
            # Only the design stage needs an accelerator.
            self.assertEqual(design["resources"]["limits"]["nvidia.com/gpu"], 1)
            self.assertNotIn("nvidia.com/gpu", aggregate["resources"]["limits"])

    def test_both_stages_carry_the_runtime_localization_marker(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            config_map, job = self.render(self.handoff(Path(name)))
            for stage in self.stages(job):
                command = stage["command"]
                self.assertIn("--runtime-localization-marker", command)
                self.assertEqual(
                    command[command.index("--runtime-localization-marker") + 1], renderer.MARKER_PATH
                )
                environment = {item["name"]: item["value"] for item in stage["env"]}
                self.assertEqual(
                    environment[bindcraft_runner.RUNTIME_LOCALIZATION_MARKER_ENV], renderer.MARKER_PATH
                )
            marker = json.loads(config_map["data"]["runtime-localization.json"])
            admission = json.loads(config_map["data"]["external-trees.json"])
            self.assertEqual(marker["generation"], admission["generation"])

    def test_rendered_job_uses_the_checked_in_production_settings_and_filters(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            _, job = self.render(self.handoff(Path(name)))
            command = self.stages(job)[0]["command"]
            self.assertIn(renderer.FILTERS, command)
            self.assertIn(renderer.FILTERS_SHA256, command)
            self.assertIn(renderer.SETTINGS_TEMPLATE, command)
            self.assertIn(renderer.SETTINGS_SHA256, command)
            self.assertNotIn("/opt/bindcraft/settings_filters/no_filters.json", command)

    def test_mpnn_lane_is_selectable_and_defaults_to_the_pinned_template(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            path = self.handoff(Path(name))
            _, default = self.render(path)
            for stage in self.stages(default):
                self.assertNotIn(
                    "FS2_BINDCRAFT_MPNN_WEIGHTS", {item["name"] for item in stage["env"]}
                )
            _, vanilla = self.render(path, mpnn_weights="original")
            for stage in self.stages(vanilla):
                environment = {item["name"]: item["value"] for item in stage["env"]}
                self.assertEqual(environment["FS2_BINDCRAFT_MPNN_WEIGHTS"], "original")

    def test_rendered_job_mounts_all_four_trees_at_the_paths_the_model_reads(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            _, job = self.render(self.handoff(Path(name)))
            container = self.stages(job)[0]
            mounted = {
                mount["mountPath"]: mount
                for mount in container["volumeMounts"]
                if mount["name"] == "external-trees"
            }
            self.assertEqual(set(mounted), set(renderer.MOUNT_PATH_BY_ROLE.values()))
            self.assertEqual(len(mounted), 4)
            for mount in mounted.values():
                self.assertTrue(mount["readOnly"])
                self.assertTrue(mount["subPath"].startswith("sha256/"))
            vanilla = renderer.MOUNT_PATH_BY_ROLE[bindcraft_runner.MPNN_VANILLA_ROLE]
            soluble = renderer.MOUNT_PATH_BY_ROLE[bindcraft_runner.MPNN_SOLUBLE_ROLE]
            self.assertTrue(vanilla.endswith("/colabdesign/mpnn/weights"))
            self.assertTrue(soluble.endswith("/colabdesign/mpnn/weights_soluble"))
            self.assertNotEqual(mounted[vanilla]["subPath"], mounted[soluble]["subPath"])

    def test_rendered_admission_matches_the_mount_paths_and_handoff_identities(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            config_map, _ = self.render(self.handoff(Path(name)))
            admission = json.loads(config_map["data"]["external-trees.json"])
            self.assertEqual(admission["schema"], bindcraft_runner.EXTERNAL_TREE_ADMISSION_SCHEMA)
            by_role = {tree["role"]: tree for tree in admission["trees"]}
            self.assertEqual(set(by_role), bindcraft_runner.REQUIRED_TREE_ROLES)
            for role, tree in by_role.items():
                self.assertEqual(tree["root"], renderer.MOUNT_PATH_BY_ROLE[role])
                self.assertRegex(tree["sha256"], r"^[0-9a-f]{64}$")

    def test_rendered_request_digest_matches_the_manifest_bytes_it_ships(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            config_map, _ = self.render(self.handoff(Path(name)))
            manifest_bytes = config_map["data"]["input-manifest.json"].encode()
            value = json.loads(config_map["data"]["request.json"])
            self.assertEqual(
                value["input_manifest"]["sha256"], hashlib.sha256(manifest_bytes).hexdigest()
            )
            self.assertEqual(value["input_manifest"]["size_bytes"], len(manifest_bytes))
            self.assertEqual(
                value["input_manifest"]["artifact_id"], json.loads(manifest_bytes)["manifest_id"]
            )
            self.assertEqual(value["parameters"]["hotspots"], [{"chain": "A", "residue": 56}])

    def test_render_rejects_a_handoff_that_is_not_the_licensed_tree(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            path = self.handoff(root)
            value = json.loads(path.read_text())
            for tree in value["trees"]:
                if tree["role"] == bindcraft_runner.PYROSETTA_ROLE:
                    tree["sha256"] = "0" * 64
            path.write_text(json.dumps(value), encoding="utf-8")
            with self.assertRaisesRegex(renderer.RenderError, "licensed tree this image"):
                self.render(path)

    def test_render_rejects_missing_roles_and_traversing_sub_paths(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            path = self.handoff(root)
            value = json.loads(path.read_text())
            value["trees"] = value["trees"][:3]
            path.write_text(json.dumps(value), encoding="utf-8")
            with self.assertRaisesRegex(renderer.RenderError, "missing roles"):
                self.render(path)
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            path = self.handoff(root)
            value = json.loads(path.read_text())
            value["trees"][0]["sub_path"] = "sha256/../escape"
            path.write_text(json.dumps(value), encoding="utf-8")
            with self.assertRaisesRegex(renderer.RenderError, "unsafe"):
                self.render(path)
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            path = self.handoff(root)
            value = json.loads(path.read_text())
            del value["generation"]
            path.write_text(json.dumps(value), encoding="utf-8")
            with self.assertRaisesRegex(renderer.RenderError, "no immutable localization generation"):
                self.render(path)

    def test_render_never_hard_codes_a_shared_filesystem_layout(self) -> None:
        source = (ROOT / "qualification" / "render_semantic_job.py").read_text(encoding="utf-8")
        # Tree locations are handoff inputs. The superseded mutable localization
        # layout must not reappear as a constant in the renderer or the runtime.
        for module in (source, (ROOT / "runtime" / "bindcraft_runtime_entrypoint.py").read_text()):
            self.assertNotIn("scientific-localization", module)

    def test_kueue_queue_is_optional_and_controls_suspension(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            path = self.handoff(Path(name))
            _, unqueued = self.render(path)
            self.assertFalse(unqueued["spec"]["suspend"])
            self.assertNotIn("kueue.x-k8s.io/queue-name", unqueued["metadata"]["labels"])
            _, queued = self.render(path, local_queue="academic-execution")
            self.assertTrue(queued["spec"]["suspend"])
            self.assertEqual(
                queued["metadata"]["labels"]["kueue.x-k8s.io/queue-name"], "academic-execution"
            )


if __name__ == "__main__":
    unittest.main()
