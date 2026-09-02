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
sys.path.insert(0, str(REPO_DIR / "catalog" / "runtime"))
from fs2_serve_catalog.artifacts import canonical_bytes  # noqa: E402

SPEC = importlib.util.spec_from_file_location("mosaic_batch_adapter", MODEL_DIR / "batch_adapter.py")
assert SPEC is not None and SPEC.loader is not None
adapter = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(adapter)

PDB = (REPO_DIR / "catalog/runtime/packaged-repository/nim-fast-start/faststart-v2/rfdiffusion-native/fixtures/1UBQ.pdb").read_bytes()
PDB_SEQUENCE = "MQIFVKTLTGKTITLEVEPSDTIENVKAKIQDKEGIPPDQQRLIFAGKQLEDGRTLSDYNIQKESTLHLVLRLRGG"
SHARED_SCHEMA_DIR = REPO_DIR / "catalog/runtime/schema"


def fixture(name: str) -> dict[str, Any]:
    return json.loads((MODEL_DIR / "fixtures" / name).read_text(encoding="utf-8"))


def schema(name: str) -> dict[str, Any]:
    return json.loads((SHARED_SCHEMA_DIR / name).read_text(encoding="utf-8"))


def add_artifact(store: dict[str, bytes], artifact_id: str, payload: bytes, media_type: str) -> dict[str, Any]:
    store[artifact_id] = payload
    return {
        "artifact_id": artifact_id,
        "sha256": hashlib.sha256(payload).hexdigest(),
        "size_bytes": len(payload),
        "media_type": media_type,
        "compression": "none",
    }


def result_for(envelope: dict[str, Any], *, atomic_commit: bool = True) -> tuple[dict[str, Any], dict[str, bytes]]:
    request = envelope["request"]
    digest = envelope["admission"]["runtime_image"].rsplit("@", 1)[1]
    count = request["parameters"]["shard_count"]
    store = {
        entry["artifact"]["artifact_id"]: (MODEL_DIR / "fixtures" / (
            "target-minibinder.fasta" if "minibinder" in entry["artifact"]["artifact_id"] else "target-antibody.fasta"
        )).read_bytes()
        for entry in envelope["input_manifest"]["entries"]
    }
    entries: list[dict[str, Any]] = []
    for index in range(count):
        shard = {
            "backend_id": adapter.ADAPTER_ID,
            "source_revision": adapter.SOURCE_REVISION,
            "recipe_sha256": adapter.RECIPE_SHA256,
            "index": index,
            "seed": request["parameters"]["base_seed"] + index,
            "status": "succeeded",
        }
        entries.append({
            "name": f"shard-{index:03d}",
            "semantic_type": "mosaic-shard-result-json/v1",
            "artifact": add_artifact(store, f"artifact.mosaic.shard.{index:03d}", canonical_bytes(shard), "application/json"),
        })
    aggregate = {
        "backend_id": adapter.ADAPTER_ID,
        "source_revision": adapter.SOURCE_REVISION,
        "recipe_sha256": adapter.RECIPE_SHA256,
        "request_sha256": adapter.request_digest(request),
        "runtime_image_digest": digest,
        "expected_shards": count,
        "succeeded_shards": count,
        "atomic_commit": atomic_commit,
    }
    entries.append({
        "name": "aggregate",
        "semantic_type": "mosaic-aggregate-json/v1",
        "artifact": add_artifact(store, "artifact.mosaic.aggregate", canonical_bytes(aggregate), "application/json"),
    })
    metrics = {
        "candidate_id": "design-000",
        "shard_index": 0,
        "seed": request["parameters"]["base_seed"],
        "sequence": PDB_SEQUENCE,
        "iptm": 0.81,
        "mean_plddt": 0.88,
        "objective": -2.4,
    }
    entries.extend([
        {
            "name": "candidate-000-metrics",
            "semantic_type": "mosaic-design-metrics-json/v1",
            "artifact": add_artifact(store, "artifact.mosaic.candidate.000.metrics", canonical_bytes(metrics), "application/json"),
        },
        {
            "name": "candidate-000-structure",
            "semantic_type": "protein-structure-pdb/v1",
            "artifact": add_artifact(store, "artifact.mosaic.candidate.000.pdb", PDB, "chemical/x-pdb"),
        },
    ])
    return {
        "schema": adapter.ARTIFACT_MANIFEST_SCHEMA,
        "manifest_id": "manifest.mosaic.output.01",
        "entries": entries,
    }, store


class MosaicBatchAdapterTest(unittest.TestCase):
    def setUp(self) -> None:
        self.request_schema = schema("scientific-run-request.schema.json")
        self.manifest_schema = schema("scientific-artifact-manifest.schema.json")
        self.workload_schema = schema("scientific-workload.schema.json")
        self.parameter_schema = json.loads((MODEL_DIR / "parameters.schema.json").read_text())

    def test_two_positive_fixtures_render_shared_shell_free_dag(self) -> None:
        jsonschema.Draft202012Validator(self.workload_schema).validate(json.loads((MODEL_DIR / "workload.json").read_text()))
        for name in ("positive-minibinder.json", "positive-multihotspot.json"):
            with self.subTest(name=name):
                envelope = fixture(name)
                request, manifest, admission = envelope["request"], envelope["input_manifest"], envelope["admission"]
                jsonschema.Draft202012Validator(self.request_schema).validate(request)
                jsonschema.Draft202012Validator(self.parameter_schema).validate(request["parameters"])
                jsonschema.Draft202012Validator(self.manifest_schema).validate(manifest)
                target = manifest["entries"][0]["artifact"]["artifact_id"]
                file_name = "target-minibinder.fasta" if "minibinder" in target else "target-antibody.fasta"
                store = {target: (MODEL_DIR / "fixtures" / file_name).read_bytes()}
                plan = adapter.render_plan(request, manifest, artifact_loader=store.__getitem__, **admission)
                count = request["parameters"]["shard_count"]
                self.assertEqual(len(plan["nodes"]), count + 1)
                self.assertEqual([node["seed"] for node in plan["nodes"][:count]], list(range(request["parameters"]["base_seed"], request["parameters"]["base_seed"] + count)))
                self.assertEqual(plan["nodes"][-1]["depends_on"], [f"design-{index:03d}" for index in range(count)])
                for node in plan["nodes"]:
                    job = node["job"]
                    self.assertTrue(job["spec"]["suspend"])
                    self.assertEqual(job["spec"]["backoffLimit"], 0)
                    labels = job["metadata"]["labels"]
                    for key in ("model-id", "workload-id", "attempt-id", "tenant-id", "service-class", "local-queue"):
                        self.assertIn(f"fs2.nebius.ai/{key}", labels)
                    argv = job["spec"]["template"]["spec"]["containers"][0]["command"]
                    self.assertNotIn(argv[0], {"sh", "bash", "/bin/sh", "/bin/bash"})
                    self.assertFalse(any("$(" in token or ";" in token for token in argv))
                self.assertIn("--atomic-rename", plan["nodes"][-1]["job"]["spec"]["template"]["spec"]["containers"][0]["command"])

    def test_negative_fixture_rejects_raw_objective(self) -> None:
        envelope = fixture("negative-raw-objective.json")
        jsonschema.Draft202012Validator(self.request_schema).validate(envelope["request"])
        with self.assertRaises(jsonschema.ValidationError):
            jsonschema.Draft202012Validator(self.parameter_schema).validate(envelope["request"]["parameters"])
        store = {envelope["input_manifest"]["entries"][0]["artifact"]["artifact_id"]: (MODEL_DIR / "fixtures/target-minibinder.fasta").read_bytes()}
        with self.assertRaises(adapter.CatalogError):
            adapter.validate_request(envelope["request"], envelope["input_manifest"], artifact_loader=store.__getitem__)

    def test_two_semantic_outputs_and_atomic_tamper_rejection(self) -> None:
        for name in ("positive-minibinder.json", "positive-multihotspot.json"):
            envelope = fixture(name)
            output, store = result_for(envelope)
            jsonschema.Draft202012Validator(self.manifest_schema).validate(output)
            receipt = adapter.validate_output_manifest(
                envelope["request"], envelope["input_manifest"], output,
                artifact_loader=store.__getitem__,
                expected_runtime_image_digest=envelope["admission"]["runtime_image"].rsplit("@", 1)[1],
            )
            self.assertEqual(receipt["status"], "passed")
            self.assertEqual(receipt["qualification_effect"], "none-offline-validation-only")
        envelope = fixture("positive-minibinder.json")
        output, store = result_for(envelope, atomic_commit=False)
        with self.assertRaises(adapter.CatalogError):
            adapter.validate_output_manifest(
                envelope["request"], envelope["input_manifest"], output,
                artifact_loader=store.__getitem__,
                expected_runtime_image_digest=envelope["admission"]["runtime_image"].rsplit("@", 1)[1],
            )

    def test_exact_recipe_artifacts_and_immutable_image_are_enforced(self) -> None:
        self.assertEqual(hashlib.sha256((MODEL_DIR / "recipe.json").read_bytes()).hexdigest(), adapter.RECIPE_SHA256)
        lock = json.loads((MODEL_DIR / "artifact-lock.json").read_text())
        self.assertEqual(lock["source_revision"], adapter.SOURCE_REVISION)
        self.assertEqual(next(item for item in lock["artifacts"] if item["id"] == "boltz2-weights")["artifact_manifest_sha256"], adapter.BOLTZ2_ARTIFACT_MANIFEST_SHA256)
        envelope = fixture("positive-minibinder.json")
        target = envelope["input_manifest"]["entries"][0]["artifact"]["artifact_id"]
        store = {target: (MODEL_DIR / "fixtures/target-minibinder.fasta").read_bytes()}
        admission = dict(envelope["admission"])
        admission["runtime_image"] = "registry.invalid/fs2/mosaic:latest"
        with self.assertRaises(adapter.CatalogError):
            adapter.render_plan(envelope["request"], envelope["input_manifest"], artifact_loader=store.__getitem__, **admission)


if __name__ == "__main__":
    unittest.main()
