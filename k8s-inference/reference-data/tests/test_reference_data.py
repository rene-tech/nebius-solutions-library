from __future__ import annotations

import argparse
import copy
import hashlib
import io
import json
import os
from pathlib import Path
import sys
import tarfile
import tempfile
import unittest
import zipfile

from jsonschema import Draft202012Validator, FormatChecker


REFERENCE_DATA = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REFERENCE_DATA))

import reference_data  # noqa: E402
import render_job  # noqa: E402


class ReferenceDataContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.work = Path(self.temporary.name)

    def _catalog(self, source: Path, *, policy: str = "automatic-public") -> dict[str, object]:
        payload = source.read_bytes()
        digest = hashlib.sha256(payload).hexdigest()
        return {
            "schema": reference_data.CATALOG_SCHEMA,
            "generated_at": "2026-09-02T00:00:00Z",
            "bundles": {
                "fixture": {
                    "id": "fixture",
                    "revision": "fixture-2026-09-02",
                    "description": "Small offline staging fixture.",
                    "upstream": {
                        "project": "fixture/reference-data",
                        "revision": "0" * 40,
                        "source_url": "https://example.invalid/source",
                        "source_sha256": "1" * 64,
                    },
                    "access": {
                        "state": "public",
                        "redistribution": "review-required",
                        "staging_policy": policy,
                        "terms": [
                            {
                                "component": "fixture",
                                "license": "test-only",
                                "url": "https://example.invalid/terms",
                                "verification": "upstream-terms-review-required",
                            }
                        ],
                    },
                    "sizing": {
                        "compressed_bytes": len(payload),
                        "expanded_bytes": len(payload),
                        "expanded_bytes_kind": "exact",
                    },
                    "update_policy": {
                        "cadence": "immutable test fixture",
                        "mutable_aliases_allowed": False,
                        "promotion": "new-revision-after-offline-validation",
                    },
                    "objects": [
                        {
                            "id": "fixture-object",
                            "source": {"url": source.resolve().as_uri()},
                            "target": "database/fixture.txt",
                            "transform": "none",
                            "source_bytes": len(payload),
                            "source_integrity": {
                                "algorithm": "sha256",
                                "digest": digest,
                                "cryptographic": True,
                            },
                            "license_component": "fixture",
                        }
                    ],
                }
            },
        }

    def _write_catalog(self, catalog: dict[str, object]) -> Path:
        path = self.work / "catalog.json"
        path.write_text(json.dumps(catalog), encoding="utf-8")
        return path

    def test_checked_in_contracts_match_json_schemas(self) -> None:
        for schema_path in REFERENCE_DATA.glob("*.schema.json"):
            Draft202012Validator.check_schema(reference_data.load_json(schema_path))
        pairs = [
            ("source-catalog.schema.json", "source-catalog.json"),
            ("preprocess-request.schema.json", "examples/private-msa-request.json"),
        ]
        for schema_name, instance_name in pairs:
            schema = reference_data.load_json(REFERENCE_DATA / schema_name)
            Draft202012Validator.check_schema(schema)
            validator = Draft202012Validator(schema, format_checker=FormatChecker())
            validator.validate(reference_data.load_json(REFERENCE_DATA / instance_name))

        catalog = reference_data.validate_catalog(
            reference_data.load_json(REFERENCE_DATA / "source-catalog.json")
        )
        self.assertEqual(4, len(catalog["bundles"]))
        self.assertEqual(
            615_100_344_355,
            sum(bundle["sizing"]["compressed_bytes"] for bundle in catalog["bundles"].values()),
        )
        requirements = reference_data.load_json(REFERENCE_DATA / "model-requirements.json")
        expected_models = {
            "alphafold3", "openfold3-nim", "protenix-v2", "esmfold2-full",
            "esmfold2-fast", "mosaic", "boltzgen", "proteina-complexa",
            "bindcraft", "rfdiffusion",
        }
        self.assertEqual(expected_models, set(requirements["models"]))
        for model in requirements["models"].values():
            for bundle_id in model.get("required_bundles", []) + model.get("optional_bundles", []):
                self.assertIn(bundle_id, catalog["bundles"])

    def test_stage_is_atomic_verifiable_and_idempotent(self) -> None:
        source = self.work / "source.txt"
        source.write_text("PRIVATE-MSA-FIXTURE\n", encoding="utf-8")
        catalog_path = self._write_catalog(self._catalog(source))
        root = self.work / "store"

        manifest, first_digest = reference_data.stage_bundle(catalog_path, "fixture", root)
        first_created_at = manifest["created_at"]
        source.unlink()
        second_manifest, second_digest = reference_data.stage_bundle(catalog_path, "fixture", root)

        self.assertEqual(first_digest, second_digest)
        self.assertEqual(first_created_at, second_manifest["created_at"])
        manifest_path = root / "manifests" / "sha256" / f"{first_digest}.json"
        verified, verified_digest = reference_data.verify_manifest(manifest_path)
        self.assertEqual(first_digest, verified_digest)
        self.assertEqual("PRIVATE-MSA-FIXTURE\n", Path(
            verified["storage"]["shared_filesystem_uri"].removeprefix("file://")
        ).joinpath("database/fixture.txt").read_text(encoding="utf-8"))
        self.assertTrue((root / "status" / "fixture.json").is_file())
        metrics = (root / "telemetry" / "fixture.prom").read_text(encoding="utf-8")
        self.assertIn("fs2_reference_data_stage_duration_seconds", metrics)
        Draft202012Validator(
            reference_data.load_json(REFERENCE_DATA / "published-manifest.schema.json"),
            format_checker=FormatChecker(),
        ).validate(verified)

    def test_checksum_failure_never_publishes_a_dataset(self) -> None:
        source = self.work / "source.txt"
        source.write_text("wrong checksum\n", encoding="utf-8")
        catalog = self._catalog(source)
        catalog["bundles"]["fixture"]["objects"][0]["source_integrity"]["digest"] = "f" * 64
        catalog_path = self._write_catalog(catalog)
        root = self.work / "store"

        with self.assertRaisesRegex(reference_data.ContractError, "checksum"):
            reference_data.stage_bundle(catalog_path, "fixture", root)
        self.assertFalse((root / "datasets").exists())

    def test_malformed_contracts_fail_closed_with_contract_errors(self) -> None:
        request = reference_data.load_json(REFERENCE_DATA / "examples" / "private-msa-request.json")
        mutations = []
        malformed_input = copy.deepcopy(request)
        malformed_input["input"] = []
        mutations.append(malformed_input)
        mutable_image = copy.deepcopy(request)
        mutable_image["execution"]["image"] = "registry.example.invalid/private-msa:latest"
        mutations.append(mutable_image)
        wrong_format = copy.deepcopy(request)
        wrong_format["backend"]["output_format"] = "protenix-json"
        mutations.append(wrong_format)
        mutable_database = copy.deepcopy(request)
        mutable_database["backend"]["database_root"] = "/reference-data/current"
        mutations.append(mutable_database)
        for document in mutations:
            with self.assertRaises(reference_data.ContractError):
                reference_data.validate_preprocess_request(document)

        source = self.work / "source.txt"
        source.write_text("classification fixture\n", encoding="utf-8")
        catalog = self._catalog(source)
        catalog["bundles"]["fixture"]["objects"][0]["source_integrity"]["cryptographic"] = False
        with self.assertRaisesRegex(reference_data.ContractError, "classification"):
            reference_data.validate_catalog(catalog)

    def test_terms_receipt_is_required_and_bound_to_the_revision(self) -> None:
        source = self.work / "source.txt"
        source.write_text("terms fixture\n", encoding="utf-8")
        catalog = self._catalog(source, policy="terms-receipt-required")
        catalog_path = self._write_catalog(catalog)
        with self.assertRaisesRegex(reference_data.ContractError, "receipt"):
            reference_data.stage_bundle(catalog_path, "fixture", self.work / "store")

        bundle = catalog["bundles"]["fixture"]
        receipt = {
            "schema": reference_data.ACCESS_SCHEMA,
            "bundle_id": "fixture",
            "revision": bundle["revision"],
            "terms_sha256": reference_data.sha256_bytes(reference_data.canonical_json(bundle["access"]["terms"])),
            "approved_at": "2026-09-02T00:00:00Z",
            "approved_by": "offline-test",
            "scope": "academic-research",
        }
        receipt_path = self.work / "receipt.json"
        receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
        manifest, _digest = reference_data.stage_bundle(
            catalog_path,
            "fixture",
            self.work / "store",
            access_receipt_path=receipt_path,
        )
        self.assertEqual(
            reference_data.sha256_bytes(reference_data.canonical_json(receipt)),
            manifest["access_receipt_sha256"],
        )

    def test_tar_traversal_and_duplicate_paths_are_rejected(self) -> None:
        for members in (["../escape"], ["duplicate", "duplicate"]):
            archive = self.work / f"archive-{len(list(self.work.glob('archive-*')))}.tar.gz"
            with tarfile.open(archive, "w:gz") as handle:
                for name in members:
                    info = tarfile.TarInfo(name)
                    info.size = 1
                    handle.addfile(info, io.BytesIO(b"x"))
            item = {"id": "archive", "target": ".", "transform": "tar.gz"}
            with self.assertRaises(reference_data.ContractError):
                reference_data.materialize(archive, item, self.work / "expanded")

        zip_archive = self.work / "archive.zip"
        with zipfile.ZipFile(zip_archive, "w") as handle:
            handle.writestr("../zip-escape", "x")
        with self.assertRaises(reference_data.ContractError):
            reference_data.materialize(
                zip_archive,
                {"id": "zip-archive", "target": ".", "transform": "zip"},
                self.work / "zip-expanded",
            )

    def test_private_preprocess_is_default_and_cpu_only(self) -> None:
        request_path = REFERENCE_DATA / "examples" / "private-msa-request.json"
        request = reference_data.load_json(request_path)
        args = argparse.Namespace(
            request=request_path,
            allow_public_msa=False,
            namespace="fs2-data",
            queue="reference-data",
            tools_config_map="fs2-reference-data-tools-123456789abc",
            shared_host_path="/mnt/fs2cache/csi-mounted-fs-path-data/reference-data",
            credentials_secret=None,
            object_storage_endpoint=None,
        )
        resources = render_job.render_preprocess(args)
        config_map, job = resources["items"]
        pod = job["spec"]["template"]["spec"]
        container = pod["containers"][0]

        self.assertTrue(job["spec"]["suspend"])
        self.assertNotIn("nvidia.com/gpu", json.dumps(container["resources"]))
        self.assertEqual("regular", pod["nodeSelector"]["capacity.fs2.nebius/type"])
        self.assertTrue(next(
            mount for mount in container["volumeMounts"] if mount["name"] == "reference-data"
        )["readOnly"])
        self.assertEqual("private-only", job["metadata"]["labels"]["reference-data.fs2.nebius.ai/network-mode"])
        self.assertNotIn(">", config_map["data"]["request.json"])

        public = copy.deepcopy(request)
        public["privacy"] = {
            "network_mode": "public-opt-in",
            "public_msa_opt_in": True,
            "log_sequence_content": False,
        }
        with self.assertRaisesRegex(reference_data.ContractError, "explicit"):
            reference_data.validate_preprocess_request(public)
        reference_data.validate_preprocess_request(public, allow_public_msa=True)

    def test_completed_preprocessing_is_a_verified_cache_hit_with_telemetry(self) -> None:
        request = reference_data.load_json(REFERENCE_DATA / "examples" / "private-msa-request.json")
        output_prefix = self.work / "outputs"
        request["output"]["prefix_uri"] = output_prefix.resolve().as_uri()
        request_sha256 = reference_data.sha256_bytes(reference_data.canonical_json(request))
        destination = output_prefix / "sha256" / request_sha256
        destination.mkdir(parents=True)
        (destination / "result.a3m").write_text(">query\nACDE\n", encoding="utf-8")
        files, tree_sha256, expanded_bytes = reference_data.tree_inventory(destination)
        result_manifest = {
            "schema": reference_data.RESULT_SCHEMA,
            "request_sha256": request_sha256,
            "input_sha256": request["input"]["sha256"],
            "reference_manifest_sha256": request["reference_data"]["manifest_sha256"],
            "backend": request["backend"]["kind"],
            "privacy_mode": request["privacy"]["network_mode"],
            "created_at": "2026-09-02T00:00:00Z",
            "duration_seconds": 1.25,
            "content": {
                "tree_sha256": tree_sha256,
                "expanded_bytes": expanded_bytes,
                "file_count": len(files),
                "files": files,
            },
        }
        reference_data.validate_preprocess_result(result_manifest)
        result_path = destination / "result-manifest.json"
        result_path.write_text(json.dumps(result_manifest), encoding="utf-8")
        result_digest = reference_data.sha256_bytes(reference_data.canonical_json(result_manifest))
        (destination / ".fs2-ready").write_text(f"{result_digest}\n", encoding="utf-8")
        request_path = self.work / "request.json"
        request_path.write_text(json.dumps(request), encoding="utf-8")
        status_root = self.work / "status-root"

        result = reference_data.run_preprocess(
            request_path,
            telemetry_root=status_root / "telemetry",
        )

        self.assertTrue(result["cache_hit"])
        self.assertEqual(result_digest, result["result_manifest_sha256"])
        observation_path = status_root / "telemetry" / "preprocessing" / "status" / f"{request_sha256}.json"
        observation = reference_data.load_json(observation_path)
        Draft202012Validator(
            reference_data.load_json(REFERENCE_DATA / "preprocess-observation.schema.json"),
            format_checker=FormatChecker(),
        ).validate(observation)
        Draft202012Validator(
            reference_data.load_json(REFERENCE_DATA / "preprocess-result.schema.json"),
            format_checker=FormatChecker(),
        ).validate(result_manifest)
        metrics = reference_data._preprocess_metrics(status_root).decode()
        self.assertIn('outcome="success"', metrics)
        self.assertIn("fs2_reference_preprocess_cache_hits", metrics)

    def test_preprocessing_failure_writes_content_free_error_observation(self) -> None:
        request = reference_data.load_json(REFERENCE_DATA / "examples" / "private-msa-request.json")
        missing = self.work / f"{'a' * 64}.fasta"
        request["input"]["uri"] = missing.resolve().as_uri()
        request["output"]["prefix_uri"] = (self.work / "outputs").resolve().as_uri()
        request_path = self.work / "failing-request.json"
        request_path.write_text(json.dumps(request), encoding="utf-8")
        request_sha256 = reference_data.sha256_bytes(reference_data.canonical_json(request))
        status_root = self.work / "status-root"

        with self.assertRaises(reference_data.ContractError):
            reference_data.run_preprocess(
                request_path,
                telemetry_root=status_root / "telemetry",
            )

        observation = reference_data.load_json(
            status_root / "telemetry" / "preprocessing" / "status" / f"{request_sha256}.json"
        )
        self.assertEqual("error", observation["outcome"])
        self.assertEqual("ContractError", observation["error_code"])
        self.assertNotIn("sequence", json.dumps(observation).lower())

    def test_job_renderer_never_embeds_credentials_or_gated_urls(self) -> None:
        source = self.work / "source.txt"
        source.write_text("gated fixture\n", encoding="utf-8")
        catalog = self._catalog(source, policy="entitlement-receipt-required")
        item = catalog["bundles"]["fixture"]["objects"][0]
        item["source"] = {"url_env": "FS2_FIXTURE_URL"}
        catalog_path = self._write_catalog(catalog)
        bundle = catalog["bundles"]["fixture"]
        receipt = {
            "schema": reference_data.ACCESS_SCHEMA,
            "bundle_id": "fixture",
            "revision": bundle["revision"],
            "terms_sha256": reference_data.sha256_bytes(reference_data.canonical_json(bundle["access"]["terms"])),
            "approved_at": "2026-09-02T00:00:00Z",
            "approved_by": "offline-test",
            "scope": "internal-evaluation",
        }
        receipt_path = self.work / "receipt.json"
        receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
        args = argparse.Namespace(
            catalog=catalog_path,
            bundle="fixture",
            image=f"registry.example.invalid/stager@sha256:{'a' * 64}",
            access_receipt=receipt_path,
            object_store_prefix=None,
            namespace="fs2-data",
            queue="reference-data",
            tools_config_map="fs2-reference-data-tools-123456789abc",
            shared_host_path="/mnt/fs2cache/csi-mounted-fs-path-data/reference-data",
            credentials_secret="reference-data-writer",
            object_storage_endpoint="https://storage.eu-north1.nebius.cloud",
            cpu="8",
            memory="32Gi",
            ephemeral_storage="32Gi",
            active_deadline_seconds=3600,
            backoff_limit=2,
        )
        resources = render_job.render_stage(args)
        rendered = json.dumps(resources)

        self.assertNotIn("gated fixture", rendered)
        self.assertNotIn(source.as_uri(), rendered)
        self.assertNotIn("secret-access-key\"", resources["items"][0]["data"]["catalog.json"])
        env = resources["items"][1]["spec"]["template"]["spec"]["containers"][0]["env"]
        credential_names = {"AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"}
        self.assertTrue(all("valueFrom" in item for item in env if item["name"] in credential_names))


if __name__ == "__main__":
    unittest.main()
