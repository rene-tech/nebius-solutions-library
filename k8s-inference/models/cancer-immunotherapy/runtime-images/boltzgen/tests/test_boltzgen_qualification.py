from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import tarfile
import tempfile
import unittest
from pathlib import Path

import gemmi
import numpy as np

HERE = Path(__file__).resolve().parents[1]
QUALIFICATION = HERE / "qualification"


def load(name: str):
    path = QUALIFICATION / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


fetch_target = load("fetch_target")
localize = load("localize_checkpoints")
render_job = load("render_job")
runtime_driver = load("runtime_driver")
validator = load("validate_result")


def atom(name: str, x: float, y: float, z: float) -> gemmi.Atom:
    value = gemmi.Atom()
    value.name = name
    value.element = gemmi.Element(name[0])
    value.pos = gemmi.Position(x, y, z)
    return value


def protein_chain(name: str, *, count: int, start: int, y: float) -> gemmi.Chain:
    chain = gemmi.Chain(name)
    for index in range(count):
        residue = gemmi.Residue()
        residue.name = "ALA"
        residue.seqid = gemmi.SeqId(start + index, " ")
        residue.het_flag = "A"
        x = index * 3.8
        residue.add_atom(atom("N", x - 1.2, y, 0.0))
        residue.add_atom(atom("CA", x, y, 0.0))
        residue.add_atom(atom("C", x + 1.2, y, 0.0))
        chain.add_residue(residue)
    return chain


def write_structure(path: Path, chains: list[gemmi.Chain]) -> None:
    structure = gemmi.Structure()
    structure.name = "boltzgen_test"
    model = gemmi.Model("1")
    for chain in chains:
        model.add_chain(chain)
    structure.add_model(model)
    path.write_text(structure.make_mmcif_document().as_string(), encoding="utf-8")


class ImageContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.lock = json.loads((HERE / "image-lock.json").read_text(encoding="utf-8"))

    def test_build_inputs_match_lock(self) -> None:
        inputs = self.lock["image"]["build_inputs"]
        for name, digest_key in (
            ("Dockerfile", "dockerfile_sha256"),
            ("requirements.lock", "dependency_lock_sha256"),
        ):
            observed = hashlib.sha256((HERE / name).read_bytes()).hexdigest()
            self.assertEqual(observed, inputs[digest_key])

    def test_target_projection_normalizes_both_mmcif_chain_namespaces(self) -> None:
        structure = gemmi.Structure()
        model = gemmi.Model("1")
        chain = protein_chain("A", count=100, start=1, y=0.0)
        for residue in chain:
            residue.subchain = "B"
        model.add_chain(chain)
        structure.add_model(model)
        source = structure.make_mmcif_document().as_string().encode("utf-8")

        projected = fetch_target.project(source)
        block = gemmi.cif.read_string(projected.decode("utf-8")).sole_block()
        atom_site = block.find(["_atom_site.label_asym_id", "_atom_site.auth_asym_id"])

        self.assertEqual({(row[0], row[1]) for row in atom_site}, {("A", "A")})

    def test_every_live_identity_is_immutable(self) -> None:
        self.assertRegex(self.lock["image"]["digest"], r"^sha256:[0-9a-f]{64}$")
        if self.lock["artifacts"]["boltzgen-checkpoints"]["generation"] is None:
            self.skipTest("checkpoint publication is still in progress")
        for artifact in self.lock["artifacts"].values():
            self.assertRegex(artifact["generation"], r"^[0-9a-f]{64}$")
            self.assertRegex(artifact["marker_sha256"], r"^[0-9a-f]{64}$")
        self.assertFalse(self.lock["route_exposed"])

    def test_adapter_compiles_exact_runtime_entrypoint(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            target = Path(raw) / "target.cif"
            target.write_bytes(b"synthetic-target")
            self.lock["input"]["projected_sha256"] = hashlib.sha256(target.read_bytes()).hexdigest()
            self.lock["input"]["projected_bytes"] = target.stat().st_size
            plan, _yaml, _target, input_bundle = render_job.compile_plan(scenario="cold", target=target, lock=self.lock)
            second_plan, _yaml, _target, second_bundle = render_job.compile_plan(
                scenario="cold", target=target, lock=self.lock
            )
        self.assertEqual(plan["configure_argv"][:2], ["boltzgen", "configure"])
        self.assertEqual(plan["design_argv"][:2], ["boltzgen", "execute"])
        self.assertEqual(plan["design_argv"][-2:], ["--steps", "design"])
        self.assertEqual(
            plan["runtime_artifacts"],
            ["boltzgen-checkpoints", "boltzgen-inference-molecules"],
        )
        self.assertEqual(plan["scheduling"]["workload_priority_value"], -100)

        request = plan["request"]
        manifest = plan["scientific_manifest"]
        campaign_pointer = manifest["entries"][0]["artifact"]
        manifest_pointer = request["input_manifest"]
        self.assertNotIn("_input_manifest_note", request)
        self.assertNotEqual(campaign_pointer["artifact_id"], manifest_pointer["artifact_id"])
        self.assertEqual(campaign_pointer["media_type"], "application/gzip")
        self.assertEqual(campaign_pointer["compression"], "gzip")
        self.assertEqual(campaign_pointer["sha256"], render_job.sha256(input_bundle.campaign_payload))
        self.assertEqual(campaign_pointer["size_bytes"], len(input_bundle.campaign_payload))
        self.assertEqual(
            manifest_pointer["media_type"],
            "application/vnd.fs2.scientific-manifest+json",
        )
        self.assertEqual(manifest_pointer["compression"], "none")
        self.assertEqual(
            input_bundle.scientific_manifest_payload,
            render_job.canonical_json(manifest),
        )
        self.assertEqual(
            manifest_pointer["sha256"],
            render_job.sha256(input_bundle.scientific_manifest_payload),
        )
        self.assertEqual(
            manifest_pointer["size_bytes"],
            len(input_bundle.scientific_manifest_payload),
        )
        self.assertEqual(plan["input_artifact_sha256"], campaign_pointer["sha256"])
        self.assertEqual(plan["input_manifest_sha256"], manifest_pointer["sha256"])
        self.assertNotEqual(plan["input_artifact_sha256"], plan["input_manifest_sha256"])
        self.assertEqual(input_bundle.campaign_payload, second_bundle.campaign_payload)
        self.assertEqual(
            input_bundle.scientific_manifest_payload,
            second_bundle.scientific_manifest_payload,
        )
        self.assertEqual(plan["request_sha256"], second_plan["request_sha256"])
        self.assertTrue(input_bundle.campaign_payload.startswith(b"\x1f\x8b"))
        with tarfile.open(fileobj=io.BytesIO(input_bundle.campaign_payload), mode="r:gz") as archive:
            self.assertEqual(
                archive.getnames(),
                ["5J89-chain-A.cif", "design-specs/pdl1-face.yaml"],
            )

        catalog = json.loads(render_job.PROFILE_PATH.read_text(encoding="utf-8"))
        profile = render_job.profile_from_catalog(catalog, "boltzgen")
        execution = render_job.boltzgen.compile_run(
            profile,
            request,
            operation_id=plan["operation_id"],
            input_artifacts=input_bundle.input_artifacts,
        )
        self.assertEqual(execution.request_sha256, plan["request_sha256"])
        configure = execution.invocation("configure", "pdl1-face")
        self.assertEqual(configure.consumes, ("campaign-input",))
        self.assertEqual(configure.materializations[0].artifact_id, "campaign-input")
        self.assertEqual(configure.materializations[0].compression, "gzip")

    def test_render_is_queued_offline_and_generation_pinned(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            target = root / "target.cif"
            target.write_bytes(b"synthetic-target")
            self.lock["input"]["projected_sha256"] = hashlib.sha256(target.read_bytes()).hexdigest()
            self.lock["input"]["projected_bytes"] = target.stat().st_size
            lock_path = root / "lock.json"
            lock_path.write_text(json.dumps(self.lock), encoding="utf-8")
            document = render_job.render(
                scenario="cold",
                target=target,
                node_name="computeinstance-example",
                pool_id="h100-1x",
                lock_path=lock_path,
            )
        items = {item["kind"]: item for item in document["items"] if item["kind"] != "ConfigMap"}
        job = items["Job"]
        self.assertTrue(job["spec"]["suspend"])
        self.assertEqual(job["metadata"]["labels"]["kueue.x-k8s.io/queue-name"], "inference-models")
        self.assertEqual(job["metadata"]["labels"]["fs2.nebius.ai/attempt-id"], "attempt-cold-04")
        self.assertEqual(job["metadata"]["annotations"]["fs2.nebius.ai/workload-priority-value"], "-100")
        container = job["spec"]["template"]["spec"]["containers"][0]
        self.assertNotIn(container["command"][0], {"sh", "bash", "/bin/sh", "/bin/bash"})
        self.assertIn("@sha256:", container["image"])
        volumes = {item["name"]: item for item in job["spec"]["template"]["spec"]["volumes"]}
        for artifact_id in ("checkpoints", "molecules"):
            self.assertIn(
                self.lock["artifacts"][
                    f"boltzgen-{artifact_id if artifact_id == 'checkpoints' else 'inference-molecules'}"
                ]["generation"],
                volumes[artifact_id]["hostPath"]["path"],
            )
        policy = items["NetworkPolicy"]
        self.assertEqual(policy["spec"]["egress"], [])
        self.assertEqual(policy["spec"]["ingress"], [])


class LocalizationTests(unittest.TestCase):
    def test_completed_staging_is_reused_and_marker_uses_physical_root(self) -> None:
        payloads = {"a.ckpt": b"checkpoint-a", "b.ckpt": b"checkpoint-b"}
        contract = {
            "source_uri": "https://example.invalid/boltzgen",
            "source_revision": "revision",
            "source_url_template": "https://example.invalid/{revision}/{name}",
            "license": "MIT",
            "mount_path": "/opt/fs2/artifacts/boltzgen-checkpoints",
            "total_bytes": sum(map(len, payloads.values())),
            "files": [
                {
                    "path": name,
                    "bytes": len(payload),
                    "sha256": hashlib.sha256(payload).hexdigest(),
                }
                for name, payload in payloads.items()
            ],
        }
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            staging = root / ".staging" / "test"
            staging.mkdir(parents=True)
            for name, payload in payloads.items():
                (staging / name).write_bytes(payload)
            receipt = localize.localize(
                lock={"artifacts": {"boltzgen-checkpoints": contract}},
                host_root=root,
                physical_host_root="/canonical/physical/root",
                staging_name="test",
            )
            self.assertEqual(receipt["state"], "published")
            self.assertEqual(receipt["reused_bytes"], contract["total_bytes"])
            generation = root / receipt["sub_path"]
            marker = json.loads((generation / localize.MARKER).read_text(encoding="utf-8"))
            self.assertEqual(marker["host_root"], "/canonical/physical/root")
            self.assertEqual(marker["generation"], receipt["generation"])

    def test_runtime_mount_alias_is_verified_by_marker_not_directory_name(self) -> None:
        marker = {
            "generation": "a" * 64,
            "entry_count": 1,
            "total_bytes": 12,
            "read_only": True,
            "visibility": "public",
            "inventory_algorithm": "fs2-flat-tree-inventory/v1",
            "host_root": "/mnt/fs2-reference-data/data",
            "sub_path": "scientific-localization/public/generations/example/sha256/" + "a" * 64,
        }
        payload = (json.dumps(marker, sort_keys=True, separators=(",", ":")) + "\n").encode()
        contract = {
            "generation": marker["generation"],
            "marker_sha256": hashlib.sha256(payload).hexdigest(),
            "entry_count": marker["entry_count"],
            "total_bytes": marker["total_bytes"],
        }
        with tempfile.TemporaryDirectory() as raw:
            mount_alias = Path(raw) / "boltzgen-checkpoints"
            mount_alias.mkdir()
            (mount_alias / runtime_driver.MARKER).write_bytes(payload)
            receipt = runtime_driver.verify_marker(mount_alias, contract)
        self.assertEqual(receipt["generation"], marker["generation"])


class SemanticValidatorTests(unittest.TestCase):
    def make_case(self, root: Path, *, binder_y: float, binder_name: str = "C") -> tuple[Path, Path]:
        target = root / "target.cif"
        workspace = root / "workspace"
        output = workspace / "intermediate_designs"
        output.mkdir(parents=True)
        target_chain = protein_chain("A", count=127, start=1, y=0.0)
        write_structure(target, [target_chain])
        complex_target = protein_chain("A", count=127, start=1, y=0.0)
        # Start the designed chain beside A:54 so the positive fixture contacts
        # the exact binding-site IDs, not an arbitrary part of the target.
        binder = protein_chain(binder_name, count=60, start=1, y=binder_y)
        for residue in binder:
            for value in residue:
                value.pos.x += 53 * 3.8
        structure = output / "pdl1-face.cif"
        write_structure(structure, [complex_target, binder])
        np.savez_compressed(
            structure.with_suffix(".npz"),
            design_mask=np.asarray([False] * 127 + [True] * 60),
            mol_type=np.zeros(187, dtype=np.int8),
        )
        return workspace, target

    def test_physical_pdl1_interface_passes(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            workspace, target = self.make_case(Path(raw), binder_y=5.0)
            report = validator.validate(workspace, target)
            self.assertEqual(report["status"], "passed")
            self.assertEqual(report["binder"]["protein_residues"], 60)
            self.assertGreaterEqual(report["interface"]["binder_residues_within_12_angstrom"], 3)

    def test_runtime_canonicalized_binder_chain_b_passes(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            workspace, target = self.make_case(Path(raw), binder_y=5.0, binder_name="B")
            report = validator.validate(workspace, target)
            self.assertEqual(report["status"], "passed")
            self.assertEqual(report["binder"]["chain"], "B")

    def test_runtime_can_relabel_binder_a_and_target_b(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            target = root / "target.cif"
            workspace = root / "workspace"
            output = workspace / "intermediate_designs"
            output.mkdir(parents=True)
            source_target = protein_chain("A", count=127, start=1, y=0.0)
            write_structure(target, [source_target])
            output_target = protein_chain("B", count=127, start=1, y=0.0)
            binder = protein_chain("A", count=60, start=1, y=5.0)
            for residue in binder:
                for value in residue:
                    value.pos.x += 53 * 3.8
            structure = output / "pdl1-face.cif"
            write_structure(structure, [binder, output_target])
            np.savez_compressed(
                structure.with_suffix(".npz"),
                design_mask=np.asarray([True] * 60 + [False] * 127),
                mol_type=np.zeros(187, dtype=np.int8),
            )

            report = validator.validate(workspace, target)

            self.assertEqual(report["target_projection"]["output_chain"], "B")
            self.assertEqual(report["binder"]["chain"], "A")

    def test_distant_binder_fails(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            workspace, target = self.make_case(Path(raw), binder_y=50.0)
            with self.assertRaises(validator.ValidationError):
                validator.validate(workspace, target)


if __name__ == "__main__":
    unittest.main(verbosity=2)
