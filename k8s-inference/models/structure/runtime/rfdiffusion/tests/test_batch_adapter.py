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

SPEC = importlib.util.spec_from_file_location("rfdiffusion_batch_adapter", MODEL_DIR / "batch_adapter.py")
assert SPEC is not None and SPEC.loader is not None
adapter = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(adapter)

PDB = (REPO_DIR / "catalog/runtime/packaged-repository/nim-fast-start/faststart-v2/rfdiffusion-native/fixtures/1UBQ.pdb").read_bytes()
CONTEXT = (MODEL_DIR / "fixtures/unconditional-context.json").read_bytes()
SCHEMA_DIR = REPO_DIR / "catalog/runtime/schema"


def fixture(name: str) -> dict[str, Any]:
    return json.loads((MODEL_DIR / "fixtures" / name).read_text())


def schema(name: str) -> dict[str, Any]:
    return json.loads((SCHEMA_DIR / name).read_text())


def input_store(envelope: dict[str, Any]) -> dict[str, bytes]:
    pointer = envelope["input_manifest"]["entries"][0]["artifact"]
    return {pointer["artifact_id"]: PDB if pointer["media_type"] == "chemical/x-pdb" else CONTEXT}


def add_artifact(store: dict[str, bytes], artifact_id: str, value: bytes, media_type: str) -> dict[str, Any]:
    store[artifact_id] = value
    return {
        "artifact_id": artifact_id,
        "sha256": hashlib.sha256(value).hexdigest(),
        "size_bytes": len(value),
        "media_type": media_type,
        "compression": "none",
    }


def output_for(envelope: dict[str, Any], *, motif_rmsd_a: float | None | object = ...) -> tuple[dict[str, Any], dict[str, bytes]]:
    request, admission = envelope["request"], envelope["admission"]
    count = request["parameters"]["shard_count"]
    runtime_digest = admission["runtime_image"].rsplit("@", 1)[1]
    store = input_store(envelope)
    entries: list[dict[str, Any]] = []
    for index in range(count):
        shard = {
            "backend_id": adapter.ADAPTER_ID,
            "source_revision": adapter.SOURCE_REVISION,
            "checkpoint_sha256": adapter.CHECKPOINT_SHA256,
            "index": index,
            "seed": request["parameters"]["base_seed"] + index,
            "status": "succeeded",
        }
        entries.append({
            "name": f"shard-{index:03d}",
            "semantic_type": "rfdiffusion-shard-result-json/v1",
            "artifact": add_artifact(store, f"artifact.rfdiffusion.shard.{index:03d}", canonical_bytes(shard), "application/json"),
        })
    aggregate = {
        "backend_id": adapter.ADAPTER_ID,
        "source_revision": adapter.SOURCE_REVISION,
        "checkpoint_sha256": adapter.CHECKPOINT_SHA256,
        "request_sha256": adapter.request_digest(request),
        "runtime_image_digest": runtime_digest,
        "expected_shards": count,
        "succeeded_shards": count,
        "atomic_commit": True,
    }
    entries.append({
        "name": "aggregate",
        "semantic_type": "rfdiffusion-aggregate-json/v1",
        "artifact": add_artifact(store, "artifact.rfdiffusion.aggregate", canonical_bytes(aggregate), "application/json"),
    })
    measured = 0.0 if any(item["kind"] == "motif" for item in request["parameters"]["contigs"]) else None
    if motif_rmsd_a is not ...:
        measured = motif_rmsd_a  # type: ignore[assignment]
    _, residue_count, span = adapter._pdb(PDB)
    metrics = {
        "candidate_id": "backbone-000",
        "shard_index": 0,
        "seed": request["parameters"]["base_seed"],
        "backbone_complete": True,
        "ca_count": residue_count,
        "coordinate_span_a": round(span, 6),
        "motif_rmsd_a": measured,
    }
    entries.extend([
        {
            "name": "candidate-000-metrics",
            "semantic_type": "rfdiffusion-backbone-metrics-json/v1",
            "artifact": add_artifact(store, "artifact.rfdiffusion.candidate.000.metrics", canonical_bytes(metrics), "application/json"),
        },
        {
            "name": "candidate-000-structure",
            "semantic_type": "protein-structure-pdb/v1",
            "artifact": add_artifact(store, "artifact.rfdiffusion.candidate.000.pdb", PDB, "chemical/x-pdb"),
        },
    ])
    return {
        "schema": adapter.ARTIFACT_MANIFEST_SCHEMA,
        "manifest_id": "manifest.rfdiffusion.output.01",
        "entries": entries,
    }, store


class RFdiffusionBatchAdapterTest(unittest.TestCase):
    def setUp(self) -> None:
        self.request_schema = schema("scientific-run-request.schema.json")
        self.manifest_schema = schema("scientific-artifact-manifest.schema.json")
        self.parameter_schema = json.loads((MODEL_DIR / "parameters.schema.json").read_text())

    def test_two_positive_fixtures_render_typed_shell_free_seed_shards(self) -> None:
        jsonschema.Draft202012Validator(schema("scientific-workload.schema.json")).validate(json.loads((MODEL_DIR / "workload.json").read_text()))
        for name in ("positive-unconditional.json", "positive-motif.json"):
            with self.subTest(name=name):
                envelope = fixture(name)
                request, manifest = envelope["request"], envelope["input_manifest"]
                jsonschema.Draft202012Validator(self.request_schema).validate(request)
                jsonschema.Draft202012Validator(self.parameter_schema).validate(request["parameters"])
                jsonschema.Draft202012Validator(self.manifest_schema).validate(manifest)
                plan = adapter.render_plan(request, manifest, artifact_loader=input_store(envelope).__getitem__, **envelope["admission"])
                count = request["parameters"]["shard_count"]
                self.assertEqual(plan["backend_id"], "rfdiffusion-upstream-v1-1-0-base")
                self.assertEqual(len(plan["nodes"]), count + 1)
                self.assertEqual([node["seed"] for node in plan["nodes"][:count]], list(range(request["parameters"]["base_seed"], request["parameters"]["base_seed"] + count)))
                self.assertEqual(plan["nodes"][-1]["depends_on"], [f"diffuse-{index:03d}" for index in range(count)])
                for node in plan["nodes"]:
                    job = node["job"]
                    self.assertTrue(job["spec"]["suspend"])
                    self.assertEqual(job["spec"]["backoffLimit"], 0)
                    argv = job["spec"]["template"]["spec"]["containers"][0]["command"]
                    self.assertNotIn(argv[0], {"sh", "bash", "/bin/sh", "/bin/bash"})
                    self.assertFalse(any("$(" in token or ";" in token for token in argv))
                    self.assertFalse(any("hydra" in token.lower() or token.startswith("inference.") or token.startswith("contigmap.") for token in argv))

    def test_negative_fixture_rejects_raw_hydra_overrides(self) -> None:
        envelope = fixture("negative-raw-hydra.json")
        jsonschema.Draft202012Validator(self.request_schema).validate(envelope["request"])
        with self.assertRaises(jsonschema.ValidationError):
            jsonschema.Draft202012Validator(self.parameter_schema).validate(envelope["request"]["parameters"])
        with self.assertRaises(adapter.CatalogError):
            adapter.validate_request(envelope["request"], envelope["input_manifest"], artifact_loader=input_store(envelope).__getitem__)

    def test_two_semantic_outputs_and_motif_rmsd_recomputation(self) -> None:
        for name in ("positive-unconditional.json", "positive-motif.json"):
            envelope = fixture(name)
            output, store = output_for(envelope)
            jsonschema.Draft202012Validator(self.manifest_schema).validate(output)
            receipt = adapter.validate_output_manifest(
                envelope["request"], envelope["input_manifest"], output,
                artifact_loader=store.__getitem__,
                expected_runtime_image_digest=envelope["admission"]["runtime_image"].rsplit("@", 1)[1],
            )
            self.assertEqual(receipt["status"], "passed")
        envelope = fixture("positive-motif.json")
        output, store = output_for(envelope, motif_rmsd_a=0.2)
        with self.assertRaises(adapter.CatalogError):
            adapter.validate_output_manifest(
                envelope["request"], envelope["input_manifest"], output,
                artifact_loader=store.__getitem__,
                expected_runtime_image_digest=envelope["admission"]["runtime_image"].rsplit("@", 1)[1],
            )

    def test_stable_source_and_checkpoint_are_exact_but_unqualified(self) -> None:
        metadata = json.loads((MODEL_DIR / "batch-adapter.json").read_text())
        lock = json.loads((MODEL_DIR / "artifact-lock.json").read_text())
        self.assertEqual(metadata["source"]["revision"], adapter.SOURCE_REVISION)
        self.assertEqual(lock["source_revision"], adapter.SOURCE_REVISION)
        self.assertEqual(next(item for item in lock["artifacts"] if item["id"] == "base-checkpoint")["sha256"], adapter.CHECKPOINT_SHA256)
        self.assertEqual(metadata["qualification"]["h100"], "pending-published-digest")


if __name__ == "__main__":
    unittest.main()
