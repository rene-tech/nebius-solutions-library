from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
import unittest
from pathlib import Path
from typing import Any

import jsonschema


MODEL_DIR = Path(__file__).resolve().parents[1]
REPO_DIR = Path(__file__).resolve().parents[5]
sys.path.insert(0, str(REPO_DIR / "catalog/runtime"))
from fs2_serve_catalog.artifacts import canonical_bytes  # noqa: E402

SPEC = importlib.util.spec_from_file_location("bindcraft_native_adapter", MODEL_DIR / "batch_adapter.py")
assert SPEC is not None and SPEC.loader is not None
adapter = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(adapter)

PDB = (REPO_DIR / "catalog/runtime/packaged-repository/nim-fast-start/faststart-v2/rfdiffusion-native/fixtures/1UBQ.pdb").read_bytes()
PDB_SEQUENCE = "MQIFVKTLTGKTITLEVEPSDTIENVKAKIQDKEGIPPDQQRLIFAGKQLEDGRTLSDYNIQKESTLHLVLRLRGG"
SCHEMA_DIR = REPO_DIR / "catalog/runtime/schema"


def fixture(name: str) -> dict[str, Any]:
    return json.loads((MODEL_DIR / "fixtures" / name).read_text())


def schema(name: str) -> dict[str, Any]:
    return json.loads((SCHEMA_DIR / name).read_text())


def add_artifact(store: dict[str, bytes], artifact_id: str, value: bytes, media_type: str) -> dict[str, Any]:
    store[artifact_id] = value
    return {
        "artifact_id": artifact_id,
        "sha256": hashlib.sha256(value).hexdigest(),
        "size_bytes": len(value),
        "media_type": media_type,
        "compression": "none",
    }


def output_for(envelope: dict[str, Any], *, scoring_engine: str = "pyrosetta") -> tuple[dict[str, Any], dict[str, bytes]]:
    request, admission = envelope["request"], envelope["admission"]
    count = request["parameters"]["shard_count"]
    runtime_digest = admission["runtime_image"].rsplit("@", 1)[1]
    store = {"artifact.bindcraft.target.1ubq": PDB}
    entries: list[dict[str, Any]] = []
    for index in range(count):
        shard = {
            "backend_id": adapter.ADAPTER_ID,
            "source_revision": adapter.SOURCE_REVISION,
            "index": index,
            "seed": request["parameters"]["base_seed"] + index,
            "status": "succeeded",
        }
        entries.append({
            "name": f"shard-{index:03d}",
            "semantic_type": "bindcraft-native-shard-result-json/v1",
            "artifact": add_artifact(store, f"artifact.bindcraft.native.shard.{index:03d}", canonical_bytes(shard), "application/json"),
        })
    aggregate = {
        "backend_id": adapter.ADAPTER_ID,
        "source_revision": adapter.SOURCE_REVISION,
        "access_profile": "academic",
        "academic_asset_id": adapter.ACADEMIC_ASSET_ID,
        "academic_artifact_sha256": adapter.ACADEMIC_ARTIFACT_SHA256,
        "request_sha256": adapter.request_digest(request),
        "runtime_image_digest": runtime_digest,
        "expected_shards": count,
        "succeeded_shards": count,
        "atomic_commit": True,
    }
    entries.append({
        "name": "aggregate",
        "semantic_type": "bindcraft-native-aggregate-json/v1",
        "artifact": add_artifact(store, "artifact.bindcraft.native.aggregate", canonical_bytes(aggregate), "application/json"),
    })
    metrics = {
        "candidate_id": "native-000",
        "shard_index": 0,
        "seed": request["parameters"]["base_seed"],
        "sequence": PDB_SEQUENCE,
        "scoring_engine": scoring_engine,
        "iptm": 0.83,
        "mean_plddt": 0.89,
        "interface_dg": -31.2,
        "shape_complementarity": 0.71,
        "interface_residue_count": 8,
        "buried_surface_area": 1200.0,
        "hotspot_geometry_validated": True,
    }
    entries.extend([
        {
            "name": "candidate-000-metrics",
            "semantic_type": "bindcraft-native-design-metrics-json/v1",
            "artifact": add_artifact(store, "artifact.bindcraft.native.candidate.000.metrics", canonical_bytes(metrics), "application/json"),
        },
        {
            "name": "candidate-000-structure",
            "semantic_type": "protein-structure-pdb/v1",
            "artifact": add_artifact(store, "artifact.bindcraft.native.candidate.000.pdb", PDB, "chemical/x-pdb"),
        },
    ])
    return {
        "schema": adapter.ARTIFACT_MANIFEST_SCHEMA,
        "manifest_id": "manifest.bindcraft.native.output.01",
        "entries": entries,
    }, store


class BindCraftNativeBatchAdapterTest(unittest.TestCase):
    def setUp(self) -> None:
        self.request_schema = schema("scientific-run-request.schema.json")
        self.manifest_schema = schema("scientific-artifact-manifest.schema.json")
        self.parameter_schema = json.loads((MODEL_DIR / "parameters.schema.json").read_text())

    def test_two_positive_fixtures_render_private_tree_shell_free_trajectory_dag(self) -> None:
        jsonschema.Draft202012Validator(schema("scientific-workload.schema.json")).validate(json.loads((MODEL_DIR / "workload.json").read_text()))
        for name in ("positive-pdl1.json", "positive-hotspot-panel.json"):
            with self.subTest(name=name):
                envelope = fixture(name)
                request, manifest, admission = envelope["request"], envelope["input_manifest"], envelope["admission"]
                jsonschema.Draft202012Validator(self.request_schema).validate(request)
                jsonschema.Draft202012Validator(self.parameter_schema).validate(request["parameters"])
                jsonschema.Draft202012Validator(self.manifest_schema).validate(manifest)
                plan = adapter.render_plan(request, manifest, artifact_loader={"artifact.bindcraft.target.1ubq": PDB}.__getitem__, **admission)
                count = request["parameters"]["shard_count"]
                self.assertEqual(plan["backend_id"], "bindcraft-v1-5-3-pyrosetta-academic")
                materialization = plan["academic_asset"]["materialization"]
                self.assertEqual(materialization, {
                    "kind": "ArtifactMaterialization",
                    "artifact_id": "bindcraft-pyrosetta-installed-tree",
                    "content_digest_sha256": "a93d68e198c81cbb87926e012dff6b50a73e99d9a41261e65f73d264c792aa8d",
                    "content_bytes": 3287122494,
                    "content_identity_kind": "tree-manifest",
                    "content_manifest_algorithm": "fs2-tree-manifest/v1",
                    "claim": "academic-assets-runtime-rwx",
                    "source_sub_path": "pyrosetta-bindcraft/site-packages",
                    "consumer_path": "/opt/fs2/academic/pyrosetta-bindcraft/site-packages",
                    "read_only": True,
                    "supplemental_group": 65532,
                })
                self.assertEqual(
                    plan["academic_asset"]["source_artifact"]["artifact_sha256"],
                    "4383d8d1a14fd3aff52983de936908791cc77bc6ac418e3bc53bb963a42c5242",
                )
                self.assertEqual(
                    plan["academic_asset"]["install_receipt_sha256"],
                    "9807d5f3ee952621d318bca2e1b942234e90492f8e414ea4060c2607b131cae4",
                )
                self.assertEqual(len(plan["nodes"]), count + 1)
                self.assertEqual(plan["nodes"][-1]["depends_on"], [f"trajectory-{index:03d}" for index in range(count)])
                for node in plan["nodes"]:
                    pod = node["job"]["spec"]["template"]["spec"]
                    self.assertEqual(node["job"]["spec"]["backoffLimit"], 0)
                    self.assertNotIn("initContainers", pod)
                    self.assertNotIn("fsGroup", pod["securityContext"])
                    self.assertEqual(pod["securityContext"]["supplementalGroups"], [65532])
                    academic = next(volume for volume in pod["volumes"] if volume["name"] == "academic-runtime")
                    self.assertEqual(academic["persistentVolumeClaim"], {
                        "claimName": "academic-assets-runtime-rwx", "readOnly": True,
                    })
                    container = pod["containers"][0]
                    mount = next(item for item in container["volumeMounts"] if item["name"] == "academic-runtime")
                    self.assertEqual(mount, {
                        "name": "academic-runtime",
                        "mountPath": "/opt/fs2/academic/pyrosetta-bindcraft/site-packages",
                        "subPath": "pyrosetta-bindcraft/site-packages",
                        "readOnly": True,
                    })
                    self.assertEqual(container["env"][0], {
                        "name": "PYTHONPATH",
                        "value": "/opt/fs2/academic/pyrosetta-bindcraft/site-packages:/opt/bindcraft",
                    })
                    self.assertEqual(container["env"][1], {
                        "name": "FS2_RUNTIME_IMAGE_DIGEST",
                        "value": admission["runtime_image"].rsplit("@", 1)[1],
                    })
                    argv = container["command"]
                    self.assertNotIn(argv[0], {"sh", "bash", "/bin/sh", "/bin/bash"})
                    self.assertFalse(any("$(" in token or ";" in token for token in argv))
                    annotations = node["job"]["metadata"]["annotations"]
                    self.assertEqual(annotations["fs2.nebius.ai/academic-asset-id"], "pyrosetta-bindcraft")
                    self.assertEqual(
                        annotations["fs2.nebius.ai/academic-materialization-sha256"],
                        materialization["content_digest_sha256"],
                    )
                    self.assertNotIn("fs2.nebius.ai/asset-access-receipt-digest", annotations)

    def test_native_request_cannot_substitute_the_open_backend(self) -> None:
        envelope = fixture("positive-pdl1.json")
        open_request = json.loads(json.dumps(envelope["request"]))
        open_request["parameters"]["schema"] = "fs2-serve.nebius.ai/bindcraft-open-freebindcraft-parameters/v1"
        with self.assertRaises(adapter.CatalogError):
            adapter.validate_request(open_request, envelope["input_manifest"], artifact_loader={"artifact.bindcraft.target.1ubq": PDB}.__getitem__)

    def test_two_semantic_outputs_require_asset_identity_and_pyrosetta(self) -> None:
        for name in ("positive-pdl1.json", "positive-hotspot-panel.json"):
            envelope = fixture(name)
            output, store = output_for(envelope)
            jsonschema.Draft202012Validator(self.manifest_schema).validate(output)
            validation = adapter.validate_output_manifest(
                envelope["request"], envelope["input_manifest"], output,
                artifact_loader=store.__getitem__,
                expected_runtime_image_digest=envelope["admission"]["runtime_image"].rsplit("@", 1)[1],
            )
            self.assertEqual(validation["status"], "passed")
            self.assertEqual(validation["academic_asset_id"], "pyrosetta-bindcraft")
        envelope = fixture("positive-pdl1.json")
        output, store = output_for(envelope, scoring_engine="openmm-freesasa")
        with self.assertRaises(adapter.CatalogError):
            adapter.validate_output_manifest(
                envelope["request"], envelope["input_manifest"], output,
                artifact_loader=store.__getitem__,
                expected_runtime_image_digest=envelope["admission"]["runtime_image"].rsplit("@", 1)[1],
            )

    def test_metadata_binds_qualified_private_tree_without_redistributed_payload(self) -> None:
        metadata = json.loads((MODEL_DIR / "adapter.json").read_text())
        lock = json.loads((MODEL_DIR / "artifact-lock.json").read_text())
        self.assertEqual(metadata["access"]["profile"], "academic")
        self.assertFalse(metadata["access"]["request_time_license_receipt_required"])
        self.assertEqual(metadata["access"]["runtime_binding"]["source_sub_path"], "pyrosetta-bindcraft/site-packages")
        self.assertEqual(metadata["access"]["runtime_binding"]["consumer_path"], "/opt/fs2/academic/pyrosetta-bindcraft/site-packages")
        self.assertEqual(metadata["access"]["runtime_binding"]["supplemental_group"], 65532)
        self.assertEqual(metadata["access"]["runtime_binding"]["artifact_id"], "bindcraft-pyrosetta-installed-tree")
        self.assertEqual(
            metadata["access"]["runtime_binding"]["content_digest_sha256"],
            "a93d68e198c81cbb87926e012dff6b50a73e99d9a41261e65f73d264c792aa8d",
        )
        self.assertEqual(
            metadata["access"]["deployment_evidence"]["authorization_receipt_sha256"],
            "5e3967f7f11b54c99f6a0f15c20dfdcc1c1d9e39fab4096d67781be275dba5ad",
        )
        self.assertEqual(metadata["qualification"]["h100"], "passed-full-post-design-semantic-run")
        self.assertEqual(lock["source_revision"], adapter.SOURCE_REVISION)
        self.assertFalse(metadata["access"]["credentials_in_request"])
        self.assertFalse(metadata["access"]["package_data_in_repository"])
        self.assertNotIn("credential_value", json.dumps(metadata).lower())
        self.assertFalse((MODEL_DIR / "bin" / "verify-academic-access").exists())
        self.assertNotIn("verify-academic-access", (MODEL_DIR / "Dockerfile").read_text())


if __name__ == "__main__":
    unittest.main()
