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
    receipt = admission["access_receipt_digest"]
    store = {"artifact.bindcraft.target.1ubq": PDB}
    entries: list[dict[str, Any]] = []
    for index in range(count):
        shard = {
            "backend_id": adapter.ADAPTER_ID,
            "source_revision": adapter.SOURCE_REVISION,
            "access_receipt_digest": receipt,
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
        "access_receipt_digest": receipt,
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

    def test_two_positive_fixtures_render_gated_shell_free_trajectory_dag(self) -> None:
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
                self.assertEqual(len(plan["nodes"]), count + 1)
                self.assertEqual(plan["nodes"][-1]["depends_on"], [f"trajectory-{index:03d}" for index in range(count)])
                for node in plan["nodes"]:
                    pod = node["job"]["spec"]["template"]["spec"]
                    self.assertEqual(node["job"]["spec"]["backoffLimit"], 0)
                    gate = pod["initContainers"][0]
                    self.assertEqual(gate["command"][0], "/opt/fs2/bin/verify-academic-access")
                    self.assertIn(admission["access_receipt_digest"], gate["command"])
                    self.assertNotIn("env", gate)
                    argv = pod["containers"][0]["command"]
                    self.assertNotIn(argv[0], {"sh", "bash", "/bin/sh", "/bin/bash"})
                    self.assertFalse(any("$(" in token or ";" in token for token in argv))
                    self.assertEqual(node["job"]["metadata"]["annotations"]["fs2.nebius.ai/asset-access-receipt-digest"], admission["access_receipt_digest"])

    def test_missing_operator_access_receipt_fails_before_job_render(self) -> None:
        envelope = fixture("negative-missing-access-receipt.json")
        jsonschema.Draft202012Validator(self.request_schema).validate(envelope["request"])
        jsonschema.Draft202012Validator(self.parameter_schema).validate(envelope["request"]["parameters"])
        adapter.validate_request(envelope["request"], envelope["input_manifest"], artifact_loader={"artifact.bindcraft.target.1ubq": PDB}.__getitem__)
        with self.assertRaises(adapter.CatalogError):
            adapter.render_plan(envelope["request"], envelope["input_manifest"], artifact_loader={"artifact.bindcraft.target.1ubq": PDB}.__getitem__, **envelope["admission"])

        open_request = json.loads(json.dumps(envelope["request"]))
        open_request["parameters"]["schema"] = "fs2-serve.nebius.ai/bindcraft-open-freebindcraft-parameters/v1"
        with self.assertRaises(adapter.CatalogError):
            adapter.validate_request(open_request, envelope["input_manifest"], artifact_loader={"artifact.bindcraft.target.1ubq": PDB}.__getitem__)

    def test_two_semantic_outputs_require_receipt_continuity_and_pyrosetta(self) -> None:
        for name in ("positive-pdl1.json", "positive-hotspot-panel.json"):
            envelope = fixture(name)
            output, store = output_for(envelope)
            jsonschema.Draft202012Validator(self.manifest_schema).validate(output)
            receipt = adapter.validate_output_manifest(
                envelope["request"], envelope["input_manifest"], output,
                artifact_loader=store.__getitem__,
                expected_runtime_image_digest=envelope["admission"]["runtime_image"].rsplit("@", 1)[1],
                access_receipt_digest=envelope["admission"]["access_receipt_digest"],
            )
            self.assertEqual(receipt["status"], "passed")
            self.assertEqual(receipt["access_receipt_digest"], envelope["admission"]["access_receipt_digest"])
        envelope = fixture("positive-pdl1.json")
        output, store = output_for(envelope, scoring_engine="openmm-freesasa")
        with self.assertRaises(adapter.CatalogError):
            adapter.validate_output_manifest(
                envelope["request"], envelope["input_manifest"], output,
                artifact_loader=store.__getitem__,
                expected_runtime_image_digest=envelope["admission"]["runtime_image"].rsplit("@", 1)[1],
                access_receipt_digest=envelope["admission"]["access_receipt_digest"],
            )

    def test_metadata_is_academic_unqualified_and_has_no_redistributed_payload(self) -> None:
        metadata = json.loads((MODEL_DIR / "adapter.json").read_text())
        lock = json.loads((MODEL_DIR / "artifact-lock.json").read_text())
        self.assertEqual(metadata["access"]["profile"], "academic")
        self.assertTrue(metadata["access"]["receipt_required"])
        self.assertEqual(metadata["qualification"]["h100"], "not-run")
        self.assertEqual(lock["source_revision"], adapter.SOURCE_REVISION)
        self.assertFalse(metadata["access"]["credentials_in_request"])
        self.assertFalse(metadata["access"]["package_data_in_repository"])
        self.assertNotIn("credential_value", json.dumps(metadata).lower())


if __name__ == "__main__":
    unittest.main()
