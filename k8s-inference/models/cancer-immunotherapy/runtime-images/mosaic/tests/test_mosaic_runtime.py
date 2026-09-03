#!/usr/bin/env python3
"""Offline contract tests for the Mosaic scientific-batch runtime image.

These tests never touch a cluster, a GPU or the registry. They prove that the
image inputs are internally consistent, that the runtime honours the canonical
``mosaic-batch`` argv contract byte for byte, and that what the runtime writes
is accepted by the canonical adapter's own output validator.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any


HERE = Path(__file__).resolve().parent
MOSAIC = HERE.parent
REPO = MOSAIC.parents[4]
LOCK = json.loads((MOSAIC / "image-lock.json").read_text(encoding="utf-8"))
ADAPTER = LOCK["adapter"]
QUALIFICATION = MOSAIC / "qualification"
CATALOG_RUNTIME = REPO / "k8s-inference" / "catalog" / "runtime"
if str(CATALOG_RUNTIME) not in sys.path:
    sys.path.insert(0, str(CATALOG_RUNTIME))


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _git_blob(revision: str, path: str) -> bytes:
    return subprocess.run(
        ["git", "-C", str(REPO), "show", f"{revision}:{path}"],
        check=True,
        stdout=subprocess.PIPE,
    ).stdout


def _load_module(name: str, source: bytes | Path):
    if isinstance(source, Path):
        payload = source.read_bytes()
    else:
        payload = source
    handle = tempfile.NamedTemporaryFile("wb", suffix=f"_{name}.py", delete=False)
    handle.write(payload)
    handle.close()
    spec = importlib.util.spec_from_file_location(name, handle.name)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


RUNTIME = _load_module("mosaic_runtime_entrypoint", MOSAIC / "runtime_entrypoint.py")
CANONICAL = _load_module(
    "canonical_mosaic_adapter",
    _git_blob(ADAPTER["commit"], f"{ADAPTER['repository_path']}/batch_adapter.py"),
)

TARGET_FASTA = (QUALIFICATION / "target-minibinder.fasta").read_bytes()
REQUEST = json.loads((QUALIFICATION / "mosaic-request.json").read_text(encoding="utf-8"))
INPUT_MANIFEST = json.loads(
    (QUALIFICATION / "mosaic-input-manifest.json").read_text(encoding="utf-8")
)
SEQUENCE = "VGLALYCLWPELFDGDAEEHHDEEALSEGKLPNEAFLAIL"
# strong_sha256 rejects repeated-character placeholders, so tests use real-shaped digests.
PLAUSIBLE_DIGEST = "sha256:" + hashlib.sha256(b"mosaic-test-image").hexdigest()
OTHER_DIGEST = "sha256:" + hashlib.sha256(b"mosaic-other-image").hexdigest()


def _artifact_loader(artifact_id: str) -> bytes:
    if artifact_id != "artifact.mosaic.target.minibinder":
        raise KeyError(artifact_id)
    return TARGET_FASTA


class _Structure:
    """Minimal stand-in for the Gemmi structure the runtime serialises."""

    def __init__(self, residues: int, *, chain: str = "A", degenerate: bool = False) -> None:
        self._residues = residues
        self._chain = chain
        self._degenerate = degenerate

    def make_pdb_string(self) -> str:
        lines = []
        serial = 0
        for index in range(self._residues):
            base = 0.0 if self._degenerate else index * 3.8
            for name, element, offset in (
                ("N", "N", 0.0), ("CA", "C", 1.2), ("C", "C", 2.4), ("O", "O", 2.9), ("CB", "C", 1.6)
            ):
                serial += 1
                x = base + (0.0 if self._degenerate else offset)
                lines.append(
                    f"ATOM  {serial:>5}  {name:<3}UNK {self._chain}{index + 1:>4}    "
                    f"{x:>8.3f}{0.0:>8.3f}{0.0:>8.3f}  1.00100.00           {element}  "
                )
        return "\n".join(["MODEL     1", *lines, "ENDMDL", "END"])


class ImageLockConsistency(unittest.TestCase):
    def test_dependency_lock_digest_matches_the_file(self) -> None:
        lock = LOCK["image"]["dependency_lock"]
        self.assertEqual(_sha256(MOSAIC / lock["path"]), lock["sha256"])

    def test_runtime_entrypoint_digest_matches_the_file(self) -> None:
        entrypoint = LOCK["image"]["runtime_entrypoint"]
        self.assertEqual(_sha256(MOSAIC / entrypoint["path"]), entrypoint["sha256"])

    def test_target_tag_is_derived_from_the_pinned_identities(self) -> None:
        image = LOCK["image"]
        expected = (
            f"{image['registry']}/{image['repository']}"
            f":{LOCK['source']['revision']}-{image['tag_suffix']}"
        )
        self.assertEqual(image["target_tag"], expected)

    def test_every_superseded_publication_is_marked_undeployable_with_a_reason(self) -> None:
        self.assertTrue(LOCK["image"]["supersedes"])
        for entry in LOCK["image"]["supersedes"]:
            self.assertFalse(entry["deployable"])
            self.assertGreater(len(entry["reason"]), 40)
            self.assertRegex(entry["digest"], r"^sha256:[0-9a-f]{64}$")

    def test_no_superseded_tag_reuses_the_current_tag_suffix(self) -> None:
        current = LOCK["image"]["tag_suffix"]
        self.assertNotIn(current, [entry["tag_suffix"] for entry in LOCK["image"]["supersedes"]])

    def test_published_digest_is_absent_or_well_formed(self) -> None:
        digest = LOCK["image"]["published_digest"]
        if digest is not None:
            self.assertRegex(digest, r"^sha256:[0-9a-f]{64}$")


class CanonicalAdapterBinding(unittest.TestCase):
    def test_pinned_adapter_files_match_the_candidate_commit(self) -> None:
        for relative, expected in ADAPTER["files"].items():
            payload = _git_blob(ADAPTER["commit"], f"{ADAPTER['repository_path']}/{relative}")
            self.assertEqual(hashlib.sha256(payload).hexdigest(), expected, relative)

    def test_runtime_identity_constants_match_the_canonical_adapter(self) -> None:
        self.assertEqual(RUNTIME.BACKEND_ID, CANONICAL.ADAPTER_ID)
        self.assertEqual(RUNTIME.SOURCE_REVISION, CANONICAL.SOURCE_REVISION)
        self.assertEqual(RUNTIME.RECIPE_SHA256, CANONICAL.RECIPE_SHA256)
        self.assertEqual(RUNTIME.SOURCE_REVISION, LOCK["source"]["revision"])

    def test_runtime_canonical_bytes_match_the_catalog_encoding(self) -> None:
        from fs2_serve_catalog.artifacts import canonical_bytes

        self.assertEqual(RUNTIME._canonical(REQUEST), canonical_bytes(REQUEST))

    def test_runtime_accepts_the_generated_argv_for_both_stages(self) -> None:
        plan = CANONICAL.render_plan(
            REQUEST,
            INPUT_MANIFEST,
            artifact_loader=_artifact_loader,
            runtime_image=f"{LOCK['image']['registry']}/mosaic@{PLAUSIBLE_DIGEST}",
            operation_id="op.test",
            workload_id="w-test",
            attempt_id="attempt-1",
            tenant_id="tenant-test",
            local_queue="inference-models",
        )
        parser = RUNTIME._parser()
        for node in plan["nodes"]:
            command = node["job"]["spec"]["template"]["spec"]["containers"][0]["command"]
            self.assertEqual(command[0], "/opt/fs2/bin/mosaic-batch")
            parsed = parser.parse_args(command[1:])
            self.assertIn(parsed.action, {"run-shard", "aggregate"})

    def test_runtime_rejects_argv_outside_the_contract(self) -> None:
        parser = RUNTIME._parser()
        for argv in (
            ["run-shard", "--request", "r", "--eval", "print(1)"],
            ["train", "--request", "r"],
            ["aggregate", "--request", "r"],
        ):
            with self.assertRaises(SystemExit):
                parser.parse_args(argv)


class BinderStructureSerialisation(unittest.TestCase):
    """The adapter-v2 defect: UNK residues lose the designed identity."""

    def test_designed_residue_names_replace_the_gemmi_placeholders(self) -> None:
        payload = RUNTIME._binder_pdb(_Structure(len(SEQUENCE)), SEQUENCE)
        self.assertNotIn(b"UNK", payload)
        recovered, residues = CANONICAL._pdb(payload)
        self.assertEqual(recovered, SEQUENCE)
        self.assertEqual(residues, len(SEQUENCE))

    def test_canonical_validator_rejects_the_unnamed_serialisation(self) -> None:
        raw = _Structure(len(SEQUENCE)).make_pdb_string().encode("ascii")
        with self.assertRaises(Exception):
            CANONICAL._pdb(raw)

    def test_residue_count_mismatch_fails_closed(self) -> None:
        with self.assertRaises(SystemExit):
            RUNTIME._binder_pdb(_Structure(len(SEQUENCE) - 1), SEQUENCE)

    def test_degenerate_coordinates_fail_the_canonical_validator(self) -> None:
        payload = RUNTIME._binder_pdb(_Structure(len(SEQUENCE), degenerate=True), SEQUENCE)
        with self.assertRaises(Exception):
            CANONICAL._pdb(payload)


class AggregateOutputContract(unittest.TestCase):
    DIGEST = PLAUSIBLE_DIGEST

    def _shard_tree(self, root: Path) -> None:
        shard = root / "shards" / "000"
        shard.mkdir(parents=True)
        (shard / "shard-result.json").write_bytes(RUNTIME._canonical({
            "backend_id": RUNTIME.BACKEND_ID,
            "source_revision": RUNTIME.SOURCE_REVISION,
            "recipe_sha256": RUNTIME.RECIPE_SHA256,
            "index": 0,
            "seed": REQUEST["parameters"]["base_seed"],
            "status": "succeeded",
        }))
        (shard / "candidate-metrics.json").write_bytes(RUNTIME._canonical({
            "candidate_id": "design-000",
            "shard_index": 0,
            "seed": REQUEST["parameters"]["base_seed"],
            "sequence": SEQUENCE,
            "iptm": 0.42,
            "mean_plddt": 0.81,
            "objective": 12.36,
        }))
        (shard / "candidate.pdb").write_bytes(
            RUNTIME._binder_pdb(_Structure(len(SEQUENCE)), SEQUENCE)
        )

    def _aggregate(self, root: Path, digest: str | None) -> None:
        request = root / "request.json"
        manifest = root / "input-manifest.json"
        request.write_text(json.dumps(REQUEST), encoding="utf-8")
        manifest.write_text(json.dumps(INPUT_MANIFEST), encoding="utf-8")
        previous = os.environ.get("FS2_RUNTIME_IMAGE_DIGEST")
        if digest is None:
            os.environ.pop("FS2_RUNTIME_IMAGE_DIGEST", None)
        else:
            os.environ["FS2_RUNTIME_IMAGE_DIGEST"] = digest
        try:
            RUNTIME._aggregate(RUNTIME._parser().parse_args([
                "aggregate",
                "--request", str(request),
                "--input-manifest", str(manifest),
                "--shards", str(root / "shards"),
                "--expected-shards", "1",
                "--staging-manifest", str(root / "output-manifest.json.tmp"),
                "--output-manifest", str(root / "output-manifest.json"),
                "--atomic-rename",
            ]))
        finally:
            os.environ.pop("FS2_RUNTIME_IMAGE_DIGEST", None)
            if previous is not None:
                os.environ["FS2_RUNTIME_IMAGE_DIGEST"] = previous

    def test_committed_manifest_passes_the_canonical_output_validator(self) -> None:
        with tempfile.TemporaryDirectory() as handle:
            root = Path(handle)
            self._shard_tree(root)
            self._aggregate(root, self.DIGEST)
            manifest = json.loads((root / "output-manifest.json").read_text(encoding="utf-8"))
            index = json.loads((root / "artifact-index.json").read_text(encoding="utf-8"))

            def loader(artifact_id: str) -> bytes:
                if artifact_id in index:
                    return Path(index[artifact_id]).read_bytes()
                return _artifact_loader(artifact_id)

            receipt = CANONICAL.validate_output_manifest(
                REQUEST,
                INPUT_MANIFEST,
                manifest,
                artifact_loader=loader,
                expected_runtime_image_digest=self.DIGEST,
            )
            self.assertEqual(receipt["status"], "passed")
            self.assertEqual(receipt["candidate_count"], 1)
            self.assertEqual(receipt["shard_count"], 1)
            self.assertEqual(receipt["request_sha256"], CANONICAL.request_digest(REQUEST))

    def test_staging_manifest_is_renamed_and_leaves_no_partial_file(self) -> None:
        with tempfile.TemporaryDirectory() as handle:
            root = Path(handle)
            self._shard_tree(root)
            self._aggregate(root, self.DIGEST)
            self.assertTrue((root / "output-manifest.json").is_file())
            self.assertFalse((root / "output-manifest.json.tmp").exists())

    def test_aggregate_fails_closed_without_the_admitted_image_digest(self) -> None:
        with tempfile.TemporaryDirectory() as handle:
            root = Path(handle)
            self._shard_tree(root)
            with self.assertRaises(SystemExit):
                self._aggregate(root, None)

    def test_aggregate_rejects_a_malformed_image_digest(self) -> None:
        with tempfile.TemporaryDirectory() as handle:
            root = Path(handle)
            self._shard_tree(root)
            with self.assertRaises(SystemExit):
                self._aggregate(root, "sha256:short")

    def test_a_different_admitted_digest_is_rejected_by_the_validator(self) -> None:
        with tempfile.TemporaryDirectory() as handle:
            root = Path(handle)
            self._shard_tree(root)
            self._aggregate(root, self.DIGEST)
            manifest = json.loads((root / "output-manifest.json").read_text(encoding="utf-8"))
            index = json.loads((root / "artifact-index.json").read_text(encoding="utf-8"))
            with self.assertRaises(Exception):
                CANONICAL.validate_output_manifest(
                    REQUEST,
                    INPUT_MANIFEST,
                    manifest,
                    artifact_loader=lambda item: (Path(index[item]).read_bytes() if item in index else _artifact_loader(item)),
                    expected_runtime_image_digest=OTHER_DIGEST,
                )


class ExternalArtifactPolicy(unittest.TestCase):
    def test_dockerfile_deletes_every_upstream_checkpoint_before_install(self) -> None:
        dockerfile = (MOSAIC / "Dockerfile").read_text(encoding="utf-8")
        self.assertIn("src/mosaic/proteinmpnn/weights", dockerfile)
        self.assertIn("-delete", dockerfile)
        self.assertFalse(LOCK["weight_policy"]["embedded"])

    def test_dockerfile_defaults_every_cache_under_tmp(self) -> None:
        dockerfile = (MOSAIC / "Dockerfile").read_text(encoding="utf-8")
        for variable, value in LOCK["image"]["cache_policy"]["variables"].items():
            self.assertIn(f"{variable}={value}", dockerfile)
            self.assertTrue(value.startswith("/tmp/"), variable)

    def test_runtime_verifies_every_external_checkpoint_by_digest(self) -> None:
        source = (MOSAIC / "runtime_entrypoint.py").read_text(encoding="utf-8")
        self.assertIn("_verified(", source)
        for artifact in LOCK["external_artifacts"]:
            digest = artifact.get("sha256")
            if digest:
                self.assertIn(digest, source)

    def test_transitional_artifact_delivery_is_declared(self) -> None:
        delivery = LOCK["artifact_delivery"]
        self.assertEqual(delivery["state"], "transitional-task-scoped")
        self.assertEqual(delivery["canonical_plane"], "unavailable")

    def test_recorded_contract_defects_carry_a_resolution(self) -> None:
        self.assertTrue(LOCK["upstream_contract_defects"])
        for defect in LOCK["upstream_contract_defects"]:
            for field in ("id", "severity", "location", "observed", "actual", "impact", "resolution"):
                self.assertTrue(defect[field])


class QualificationFixtures(unittest.TestCase):
    def test_target_fasta_matches_its_content_addressed_pointer(self) -> None:
        pointer = INPUT_MANIFEST["entries"][0]["artifact"]
        self.assertEqual(len(TARGET_FASTA), pointer["size_bytes"])
        self.assertEqual(hashlib.sha256(TARGET_FASTA).hexdigest(), pointer["sha256"])

    def test_request_is_accepted_by_the_canonical_adapter(self) -> None:
        request, manifest = CANONICAL.validate_request(
            REQUEST, INPUT_MANIFEST, artifact_loader=_artifact_loader
        )
        self.assertEqual(request["operation"], "design-binder")
        self.assertEqual(manifest["manifest_id"], INPUT_MANIFEST["manifest_id"])

    def test_hotspots_are_inside_the_target_and_converted_to_zero_based(self) -> None:
        sequence = TARGET_FASTA.decode("ascii").splitlines()[1]
        for hotspot in REQUEST["parameters"]["hotspots"]:
            self.assertTrue(1 <= hotspot <= len(sequence))
        source = (MOSAIC / "runtime_entrypoint.py").read_text(encoding="utf-8")
        self.assertIn('epitope_idx=[item - 1 for item in parameters["hotspots"]]', source)

    def test_recipe_objective_matches_the_runtime_loss(self) -> None:
        recipe = json.loads(
            _git_blob(ADAPTER["commit"], f"{ADAPTER['repository_path']}/recipe.json")
        )
        source = (MOSAIC / "runtime_entrypoint.py").read_text(encoding="utf-8")
        weights = {term["term"]: term["weight"] for term in recipe["objective"]}
        self.assertIn(f"{weights['BinderTargetContact']} * BinderTargetContact", source)
        self.assertIn(f"{weights['PLDDTLoss']} * PLDDTLoss()", source)
        self.assertIn(f"{weights['InverseFoldingSequenceRecovery']} * InverseFoldingSequenceRecovery", source)
        self.assertIn("WithinBinderContact()", source)
        self.assertIn(f"recycling_steps={recipe['features']['recycling_steps']}", source)
        self.assertIn(f"sampling_steps={recipe['features']['diffusion_sampling_steps']}", source)
        self.assertIn(f"stepsize={recipe['optimizer']['step_size']}", source)
        self.assertIn(f"momentum={recipe['optimizer']['momentum']}", source)


if __name__ == "__main__":
    unittest.main(verbosity=2)
