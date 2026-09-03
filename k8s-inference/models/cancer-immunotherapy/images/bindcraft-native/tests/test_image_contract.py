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
pyrosetta_patch = load_module(
    "patch_bindcraft_pyrosetta", ROOT / "runtime" / "patch_bindcraft_pyrosetta.py"
)
bindcraft_runner = load_module(
    "bindcraft_runtime_entrypoint", ROOT / "runtime" / "bindcraft_runtime_entrypoint.py"
)
tree_identity = load_module("tree_identity", ROOT / "runtime" / "tree_identity.py")
renderer = load_module("render_semantic_job", ROOT / "qualification" / "render_semantic_job.py")


class ImageLockTests(unittest.TestCase):
    def test_lock_carries_only_the_native_academic_bindcraft_image(self) -> None:
        # This package is the native PyRosetta BindCraft lane and nothing else.
        # The open fallback and the RFdiffusion/ProteinMPNN runtimes were split
        # out; a lock that still named them would imply this tree builds them.
        lock = build_images.load_lock()
        self.assertEqual([image["id"] for image in lock["images"]], ["bindcraft-academic"])
        image = lock["images"][0]
        self.assertEqual(image["source"]["revision"], "7cd4ace1b7407adf66a50dfefa47de2270f5e4a9")
        self.assertEqual(image["relationship"], "canonical-native-academic")
        self.assertTrue(image["equivalent_to_requested"])
        # The adapter is bound by content, never by commit: the commit that
        # produced this wrapper was rebased away, and r16 shipped a label naming
        # a revision no longer reachable from any branch.
        self.assertNotIn("adapter_commit", lock)
        self.assertEqual(
            lock["adapter_wrapper_sha256"],
            "1c62303bb5eca99581fcd0ca9c45cb27d3a8e275875d198db57bec2edb2b7be3",
        )
        dockerfile = (ROOT / "Dockerfile.bindcraft").read_text(encoding="utf-8")
        self.assertNotIn("3475ce0ee8efdf2d3ccbcc65651ab11fe7cb34fe", dockerfile)
        self.assertIn("adapter.wrapper.sha256", dockerfile)
        self.assertEqual(sorted(lock["shared_sources"]), [
            "colabdesign", "jaxlib_cuda12_cudnn89_wheel", "nvidia_cuda_nvcc_cuda121_wheel",
            "nvidia_cusparse_cuda121_wheel", "nvidia_nvjitlink_cuda121_wheel",
        ])
        self.assertEqual(
            sorted(path.name for path in ROOT.glob("Dockerfile*")), ["Dockerfile.bindcraft"]
        )

    def test_in_image_sources_match_the_recorded_publication_identity(self) -> None:
        """Keep the source and the evidence describing the image in step.

        The qualification evidence records the SHA-256 of every file the build
        copies into the image, and the publisher fails closed unless the pulled
        image contains exactly those bytes. Editing one without re-recording it
        would leave the evidence describing an image nobody built.

        runtime_entrypoint.py is the reason this test exists. It is the shared
        outer entrypoint and still names the RFdiffusion, ProteinMPNN and
        open-fallback runtimes, which reads like leftovers from the split. It is
        not - trimming those branches would change the digest for no behavioural
        gain, because this image only ever sets FS2_RUNTIME_NAME=bindcraft-academic.
        """

        evidence = json.loads(
            (ROOT / "evidence" / "native-final-image-qualification.json").read_text(encoding="utf-8")
        )
        recorded = evidence["source_identity_in_published_image"]["files"]
        self.assertEqual(len(recorded), 5)
        for entry in recorded:
            repository_path = entry["repository_path"]
            if repository_path.startswith("models/"):
                path = build_images.REPOSITORY_ROOT / repository_path
            else:
                path = ROOT / repository_path
            self.assertTrue(path.is_file(), repository_path)
            raw = path.read_bytes()
            self.assertEqual(hashlib.sha256(raw).hexdigest(), entry["sha256"], repository_path)
            self.assertEqual(len(raw), entry["size_bytes"], repository_path)

    def test_the_receipt_never_omits_the_measured_tree_ownership(self) -> None:
        """An absent field reads as satisfied; an explicit null reads as unchecked.

        Both this renderer's Pod and the adapter's reach a repaired tree through
        supplemental group 65532 and the currently damaged tree through primary
        group 10001, so a passing run cannot distinguish them. The receipt has to
        state which claim state produced it, and it has to state that even when
        nobody measured, or a later reader will take a green run for a passing
        delivery contract.
        """

        evidence = json.loads(
            (ROOT / "evidence" / "native-final-image-qualification.json").read_text(encoding="utf-8")
        )
        live = evidence["live_semantic_acceptance"]
        ownership = live["runtime_tree_ownership"]
        self.assertEqual(set(ownership["per_role"]), bindcraft_runner.REQUIRED_TREE_ROLES)
        measured = ownership["state"] == "measured"
        if not measured:
            self.assertEqual(ownership["state"], "not-measured")
            self.assertTrue(all(value is None for value in ownership["per_role"].values()))
            self.assertEqual(ownership["contract_conformance"], "unproven")
            # A run cannot be claimed while nothing was measured.
            self.assertEqual(live["state"], "not executed")
        else:
            for role, value in ownership["per_role"].items():
                self.assertEqual({"uid", "gid", "mode"}, set(value), role)
            conformant = all(value["gid"] == renderer.ACADEMIC_ASSET_GID
                             for value in ownership["per_role"].values())
            self.assertEqual(ownership["contract_conformance"], "proven" if conformant else "unproven")

    def test_the_image_declares_every_tree_it_expects_from_outside_itself(self) -> None:
        external = build_images.load_lock()["images"][0]["external_artifacts"]
        joined = " ".join(external).lower()
        self.assertEqual(len(external), 4)
        for expected in ("alphafold2", "vanilla", "soluble", "pyrosetta"):
            self.assertIn(expected, joined)

    def test_dockerfile_pins_base_and_excludes_weight_downloads(self) -> None:
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
        dockerfile = (ROOT / "Dockerfile.bindcraft").read_text(encoding="utf-8")
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

    def test_final_tag_is_r18_and_names_the_digest_it_supersedes(self) -> None:
        image = build_images.load_lock()["images"][0]
        self.assertEqual(image["build_tag_suffix"], "-cuda121-r18")
        self.assertTrue(image["target"].endswith(image["source"]["revision"] + "-cuda121-r18"))
        # r17 was reconstructible but could not read the accepted localization
        # generations: it rejected the marker the artifact plane writes inside
        # every one of them.
        self.assertEqual(
            image["supersedes"],
            "cr.eu-north1.nebius.cloud/e00akg9ndpx77eaexh/fs2-models/bindcraft@"
            "sha256:aefd9b1cfd3002182b0b19a147e86cb138b39af315f1b061bf4a10c119654850",
        )

    def test_the_adapter_wrapper_is_consumed_as_a_build_context(self) -> None:
        # The wrapper is the adapter's published interface and the only file this
        # package needs from the adapter tree, so it is carried alone rather than
        # dragging in an adapter this task does not own.
        self.assertEqual(
            build_images.ADAPTER_CONTEXT,
            build_images.REPOSITORY_ROOT / "models" / "structure" / "runtime",
        )
        wrapper = build_images.ADAPTER_CONTEXT / "bindcraft-native/bin/bindcraft-batch"
        self.assertTrue(wrapper.is_file())
        self.assertEqual(
            hashlib.sha256(wrapper.read_bytes()).hexdigest(),
            "1c62303bb5eca99581fcd0ca9c45cb27d3a8e275875d198db57bec2edb2b7be3",
        )
        bindcraft = (ROOT / "Dockerfile.bindcraft").read_text(encoding="utf-8")
        self.assertIn("bindcraft-native/bin/bindcraft-batch", bindcraft)
        self.assertNotIn("verify-academic-access", bindcraft)
        # load_lock re-hashes the wrapper, so a swapped file fails the build.
        with mock.patch.object(build_images, "ADAPTER_CONTEXT", ROOT / "tests"):
            with self.assertRaisesRegex(build_images.BuildError, "adapter wrapper is missing"):
                build_images.load_lock()

    def test_one_shot_gpu_smoke_bypasses_only_jax_shutdown_hooks(self) -> None:
        entrypoint = (ROOT / "runtime" / "runtime_entrypoint.py").read_text(encoding="utf-8")
        self.assertIn("sys.stdout.flush()", entrypoint)
        self.assertIn("os._exit(0)", entrypoint)
        self.assertIn("os.execvp(sys.argv[1]", entrypoint)

    def test_colabdesign_proteinmpnn_weights_are_external(self) -> None:
        entrypoint = (ROOT / "runtime" / "runtime_entrypoint.py").read_text(encoding="utf-8")
        self.assertIn('".pkl"', entrypoint)
        text = (ROOT / "Dockerfile.bindcraft").read_text(encoding="utf-8")
        self.assertIn("colabdesign/mpnn/weights_soluble", text)
        self.assertIn("colabdesign/mpnn/weights", text)
        # Both are removed in the same layer that installs ColabDesign, so the
        # image can never ship either lane's weights.
        self.assertIn("rm -rf \\\n      /opt/conda/lib/python3.10/site-packages/colabdesign/mpnn/weights", text)

    def test_native_semantic_run_is_generated_rather_than_hand_written(self) -> None:
        # The hand-written native Pod was removed: it pinned a superseded digest
        # and created an empty colabdesign weights_soluble package, so it looked
        # like a run against the soluble MPNN tree while reading no weights.
        self.assertFalse((ROOT / "kubernetes" / "bindcraft-semantic-pod.yaml").exists())
        self.assertTrue((ROOT / "qualification" / "render_semantic_job.py").is_file())

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

    def test_republication_replaces_the_record_and_drops_split_out_siblings(self) -> None:
        # The receipt is rebuilt from the lock, so a record for an image this
        # package no longer builds cannot survive a republication. That is what
        # keeps the split-out RFdiffusion, ProteinMPNN and open-fallback records
        # from reappearing in a BindCraft-only receipt.
        lock = build_images.load_lock()
        existing = {
            "schema": "fs2.nebius.ai/cancer-runtime-image-publication/v1",
            "images": [
                {"id": "bindcraft-academic", "digest": "sha256:" + "1" * 64},
                {"id": "rfdiffusion", "digest": "sha256:" + "9" * 64},
            ],
        }
        with tempfile.TemporaryDirectory() as name:
            receipt_path = Path(name) / "receipt.json"
            receipt_path.write_text(json.dumps(existing), encoding="utf-8")
            fresh = {"id": "bindcraft-academic", "digest": "sha256:" + "2" * 64}
            with mock.patch.object(build_images, "RECEIPT_PATH", receipt_path):
                build_images.write_receipt(lock, [fresh])
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
            self.assertEqual([record["id"] for record in receipt["images"]], ["bindcraft-academic"])
            self.assertEqual(receipt["images"][0]["digest"], "sha256:" + "2" * 64)

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


class AcceptedLocalizationHandoffTests(unittest.TestCase):
    """Hold this runtime to the artifact plane's accepted handoff, not to a guess.

    While the handoff was unavailable this package invented one. It is now on
    main, so these read the real document and fail if the runtime drifts from it.
    """

    HANDOFF = (
        build_images.REPOSITORY_ROOT
        / "models/cancer-immunotherapy/artifact-localization/evidence/binding-handoff.json"
    )

    def bindcraft_artifacts(self) -> dict[str, dict]:
        document = json.loads(self.HANDOFF.read_text(encoding="utf-8"))
        chosen = {}
        for artifact in document["artifacts"]:
            for consumer in artifact["consumers"]:
                if consumer.get("model_id") == "bindcraft":
                    chosen[artifact["artifact_id"]] = artifact
        return chosen

    def test_the_handoff_binds_exactly_this_runtime_s_four_mount_paths(self) -> None:
        artifacts = self.bindcraft_artifacts()
        self.assertEqual(len(artifacts), 4)
        mounted = {
            consumer["mount_path"]
            for artifact in artifacts.values()
            for consumer in artifact["consumers"]
            if consumer.get("model_id") == "bindcraft"
        }
        self.assertEqual(mounted, set(renderer.MOUNT_PATH_BY_ROLE.values()))

    def test_the_licensed_identity_the_image_pins_is_the_accepted_one(self) -> None:
        licensed = self.bindcraft_artifacts()["bindcraft-pyrosetta-installed-tree"]
        identity = licensed["tree_identity"]
        self.assertEqual(identity["inventory_algorithm"], tree_identity.TREE_MANIFEST_ALGORITHM)
        self.assertEqual(
            identity["inventory_sha256"], bindcraft_runner.PYROSETTA_TREE_MANIFEST_SHA256
        )
        self.assertEqual(licensed["visibility"], "tenant-private")
        self.assertEqual(licensed["volume"]["kind"], "persistent-volume-claim")

    def test_the_three_public_trees_are_host_plane_flat_inventories(self) -> None:
        artifacts = self.bindcraft_artifacts()
        public = {name: value for name, value in artifacts.items() if value["visibility"] == "public"}
        self.assertEqual(len(public), 3)
        for name, artifact in public.items():
            self.assertEqual(
                artifact["tree_identity"]["inventory_algorithm"],
                tree_identity.FLAT_INVENTORY_ALGORITHM,
                name,
            )
            self.assertEqual(artifact["volume"]["kind"], "host-path", name)
            self.assertEqual(artifact["volume"]["plane"], "reference-data-host", name)
            self.assertEqual(
                artifact["volume"]["node_selector"], {"storage.fs2.nebius/reference-data": "true"}, name
            )

    def test_every_accepted_generation_carries_the_marker_this_runtime_excludes(self) -> None:
        # This is why the exclusion is mandatory rather than an optimisation.
        for name, artifact in self.bindcraft_artifacts().items():
            marker = artifact["marker"]
            self.assertTrue(marker["in_generation"], name)
            self.assertEqual(marker["relative_path"], tree_identity.RUNTIME_MARKER_NAME, name)


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

    def test_the_in_generation_marker_never_moves_a_published_digest(self) -> None:
        """The artifact plane writes a marker inside every generation it publishes.

        Both identities must exclude it by that one reserved name, exactly as the
        producer does, or this runtime rejects every accepted generation: the
        published digests were computed over the contracted content only.
        """

        self.assertEqual(tree_identity.RUNTIME_MARKER_NAME, ".fs2-runtime-tree.json")
        marker = b'{"schema":"fs2-serve.nebius.ai/scientific-localization-generation-marker/v1"}\n'
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            self.flat(root)
            expected = tree_identity.flat_tree_inventory(root)["inventory_sha256"]
            entries = tree_identity.flat_tree_inventory(root)["entry_count"]
            (root / tree_identity.RUNTIME_MARKER_NAME).write_bytes(marker)
            receipt = tree_identity.verify_flat_tree(
                root, artifact_id="mpnn", expected_inventory_sha256=expected
            )
            self.assertEqual(receipt["entry_count"], entries)
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            self.nested(root)
            expected = tree_identity.tree_manifest(root)["tree_manifest_sha256"]
            (root / tree_identity.RUNTIME_MARKER_NAME).write_bytes(marker)
            tree_identity.verify_tree(
                root, artifact_id="licensed", expected_tree_manifest_sha256=expected
            )

    def test_no_other_dotfile_is_admitted(self) -> None:
        # Only the one reserved name is excluded; anything else with a leading
        # dot fails closed rather than being silently skipped.
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            self.flat(root)
            (root / ".fs2-other.json").write_bytes(b"{}\n")
            with self.assertRaisesRegex(tree_identity.TreeIdentityError, "safe flat-root names"):
                tree_identity.flat_tree_inventory(root)

    def test_every_receipt_records_the_mounted_root_ownership(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            self.flat(root)
            flat = tree_identity.verify_flat_tree(
                root,
                artifact_id="mpnn",
                expected_inventory_sha256=tree_identity.flat_tree_inventory(root)["inventory_sha256"],
            )
            self.assertEqual({"uid", "gid", "mode"}, set(flat["ownership"]))
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            self.nested(root)
            nested = tree_identity.verify_tree(
                root,
                artifact_id="licensed",
                expected_tree_manifest_sha256=tree_identity.tree_manifest(root)["tree_manifest_sha256"],
            )
            self.assertEqual({"uid", "gid", "mode"}, set(nested["ownership"]))

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


class CrossJobHandoffTests(unittest.TestCase):
    """The design and aggregate stages are separate Jobs over a shared claim.

    Nothing about a durable volume guarantees the bytes on it are still the ones
    the design Pod wrote, so the aggregate holds every handed-off artifact to the
    digest its producing shard published.
    """

    def shard(self, root: Path) -> dict[str, object]:
        artifacts = root / "artifacts"
        artifacts.mkdir(parents=True)
        payload = artifacts / "shard.json"
        payload.write_bytes(b'{"status":"succeeded"}\n')
        candidate = artifacts / "candidate-000.pdb"
        candidate.write_bytes(b"ATOM\nEND\n")

        def artifact(artifact_id: str, path: Path) -> dict[str, object]:
            raw = path.read_bytes()
            return {
                "artifact_id": artifact_id,
                "sha256": hashlib.sha256(raw).hexdigest(),
                "size_bytes": len(raw),
                "media_type": "application/json",
                "compression": "none",
            }

        return {
            "shard": {"name": "shard-000", "artifact": artifact("shard.000", payload)},
            "candidates": [
                {"name": "candidate-000-structure", "artifact": artifact("cand.000", candidate)}
            ],
            "artifact_paths": {
                "shard.000": "artifacts/shard.json",
                "cand.000": "artifacts/candidate-000.pdb",
            },
        }

    def test_an_intact_handoff_is_admitted(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            bindcraft_runner._verify_handoff(root, self.shard(root))

    def test_a_rewritten_artifact_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            shard = self.shard(root)
            (root / "artifacts" / "candidate-000.pdb").write_bytes(b"ATOM\nATOM\nEND\n")
            with self.assertRaisesRegex(bindcraft_runner.ContractError, "does not match the digest"):
                bindcraft_runner._verify_handoff(root, shard)

    def test_a_missing_artifact_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            shard = self.shard(root)
            (root / "artifacts" / "candidate-000.pdb").unlink()
            with self.assertRaisesRegex(bindcraft_runner.ContractError, "is missing"):
                bindcraft_runner._verify_handoff(root, shard)

    def test_declarations_and_paths_must_cover_the_same_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            shard = self.shard(root)
            del shard["artifact_paths"]["cand.000"]
            with self.assertRaisesRegex(bindcraft_runner.ContractError, "paths and declarations disagree"):
                bindcraft_runner._verify_handoff(root, shard)

    def test_an_escaping_artifact_path_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            shard = self.shard(root)
            shard["artifact_paths"]["cand.000"] = "../escape.pdb"
            with self.assertRaisesRegex(bindcraft_runner.ContractError, "unsafe"):
                bindcraft_runner._verify_handoff(root, shard)


class SemanticJobRenderTests(unittest.TestCase):
    # Built from the artifact plane's real accepted handoff rather than a shape
    # invented here, so a change upstream fails these tests instead of the run.
    ACADEMIC_CLAIM = "academic-assets-runtime-rwx"
    REFERENCE_PLANE_HOST_PATH = "/mnt/fs2-reference-data/data"
    NODE_SELECTOR = {"storage.fs2.nebius/reference-data": "true"}

    def handoff(self, root: Path, **overrides: object) -> Path:
        value = json.loads(AcceptedLocalizationHandoffTests.HANDOFF.read_text(encoding="utf-8"))
        # The real document reports its generations unpublished, which the
        # renderer refuses by design; a render test has to say they exist.
        value["evidence"]["generations_published"] = True
        value.update(overrides)
        path = root / "handoff.json"
        path.write_text(json.dumps(value), encoding="utf-8")
        return path

    def artifact(self, value: dict, artifact_id: str) -> dict:
        for artifact in value["artifacts"]:
            if artifact["artifact_id"] == artifact_id:
                return artifact
        raise AssertionError(artifact_id)

    def render(self, path: Path, **overrides: object):
        argv = [
            "--handoff", str(path),
            "--image", "cr.eu-north1.nebius.cloud/x/fs2-models/bindcraft@sha256:" + "b" * 64,
            "--run-id", "r17acceptance",
            "--job-name", "fs2-bindcraft-r17-acceptance",
            "--workspace-claim", "fs2-bindcraft-acceptance-workspace",
        ]
        if "stage" not in overrides:
            argv += ["--stage", "design"]
        for key, item in overrides.items():
            argv += ["--" + key.replace("_", "-"), str(item)]
        args = renderer.parser().parse_args(argv)
        if args.hotspot is None:
            args.hotspot = [56]
        return renderer.render(args)

    def stages(self, path: Path) -> tuple[dict, dict]:
        """Render both Jobs; each stage is its own Job now."""

        _, design = self.render(path, stage="design")
        _, aggregate = self.render(path, stage="aggregate")
        return (
            design["spec"]["template"]["spec"]["containers"][0],
            aggregate["spec"]["template"]["spec"]["containers"][0],
        )

    def test_both_stages_enter_the_image_through_the_outer_entrypoint(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            design, aggregate = self.stages(self.handoff(Path(name)))
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
            path = self.handoff(Path(name))
            config_map, _ = self.render(path, stage="design")
            for stage in self.stages(path):
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
            # A generation is per artifact in the accepted contract and equals
            # that tree's own identity, so no run-level token is invented.
            identities = {tree["role"]: tree["sha256"] for tree in admission["trees"]}
            self.assertEqual(
                {role: entry["generation"] for role, entry in marker["trees"].items()}, identities
            )

    def test_rendered_job_uses_the_checked_in_production_settings_and_filters(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            command = self.stages(self.handoff(Path(name)))[0]["command"]
            self.assertIn(renderer.FILTERS, command)
            self.assertIn(renderer.FILTERS_SHA256, command)
            self.assertIn(renderer.SETTINGS_TEMPLATE, command)
            self.assertIn(renderer.SETTINGS_SHA256, command)
            self.assertNotIn("/opt/bindcraft/settings_filters/no_filters.json", command)

    def test_mpnn_lane_is_selectable_and_defaults_to_the_pinned_template(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            path = self.handoff(Path(name))
            for stage in self.stages(path):
                self.assertNotIn(
                    "FS2_BINDCRAFT_MPNN_WEIGHTS", {item["name"] for item in stage["env"]}
                )
            vanilla = [self.render(path, stage=s, mpnn_weights="original")[1]
                       for s in ("design", "aggregate")]
            for stage in [job["spec"]["template"]["spec"]["containers"][0] for job in vanilla]:
                environment = {item["name"]: item["value"] for item in stage["env"]}
                self.assertEqual(environment["FS2_BINDCRAFT_MPNN_WEIGHTS"], "original")

    def test_public_trees_come_from_the_reference_plane_and_only_pyrosetta_from_the_claim(self) -> None:
        # The four trees do not share a backing store. An earlier renderer put
        # all four on one claim, which could not have mounted the three public
        # immutable generations at all.
        with tempfile.TemporaryDirectory() as name:
            path = self.handoff(Path(name))
            _, job = self.render(path, stage="design")
            spec = job["spec"]["template"]["spec"]
            container = spec["containers"][0]
            volumes = {volume["name"]: volume for volume in spec["volumes"]}
            mounted = {
                mount["mountPath"]: mount
                for mount in container["volumeMounts"]
                if mount["name"].startswith("trees-")
            }
            self.assertEqual(set(mounted), set(renderer.MOUNT_PATH_BY_ROLE.values()))
            self.assertEqual(len(mounted), 4)

            licensed = mounted[renderer.MOUNT_PATH_BY_ROLE[bindcraft_runner.PYROSETTA_ROLE]]
            self.assertEqual(
                volumes[licensed["name"]]["persistentVolumeClaim"]["claimName"], self.ACADEMIC_CLAIM
            )
            self.assertTrue(
                licensed["subPath"].startswith("scientific-localization/private/generations/"),
                licensed["subPath"],
            )

            public_paths = set(renderer.MOUNT_PATH_BY_ROLE.values()) - {licensed["mountPath"]}
            for mount_path in public_paths:
                mount = mounted[mount_path]
                self.assertEqual(
                    volumes[mount["name"]]["hostPath"]["path"], self.REFERENCE_PLANE_HOST_PATH
                )
                self.assertIn("/sha256/", mount["subPath"], mount_path)
                self.assertTrue(
                    mount["subPath"].startswith("scientific-localization/public/generations/"),
                    mount_path,
                )
            # One hostPath volume shared by the three public trees, plus the claim.
            self.assertEqual(len({mounted[p]["name"] for p in public_paths}), 1)
            for mount in mounted.values():
                self.assertTrue(mount["readOnly"])

            vanilla = renderer.MOUNT_PATH_BY_ROLE[bindcraft_runner.MPNN_VANILLA_ROLE]
            soluble = renderer.MOUNT_PATH_BY_ROLE[bindcraft_runner.MPNN_SOLUBLE_ROLE]
            self.assertTrue(vanilla.endswith("/colabdesign/mpnn/weights"))
            self.assertTrue(soluble.endswith("/colabdesign/mpnn/weights_soluble"))
            self.assertNotEqual(mounted[vanilla]["subPath"], mounted[soluble]["subPath"])

    def test_both_stages_are_pinned_to_a_node_carrying_the_reference_plane(self) -> None:
        # The public generations are hostPath, so a Pod that lands elsewhere
        # would mount an empty or foreign directory.
        with tempfile.TemporaryDirectory() as name:
            path = self.handoff(Path(name))
            _, design = self.render(path, stage="design")
            _, aggregate = self.render(path, stage="aggregate")
            for job in (design, aggregate):
                selector = job["spec"]["template"]["spec"]["nodeSelector"]
                for key, value in self.NODE_SELECTOR.items():
                    self.assertEqual(selector[key], value)
            self.assertEqual(
                design["spec"]["template"]["spec"]["nodeSelector"]["accelerator.fs2.nebius/class"],
                "nvidia-h100-sxm5-80gb",
            )
            self.assertNotIn(
                "accelerator.fs2.nebius/class",
                aggregate["spec"]["template"]["spec"]["nodeSelector"],
            )

    def test_a_mixed_plane_run_joins_both_planes_groups(self) -> None:
        # Read access follows the planes actually mounted: the public host plane
        # is owned by 1000 and the private academic claim delivers on 65532. The
        # localization renderer on main asserts exactly [1000, 65532] for this
        # shape, so a run that joined only one of them could not read the other.
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            _, default = self.render(self.handoff(root), stage="design")
            self.assertEqual(
                default["spec"]["template"]["spec"]["securityContext"]["supplementalGroups"],
                [renderer.PUBLIC_PLANE_GID, renderer.ACADEMIC_ASSET_GID],
            )
            path = self.handoff(root, supplemental_groups=[4242])
            _, declared = self.render(path, stage="design")
            self.assertEqual(
                declared["spec"]["template"]["spec"]["securityContext"]["supplementalGroups"],
                sorted({4242, renderer.PUBLIC_PLANE_GID, renderer.ACADEMIC_ASSET_GID}),
            )

    def test_the_default_supplemental_group_is_the_published_contract(self) -> None:
        # academic-asset-readiness.json publishes asset_gid 65532 with
        # consumer_access "supplemental-group". The claim was later recursively
        # chowned to group 10001 by pods mounting it with fsGroup 10001, so the
        # group observed on the volume is damage. Encoding the observation would
        # make this runtime depend on the fault and break on repair.
        self.assertEqual(renderer.ACADEMIC_ASSET_GID, 65532)
        with tempfile.TemporaryDirectory() as name:
            _, job = self.render(self.handoff(Path(name)), stage="design")
            context = job["spec"]["template"]["spec"]["securityContext"]
            self.assertIn(65532, context["supplementalGroups"])
            self.assertNotIn("fsGroup", context)
            self.assertEqual(renderer.PUBLIC_PLANE_GID, 1000)

    def test_supplemental_groups_must_be_positive_integers(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            path = self.handoff(Path(name), supplemental_groups=["1000"])
            with self.assertRaisesRegex(renderer.RenderError, "positive integers"):
                self.render(path, stage="design")

    def mutate(self, root: Path, artifact_id: str, **changes: object) -> Path:
        value = json.loads(self.handoff(root).read_text())
        artifact = self.artifact(value, artifact_id)
        for key, item in changes.items():
            artifact[key] = item
        path = root / "handoff.json"
        path.write_text(json.dumps(value), encoding="utf-8")
        return path

    def test_the_licensed_tree_may_not_be_served_from_a_public_volume(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            path = self.mutate(root, "bindcraft-pyrosetta-installed-tree", volume={
                "kind": "host-path", "host_root": self.REFERENCE_PLANE_HOST_PATH,
                "sub_path": "scientific-localization/public/x", "node_selector": self.NODE_SELECTOR,
            })
            with self.assertRaisesRegex(renderer.RenderError, "private academic claim"):
                self.render(path, stage="design")

    def test_a_public_tree_may_not_be_served_from_the_private_claim(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            licensed = json.loads(self.handoff(root).read_text())
            claim_volume = dict(
                self.artifact(licensed, "bindcraft-pyrosetta-installed-tree")["volume"]
            )
            path = self.mutate(root, "alphafold2-params-bindcraft", volume=claim_volume)
            with self.assertRaisesRegex(renderer.RenderError, "must not be served from the private"):
                self.render(path, stage="design")

    def test_each_stage_is_its_own_job_so_the_gpu_is_released_after_design(self) -> None:
        # Running design as an init container of the aggregate's Pod kept the
        # accelerator allocated for the whole Pod lifetime, including the
        # CPU-only aggregation.
        with tempfile.TemporaryDirectory() as name:
            path = self.handoff(Path(name))
            _, design = self.render(path, stage="design")
            _, aggregate = self.render(path, stage="aggregate")
            for job in (design, aggregate):
                spec = job["spec"]["template"]["spec"]
                self.assertNotIn("initContainers", spec)
                self.assertEqual(len(spec["containers"]), 1)
            self.assertTrue(design["metadata"]["name"].endswith("-design"))
            self.assertTrue(aggregate["metadata"]["name"].endswith("-aggregate"))
            self.assertEqual(
                design["spec"]["template"]["spec"]["containers"][0]["resources"]["limits"][
                    "nvidia.com/gpu"
                ],
                1,
            )
            self.assertNotIn(
                "nvidia.com/gpu",
                aggregate["spec"]["template"]["spec"]["containers"][0]["resources"]["limits"],
            )
            # The handoff between them must outlive the design Pod.
            for job in (design, aggregate):
                volumes = {v["name"]: v for v in job["spec"]["template"]["spec"]["volumes"]}
                self.assertIn("persistentVolumeClaim", volumes["workspace"])
                self.assertNotIn("emptyDir", volumes["workspace"])

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
            foreign = "0" * 64
            path = self.mutate(
                root, "bindcraft-pyrosetta-installed-tree",
                generation=foreign,
                tree_identity={"inventory_algorithm": "fs2-tree-manifest/v1",
                               "inventory_sha256": foreign},
            )
            with self.assertRaisesRegex(renderer.RenderError, "licensed tree this image"):
                self.render(path, stage="design")

    def test_render_rejects_missing_roles_and_traversing_sub_paths(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            value = json.loads(self.handoff(root).read_text())
            value["models"]["bindcraft"] = value["models"]["bindcraft"][:3]
            path = root / "handoff.json"
            path.write_text(json.dumps(value), encoding="utf-8")
            with self.assertRaisesRegex(renderer.RenderError, "missing roles"):
                self.render(path, stage="design")
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            path = self.handoff(root)
            value = json.loads(self.handoff(root).read_text())
            volume = dict(self.artifact(value, "colabdesign-mpnn-weights-vanilla")["volume"])
            volume["sub_path"] = "sha256/../escape"
            path = self.mutate(root, "colabdesign-mpnn-weights-vanilla", volume=volume)
            with self.assertRaisesRegex(renderer.RenderError, "unsafe"):
                self.render(path, stage="design")
    def test_no_tree_location_is_hard_coded(self) -> None:
        # Every location comes from the handoff. Asserting the real document's
        # own paths are absent from the source is stronger than a keyword ban:
        # it fails if any concrete generation path is ever pasted in.
        source = (ROOT / "qualification" / "render_semantic_job.py").read_text(encoding="utf-8")
        runtime = (ROOT / "runtime" / "bindcraft_runtime_entrypoint.py").read_text(encoding="utf-8")
        document = json.loads(
            AcceptedLocalizationHandoffTests.HANDOFF.read_text(encoding="utf-8")
        )
        located = set()
        for artifact in document["artifacts"]:
            volume = artifact["volume"]
            located.add(volume["sub_path"])
            for key in ("host_root", "host_path", "claim"):
                if isinstance(volume.get(key), str):
                    located.add(volume[key])
        self.assertTrue(located)
        for module in (source, runtime):
            for location in located:
                self.assertNotIn(location, module)

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
