"""Invariants of the AlphaFold 3 runtime image definition and its contracts.

These assertions are about the committed build definition, so they hold before
anything is built and they fail loudly if a later edit weakens the licensing,
nonroot, cache-honesty or stage-separation properties.
"""

from __future__ import annotations

import json
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REPO = ROOT.parents[3]

UPSTREAM_COMMIT = "85c4d20505fd5cef05eac22b534d4e793971ae69"
UPSTREAM_TREE = "efa1a376c9cf94d517d70e68425bc1ed3b17a570"
BASE_DIGEST = "sha256:c87e78933f4c16e3272123bf2f75537306596d0fbaa395a29696a22786e5ee0e"
AUTHORIZED_SHA256 = "74d0258616917cd122f5eab6d076afe4a8930e96823851e65e4f777dfb1f33ff"
AUTHORIZED_BYTES = 1020545840

DOCKERFILE = (ROOT / "Dockerfile").read_text(encoding="utf-8")


def contract(name: str) -> dict:
    return json.loads((ROOT / "contracts" / name).read_text(encoding="utf-8"))


class DockerfileTests(unittest.TestCase):
    def test_every_base_image_is_digest_pinned(self) -> None:
        from_lines = [
            line
            for line in DOCKERFILE.splitlines()
            if line.startswith("FROM ") and "scratch" not in line
        ]
        self.assertTrue(from_lines)
        for line in from_lines:
            self.assertRegex(line, r"@sha256:[a-f0-9]{64}(?: AS [a-z0-9]+)?$")
        self.assertIn(BASE_DIGEST, DOCKERFILE)

    def test_the_uv_toolchain_is_digest_pinned(self) -> None:
        self.assertRegex(
            DOCKERFILE, r"ghcr\.io/astral-sh/uv:0\.9\.24@sha256:[a-f0-9]{64}"
        )

    def test_the_upstream_revision_and_tree_are_both_asserted(self) -> None:
        self.assertIn(f"AF3_COMMIT={UPSTREAM_COMMIT}", DOCKERFILE)
        self.assertIn(f"AF3_TREE={UPSTREAM_TREE}", DOCKERFILE)
        self.assertIn('test "$(git rev-parse HEAD)" = "${AF3_COMMIT}"', DOCKERFILE)
        self.assertIn("git rev-parse 'HEAD^{tree}'", DOCKERFILE)

    def test_the_hmmer_tarball_is_checksum_pinned(self) -> None:
        self.assertIn(
            "ca70d94fd0cf271bd7063423aabb116d42de533117343a9b27a65c17ff06fbf3", DOCKERFILE
        )
        self.assertIn("sha256sum --check --strict", DOCKERFILE)

    def test_the_distribution_version_is_asserted_not_guessed(self) -> None:
        self.assertIn("SETUPTOOLS_SCM_PRETEND_VERSION", DOCKERFILE)
        self.assertIn("alphafold3 version mismatch", DOCKERFILE)

    def test_the_build_fails_if_a_licensed_or_database_payload_is_present(self) -> None:
        self.assertIn("licensed payload present in image", DOCKERFILE)
        self.assertIn("reference database present in image", DOCKERFILE)
        self.assertIn("reference mmcif_files directory present in image", DOCKERFILE)
        self.assertIn("unexpected payload in upstream source", DOCKERFILE)

    def test_the_image_runs_as_a_numeric_nonroot_user(self) -> None:
        self.assertIn("USER 1001:1001", DOCKERFILE)
        # House style: numeric users only, no account creation.
        self.assertNotIn("useradd", DOCKERFILE)
        self.assertNotIn("groupadd", DOCKERFILE)

    def test_no_blanket_package_upgrade(self) -> None:
        self.assertNotIn("apt-get upgrade", DOCKERFILE)
        self.assertNotIn("apk upgrade", DOCKERFILE)

    def test_writable_paths_tolerate_an_arbitrary_overridden_uid(self) -> None:
        # The pod chooses runAsUser, so the writable directories must not be
        # owned by one specific uid.
        self.assertIn("install -d -m 1777", DOCKERFILE)
        self.assertIn("install -d -m 0555 /models /reference-data", DOCKERFILE)

    def test_the_cache_env_points_at_the_documented_paths(self) -> None:
        for expected in (
            "ENV FS2_AF3_CACHE_ROOT=/cache/alphafold3",
            "ENV FS2_AF3_JAX_CACHE_DIR=/cache/alphafold3/jax",
            "ENV FS2_AF3_TRITON_CACHE_DIR=/cache/alphafold3/triton",
            "ENV FS2_AF3_XDG_CACHE_DIR=/cache/alphafold3/xdg",
        ):
            self.assertIn(expected, DOCKERFILE)

    def test_the_binding_defaults_are_mount_points_only(self) -> None:
        self.assertIn("ENV FS2_AF3_PARAMETER_PATH=/models/af3.bin.zst", DOCKERFILE)
        self.assertIn("ENV FS2_AF3_REFERENCE_MOUNT=/reference-data", DOCKERFILE)
        # No snapshot or parameter identity may be baked in as a path.
        self.assertNotIn(AUTHORIZED_SHA256, DOCKERFILE)

    def test_the_entrypoint_is_the_fail_closed_runtime(self) -> None:
        self.assertIn(
            'ENTRYPOINT ["/alphafold3_venv/bin/python3", "/opt/fs2/af3_runtime.py"]', DOCKERFILE
        )
        self.assertIn('CMD ["verify"]', DOCKERFILE)

    def test_no_gpu_family_or_node_name_is_hardcoded(self) -> None:
        lowered = DOCKERFILE.lower()
        for forbidden in ("h100", "sxm5", "nodename", "node-name", "b300", "a100"):
            self.assertNotIn(forbidden, lowered)

    def test_the_labels_describe_the_external_binding_honestly(self) -> None:
        for expected in (
            'ai.nebius.fs2.runtime="alphafold3"',
            'ai.nebius.fs2.runtime.parameters="external-volume"',
            'ai.nebius.fs2.runtime.reference-data="external-volume"',
            'ai.nebius.fs2.runtime.cache-level="L1-image-local"',
            f'org.opencontainers.image.revision="${{AF3_COMMIT}}"',
        ):
            self.assertIn(expected, DOCKERFILE)

    def test_the_build_context_is_closed_to_everything_else(self) -> None:
        ignore = (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
        self.assertEqual("*", ignore[0])
        allowed = {line.lstrip("!") for line in ignore if line.startswith("!")}
        self.assertEqual(
            {
                "runtime/af3_runtime.py",
                "contracts/af3-runtime-source-lock.json",
                "contracts/af3-parameter-binding.json",
            },
            allowed,
        )


class NoLicensedBytesTests(unittest.TestCase):
    def test_no_parameter_or_database_payload_is_committed(self) -> None:
        forbidden_suffixes = {".bin", ".zst", ".pt", ".pkl", ".safetensors", ".whl", ".fasta"}
        for path in ROOT.rglob("*"):
            if not path.is_file() or "__pycache__" in path.parts:
                continue
            with self.subTest(path=path.relative_to(ROOT)):
                self.assertNotIn(path.suffix, forbidden_suffixes)
                # Nothing committed here should be large enough to be a payload.
                self.assertLess(path.stat().st_size, 2 * 1024 * 1024)

    def test_no_concrete_registry_account_path_is_committed(self) -> None:
        """The registry account path is a deploy-time binding, never committed.

        The needles are assembled at run time so this file does not itself
        contain the literals it forbids.
        """
        opaque_id = re.compile("-e" + "[0-9a-z]{15,}")
        registry_prefix = "cr.eu-north1.nebius.cloud/" + "e0"
        for path in sorted(ROOT.rglob("*")):
            if not path.is_file() or "__pycache__" in path.parts:
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
            with self.subTest(path=path.relative_to(ROOT)):
                self.assertIsNone(opaque_id.search(text))
                self.assertNotIn(registry_prefix, text)


class ParameterBindingContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.binding = contract("af3-parameter-binding.json")

    def test_it_restates_the_authorized_identity_exactly(self) -> None:
        artifact = self.binding["artifact"]
        self.assertEqual(AUTHORIZED_SHA256, artifact["sha256"])
        self.assertEqual(AUTHORIZED_BYTES, artifact["size_bytes"])
        self.assertEqual("file-digest", artifact["content_identity_kind"])
        self.assertIsNone(artifact["content_manifest_algorithm"])

    def test_it_agrees_with_the_academic_assets_source_of_truth(self) -> None:
        """The identity is owned by academic-assets; this must not drift from it."""
        source = json.loads(
            (REPO / "academic-assets" / "contracts" / "academic-assets.json").read_text(
                encoding="utf-8"
            )
        )
        text = json.dumps(source)
        self.assertIn(AUTHORIZED_SHA256, text)
        self.assertIn(str(AUTHORIZED_BYTES), text)
        self.assertIn("alphafold3/af3.bin.zst", text)
        self.assertIn("/models/af3.bin.zst", text)
        self.assertIn(UPSTREAM_COMMIT, text)

    def test_it_forbids_embedding_and_fs_group(self) -> None:
        self.assertFalse(self.binding["license"]["embed_in_image"])
        self.assertFalse(self.binding["license"]["world_readable"])
        self.assertEqual("prohibited", self.binding["license"]["redistribution"])
        permissions = self.binding["delivery"]["permissions"]
        self.assertEqual(65532, permissions["asset_gid"])
        self.assertTrue(permissions["fs_group_forbidden"])

    def test_the_quarantine_claim_is_named_as_forbidden(self) -> None:
        forbidden = self.binding["delivery"]["forbidden_claims"]
        names = {entry["claim"] for entry in forbidden}
        self.assertIn("cancer-immunotherapy-academic-assets-rwx-v1", names)

    def test_the_canonical_mode_is_the_subpath_file_mount(self) -> None:
        modes = {mode["mode"]: mode for mode in self.binding["delivery"]["supported_modes"]}
        canonical = modes["subpath-file-mount"]
        self.assertTrue(canonical["preferred"])
        self.assertEqual("alphafold3/af3.bin.zst", canonical["source_sub_path"])
        self.assertEqual("/models/af3.bin.zst", canonical["consumer_path"])
        self.assertEqual("/models", canonical["model_dir"])
        self.assertTrue(canonical["read_only"])
        self.assertFalse(canonical["embeds_bytes"])


class HandoffContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.handoff = contract("af3-runtime-handoff.json")

    def test_the_registry_path_stays_withheld_and_the_digest_is_authoritative(self) -> None:
        image = self.handoff["image"]
        self.assertEqual("withheld", image["repository"])
        self.assertIn(image["digest_state"], {"unpublished", "published"})
        if image["digest_state"] == "published":
            self.assertRegex(image["digest"], r"^sha256:[0-9a-f]{64}$")
        else:
            self.assertIsNone(image["digest"])
        # The retained historical evidence images are not this runtime.
        self.assertIn("eaea560c", image["digest_note"])
        self.assertIn("bead2e68", image["digest_note"])
        self.assertNotEqual(
            "sha256:eaea560ce2ddba8d828371d1cba01da954d9a68ff5e77ba4d43b36b107141887",
            image["digest"],
        )

    def test_a_superseded_publication_is_recorded_rather_than_hidden(self) -> None:
        superseded = self.handoff["image"]["superseded"]
        self.assertTrue(superseded)
        for entry in superseded:
            self.assertRegex(entry["digest"], r"^sha256:[0-9a-f]{64}$")
            self.assertIn("must not be deployed", entry["reason"].lower())
            self.assertNotEqual(entry["digest"], self.handoff["image"]["digest"])

    def test_the_gpu_requirement_is_family_agnostic(self) -> None:
        image = self.handoff["image"]
        self.assertEqual("agnostic", image["gpu_family"])
        self.assertNotIn("H100", image["gpu_requirement"].replace("H100-specific", ""))

    def test_the_cache_is_never_described_as_a_gpu_snapshot(self) -> None:
        cache = self.handoff["cache_levels"]
        self.assertFalse(cache["is_gpu_snapshot"])
        self.assertEqual([], cache["claimed_levels_above_L1"])
        self.assertIn("compilation artefacts only", cache["auxiliary_compiler_cache"])

    def test_the_two_stages_are_separated_and_never_share_bindings(self) -> None:
        stages = {stage["stage"]: stage for stage in self.handoff["stages"]}
        self.assertEqual({"data", "inference"}, set(stages))

        data = stages["data"]
        self.assertEqual("cpu", data["role"])
        self.assertEqual(0, data["gpu"])
        self.assertIn("--norun_inference", data["composed_flags"])
        self.assertIn("the academic parameter claim", data["forbidden_mounts"])

        inference = stages["inference"]
        self.assertEqual("gpu", inference["role"])
        self.assertEqual(1, inference["gpu"])
        self.assertIn("--norun_data_pipeline", inference["composed_flags"])
        self.assertIn("the reference database tree", inference["forbidden_mounts"])

        data_mounts = {mount["name"] for mount in data["mounts"]}
        inference_mounts = {mount["name"] for mount in inference["mounts"]}
        self.assertIn("reference-data", data_mounts)
        self.assertNotIn("academic-parameters", data_mounts)
        self.assertIn("academic-parameters", inference_mounts)
        self.assertNotIn("reference-data", inference_mounts)

    def test_the_parameter_mount_is_the_terraform_owned_claim(self) -> None:
        stages = {stage["stage"]: stage for stage in self.handoff["stages"]}
        mounts = {mount["name"]: mount for mount in stages["inference"]["mounts"]}
        parameters = mounts["academic-parameters"]
        self.assertEqual("academic-assets-runtime-rwx", parameters["claim"])
        self.assertEqual("fs2-academic-poc", parameters["claim_namespace"])
        self.assertEqual("alphafold3/af3.bin.zst", parameters["source_sub_path"])
        self.assertEqual("/models/af3.bin.zst", parameters["mount_path"])
        self.assertTrue(parameters["read_only"])
        self.assertIn("Terraform", parameters["owner"])

    def test_pod_security_requires_the_supplemental_group_and_forbids_fs_group(self) -> None:
        security = self.handoff["pod_security"]
        self.assertTrue(security["runAsNonRoot"])
        self.assertEqual([65532], security["supplementalGroups"])
        self.assertEqual("must not be set", security["fsGroup"])

    def test_readiness_never_claims_the_model_is_servable(self) -> None:
        readiness = self.handoff["readiness"]
        self.assertIn(readiness["state"], {"not-ready", "runtime-qualified"})
        blocking = " ".join(readiness["blocking"]).lower()
        for gate in ("reference-data", "controller"):
            self.assertIn(gate, blocking)
        self.assertIn("not servable", readiness["rule"].lower())
        if readiness["state"] == "runtime-qualified":
            completed = " ".join(readiness["completed"]).lower()
            self.assertIn("registry", completed)
            self.assertIn("h100", completed)

    def test_every_superseded_digest_is_distinct_and_explained(self) -> None:
        image = self.handoff["image"]
        digests = {entry["digest"] for entry in image["superseded"]}
        self.assertEqual(len(image["superseded"]), len(digests))
        self.assertGreaterEqual(len(digests), 1)
        self.assertNotIn(image["digest"], digests)
        for entry in image["superseded"]:
            self.assertIn("must not be deployed", entry["reason"].lower())

    def test_the_command_io_contract_is_advertised_to_consumers(self) -> None:
        contract = self.handoff["command_io_contract"]
        self.assertEqual(
            "contracts/af3-command-io-contract.json", contract["document"]
        )
        self.assertEqual("fixtures/", contract["fixtures"])
        self.assertIn("fs2-run-alphafold3", contract["note"])


class ImageLockTests(unittest.TestCase):
    """The committed lock must stay honest about what has and has not happened."""

    def setUp(self) -> None:
        path = ROOT / "contracts" / "af3-image-lock.json"
        if not path.is_file():
            self.skipTest(
                "the image lock is produced by a build and lands in the evidence commit "
                "that follows it"
            )
        self.lock = json.loads(path.read_text(encoding="utf-8"))

    def test_it_validates_against_its_published_schema(self) -> None:
        from jsonschema import Draft202012Validator

        schema = json.loads(
            (ROOT / "schemas" / "af3-image-lock.schema.json").read_text(encoding="utf-8")
        )
        Draft202012Validator(schema).validate(self.lock)

    def test_it_pins_the_upstream_and_base_identities(self) -> None:
        source = self.lock["source"]
        self.assertEqual(UPSTREAM_COMMIT, source["upstream_commit"])
        self.assertEqual(UPSTREAM_TREE, source["upstream_tree"])
        self.assertEqual("3.0.4", source["upstream_version"])
        self.assertEqual(BASE_DIGEST, self.lock["build"]["base_image_digest"])
        self.assertEqual("linux/amd64", self.lock["build"]["platform"])

    def test_it_records_both_required_attestations(self) -> None:
        self.assertEqual(
            ["https://slsa.dev/provenance/v1", "https://spdx.dev/Document"],
            sorted(self.lock["build"]["attestation_predicates"]),
        )

    def test_it_proves_no_payload_is_embedded(self) -> None:
        hygiene = self.lock["hygiene"]
        self.assertFalse(hygiene["parameters_embedded"])
        self.assertFalse(hygiene["reference_databases_embedded"])
        self.assertEqual([], hygiene["layer_inspection"]["offenders"])
        self.assertGreater(hygiene["layer_inspection"]["layers_inspected"], 0)
        self.assertGreater(hygiene["layer_inspection"]["entries_inspected"], 0)

    def test_it_tracks_the_current_build_definition(self) -> None:
        """A stale lock must not describe a Dockerfile or entrypoint that changed."""
        import hashlib

        def digest(path: Path) -> str:
            return hashlib.sha256(path.read_bytes()).hexdigest()

        source = self.lock["source"]
        self.assertEqual(digest(ROOT / "Dockerfile"), source["dockerfile_sha256"])
        self.assertEqual(
            digest(ROOT / "runtime" / "af3_runtime.py"), source["entrypoint_sha256"]
        )
        self.assertEqual(
            digest(ROOT / "contracts" / "af3-parameter-binding.json"),
            source["parameter_binding_sha256"],
        )
        self.assertEqual(
            digest(ROOT / "contracts" / "af3-runtime-source-lock.json"),
            source["source_lock_sha256"],
        )

    def test_publication_records_a_digest_without_a_registry_path(self) -> None:
        publication = self.lock["publication"]
        self.assertEqual("published", publication["state"])
        self.assertRegex(publication["digest"], r"^sha256:[0-9a-f]{64}$")
        self.assertEqual("withheld", publication["repository"])
        self.assertFalse(publication["overwrote_existing_tag"])
        self.assertTrue(publication["published_at"])

    def test_the_lock_and_the_handoff_name_the_same_published_image(self) -> None:
        """Consumers read the handoff; it must not drift from the lock."""
        handoff = contract("af3-runtime-handoff.json")["image"]
        publication = self.lock["publication"]
        self.assertEqual(publication["digest"], handoff["digest"])
        self.assertEqual(publication["tag"], handoff["tag"])
        self.assertEqual("published", handoff["digest_state"])

    def test_the_smoke_block_does_not_claim_a_gpu_run(self) -> None:
        smoke = self.lock["smoke"]
        self.assertEqual("3.0.4", smoke["distribution_version"])
        self.assertEqual(0, smoke["entrypoint_exit_code"])
        self.assertFalse(smoke["gpu_present"])
        self.assertIn("Offline probes only", smoke["note"])


class SourceLockTests(unittest.TestCase):
    def setUp(self) -> None:
        self.lock = contract("af3-runtime-source-lock.json")

    def test_the_upstream_identity_is_exact(self) -> None:
        upstream = self.lock["upstream"]
        self.assertEqual(UPSTREAM_COMMIT, upstream["commit"])
        self.assertEqual(UPSTREAM_TREE, upstream["tree"])
        self.assertEqual("v3.0.4", upstream["release_tag"])
        self.assertEqual("3.0.4", upstream["version"])
        self.assertTrue(upstream["tag_resolves_to_commit"])

    def test_the_access_classes_are_stated_truthfully(self) -> None:
        access = self.lock["access_class"]
        self.assertEqual("public-apache-2.0", access["source"])
        self.assertEqual("academic-restricted-operator-obtained", access["parameters"])
        self.assertIn("never embedded", access["notes"])

    def test_nondeterministic_inputs_are_disclosed_rather_than_hidden(self) -> None:
        reproducibility = self.lock["reproducibility"]
        self.assertTrue(reproducibility["nondeterministic_inputs"])
        self.assertIn("apt", reproducibility["nondeterministic_inputs"][0])
        self.assertIn("build receipt", reproducibility["mitigation"])
        self.assertEqual("linux/amd64", reproducibility["platform"])

    def test_the_upstream_deviation_is_documented(self) -> None:
        environment = self.lock["python_environment"]
        self.assertIn("--no-dev", environment["sync_flags"])
        self.assertIn("all-groups", environment["deviation_from_upstream"])

    def test_no_cache_level_above_l1_is_claimed(self) -> None:
        levels = self.lock["cache_levels"]
        self.assertIn("image-local", levels["L1"])
        self.assertIn("not a GPU memory snapshot", levels["auxiliary_compiler_cache"])
        self.assertIn("No L2 or higher", levels["unclaimed"])


if __name__ == "__main__":
    unittest.main()
