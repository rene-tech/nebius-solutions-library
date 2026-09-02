"""Offline tests for the private academic asset ingestion path.

The suite deliberately builds tiny but structurally real artifacts (a genuine
zstd frame and a genuine Python wheel) so the structural validators are
exercised for real rather than stubbed out.
"""

from __future__ import annotations

import copy
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import academic_assets as aa  # noqa: E402

ASSET_ROOT = Path(__file__).resolve().parents[1]
REAL_CONTRACT = ASSET_ROOT / "contracts" / "academic-assets.json"
READINESS_SCHEMA = ASSET_ROOT / "schemas" / "readiness.schema.json"
STAGE_SCHEMA = ASSET_ROOT / "schemas" / "stage-receipt.schema.json"
AUTHORIZATION_SCHEMA = ASSET_ROOT / "schemas" / "use-authorization.schema.json"
ACCEPTANCE_SCHEMA = ASSET_ROOT / "schemas" / "license-acceptance.schema.json"

TENANT = "tenant-academic"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_zstd(path: Path, payload: bytes) -> None:
    executable = shutil.which("zstd")
    if executable is None:  # pragma: no cover - environment guard
        raise unittest.SkipTest("zstd is required for structural validation tests")
    subprocess.run(
        [executable, "-q", "-f", "-o", str(path), "-"],
        input=payload,
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def write_wheel(
    path: Path,
    *,
    distribution: str = "pyrosetta",
    version: str = "2026.29+releasequarterly.80a0635615",
    tag: str = "cp310-cp310-linux_x86_64",
    metadata_version: str | None = None,
    include_record: bool = True,
) -> None:
    dist_info = f"{distribution.replace('-', '_')}-{version}.dist-info"
    declared = metadata_version or version
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(f"{distribution}/__init__.py", "value = 1\n")
        archive.writestr(
            f"{dist_info}/METADATA",
            f"Metadata-Version: 2.1\nName: {distribution}\nVersion: {declared}\n\nbody\n",
        )
        archive.writestr(
            f"{dist_info}/WHEEL",
            f"Wheel-Version: 1.0\nGenerator: test\nRoot-Is-Purelib: false\nTag: {tag}\n",
        )
        if include_record:
            archive.writestr(f"{dist_info}/RECORD", f"{dist_info}/METADATA,,\n")


class AcademicAssetTestCase(unittest.TestCase):
    """Builds an isolated owner-only state directory and a tiny-but-real contract."""

    maxDiff = None

    def setUp(self) -> None:
        self.workspace = Path(tempfile.mkdtemp(prefix="academic-assets-test-"))
        self.addCleanup(shutil.rmtree, self.workspace, ignore_errors=True)
        self.state_dir = self.workspace / "private-state"
        self.sources = self.workspace / "sources"
        self.sources.mkdir(parents=True)

        self.af3_path = self.sources / "af3.bin.zst"
        write_zstd(self.af3_path, b"alphafold3-test-parameters" * 512)
        self.wheel_name = "pyrosetta-2026.29+releasequarterly.80a0635615-cp310-cp310-linux_x86_64.whl"
        self.wheel_path = self.sources / self.wheel_name
        write_wheel(self.wheel_path)

        self.contract_path = self.workspace / "contract.json"
        self.contract_document = self.build_contract()
        self.write_contract(self.contract_document)

    # ---------------------------------------------------------------- fixtures

    def build_contract(self) -> dict[str, Any]:
        real = json.loads(REAL_CONTRACT.read_text())
        contract = copy.deepcopy(real)
        af3 = contract["assets"]["alphafold3"]["artifact"]
        af3["size_bytes"] = self.af3_path.stat().st_size
        af3["sha256"] = sha256_file(self.af3_path)
        wheel = contract["assets"]["pyrosetta-bindcraft"]["artifact"]
        wheel["size_bytes"] = self.wheel_path.stat().st_size
        wheel["sha256"] = sha256_file(self.wheel_path)
        contract["assets"]["alphafold3"]["runtime"]["offline_validation"][
            "expect_min_parameter_arrays"
        ] = 1
        # Default fixture: a runtime image whose packaged identity matches its pinned
        # tag. The committed contract currently declares a mismatch; that hold is
        # exercised explicitly in test_image_identity_mismatch_holds_short_of_ready.
        image = contract["assets"]["alphafold3"]["runtime"]["runtime_image"]
        image["packaged_distribution_version"] = image["expected_distribution_version"]
        image.pop("identity_mismatch", None)
        image["revalidation_required"] = False
        # Default fixture: a final runtime wrapper. The committed contract currently
        # pins an evidence-only stock image; that case is exercised explicitly in
        # test_evidence_only_image_is_not_published_as_a_runtime_image.
        image["role"] = "final-runtime-wrapper"
        image["final_wrapper"] = True
        return contract

    def write_contract(self, document: dict[str, Any]) -> None:
        self.contract_path.write_text(json.dumps(document, indent=2) + "\n")

    def authorization(self, asset_id: str, **overrides: Any) -> Path:
        receipt = {
            "schema": "fs2-serve.nebius.ai/academic-use-authorization/v1",
            "asset_id": asset_id,
            "status": "granted",
            "granted_at": "2026-09-02T21:55:00Z",
            "granted_by_role": "platform-owner",
            "authorization_id": "test-academic-poc",
            "tenant_id": TENANT,
            "scope": "academic-noncommercial",
            "provenance": "explicit platform-owner instruction recorded in the task",
            "supersedes_formal_acceptance": False,
            "permitted_operations": [
                "verify-artifact",
                "stage-tenant-private-volume",
                "install-to-tenant-private-volume",
                "validate-runtime",
                "execute-scientific-prediction",
            ],
            "use_class": "academic-non-commercial",
            "non_exportable": True,
            # Cite exactly the licence digests the contract pins.
            "licence_references": [
                {
                    "document_id": term["document_id"].split("@", 1)[0],
                    "url": "https://example.invalid/licence",
                    "pinned_revision": term["document_id"].split("@", 1)[-1],
                    "version": "test licence",
                    "sha256": term["sha256"],
                    "verified_at": "2026-09-02T21:00:00Z",
                    "basis": "test fixture licence reference",
                }
                for term in self.contract_document["assets"][asset_id]["acceptance"]["terms"]
            ],
            "artifact_reference": {
                "filename": self.contract_document["assets"][asset_id]["artifact"]["filename"],
                "version": self.contract_document["assets"][asset_id]["artifact"]["version"],
                "sha256": self.contract_document["assets"][asset_id]["artifact"]["sha256"],
                "source_url": self.contract_document["assets"][asset_id]["artifact"]["source_url"],
            },
        }
        receipt.update(overrides)
        path = self.workspace / f"{asset_id}-authorization-{len(list(self.workspace.glob('*.json')))}.json"
        path.write_text(json.dumps(receipt))
        return path

    def acceptance(self, asset_id: str, **overrides: Any) -> Path:
        spec = self.contract_document["assets"][asset_id]["acceptance"]
        receipt = {
            "schema": "fs2-serve.nebius.ai/academic-license-acceptance/v3",
            "asset_id": asset_id,
            "status": "accepted-by-authorized-representative",
            "accepted_at": "2026-09-02T22:00:00Z",
            "tenant": {
                "tenant_id": TENANT,
                "institution_id": "test-institution",
                "institution_name": "Test Institution",
            },
            "actor": {
                "actor_id": "rep-1",
                "display_name": "Test Representative",
                "role": "authorized-organization-representative",
            },
            "signature": {"type": "signed-attestation-document", "sha256": "a" * 64},
            "scope": spec["scope"],
            "distribution_scope": spec["distribution_scope"],
            "terms": copy.deepcopy(spec["terms"]),
            "accepted_terms_sha256": "b" * 64,
            "entitlements": [
                {"entitlement_id": entry["entitlement_id"], "issuer": entry["issuer"], "evidence_sha256": "c" * 64}
                for entry in spec["required_entitlements"]
            ],
            "source_claims": copy.deepcopy(spec["source_claims"]),
        }
        receipt.update(overrides)
        path = self.workspace / f"{asset_id}-acceptance-{len(list(self.workspace.glob('*.json')))}.json"
        path.write_text(json.dumps(receipt))
        return path

    def run_cli(self, *argv: str, contract: Path | None = None) -> tuple[int, dict[str, Any]]:
        stream = io.StringIO()
        original = sys.stdout
        sys.stdout = stream
        try:
            code = aa.main(["--contract", str(contract or self.contract_path), *argv])
        finally:
            sys.stdout = original
        return code, json.loads(stream.getvalue())

    def ingest(
        self,
        generation: str,
        *,
        af3_artifact: bool = True,
        wheel_artifact: bool = True,
        af3_authorization: bool = True,
        wheel_authorization: bool = True,
        af3_acceptance: bool = False,
        wheel_acceptance: bool = False,
    ) -> tuple[int, dict[str, Any]]:
        argv = ["ingest", "--state-dir", str(self.state_dir), "--generation", generation]
        if af3_artifact:
            argv += ["--alphafold3-path", str(self.af3_path)]
        if af3_authorization:
            argv += ["--alphafold3-authorization", str(self.authorization("alphafold3"))]
        if af3_acceptance:
            argv += ["--alphafold3-acceptance", str(self.acceptance("alphafold3"))]
        if wheel_artifact:
            argv += ["--pyrosetta-bindcraft-path", str(self.wheel_path)]
        if wheel_authorization:
            argv += ["--pyrosetta-bindcraft-authorization", str(self.authorization("pyrosetta-bindcraft"))]
        if wheel_acceptance:
            argv += ["--pyrosetta-bindcraft-acceptance", str(self.acceptance("pyrosetta-bindcraft"))]
        return self.run_cli(*argv)

    def asset(self, projection: dict[str, Any], asset_id: str) -> dict[str, Any]:
        return next(item for item in projection["assets"] if item["asset_id"] == asset_id)

    def cache_receipt(self, asset_id: str, **overrides: Any) -> Path:
        contract = self.contract_document
        target = contract["runtime_cache"]
        delivery = contract["assets"][asset_id]["delivery"]
        receipt = {
            "schema": "fs2-serve.nebius.ai/academic-cache-receipt/v3",
            "asset_id": asset_id,
            "artifact_sha256": contract["assets"][asset_id]["artifact"]["sha256"],
            "observed_at": "2026-09-02T22:10:00Z",
            "tenant_id": TENANT,
            "institution_id": None,
            # Environment identity is observed per deployment, not contracted.
            "project_id": "project-test",
            "region": "eu-north1",
            "cluster_id": "mk8scluster-test",
            "filesystem_id": None,
            "volume_handle": "pvc-00000000-0000-0000-0000-000000000000",
            "pvc_namespace": target["pvc_namespace"],
            "pvc_name": target["pvc_name"],
            "pvc_uid": "00000000-0000-0000-0000-000000000000",
            "file_size_bytes": contract["assets"][asset_id]["artifact"]["size_bytes"],
            "file_mode": delivery["file_mode"],
            "directory_mode": delivery["directory_mode"],
            "asset_gid": delivery["asset_gid"],
            "verified": True,
            "runtime_mount_allowed": True,
            "general_shared_cache": False,
        }
        receipt.update(overrides)
        return self.write_receipt(asset_id, "cache", receipt)

    def install_receipt(self, asset_id: str, **overrides: Any) -> Path:
        contract = self.contract_document
        spec = contract["assets"][asset_id]
        delivery = spec["delivery"]
        offline = spec["runtime"]["offline_validation"]
        receipt = {
            "schema": "fs2-serve.nebius.ai/academic-install-receipt/v3",
            "asset_id": asset_id,
            "artifact_sha256": spec["artifact"]["sha256"],
            "observed_at": "2026-09-02T22:13:00Z",
            "tenant_id": TENANT,
            "institution_id": None,
            "install_relative_path": delivery["install_relative_path"],
            "installed_distribution": offline.get("expect_distribution"),
            "installed_distribution_version": offline.get("expect_version"),
            "python_version": "3.10.21",
            "file_count": 1234,
            "file_mode": delivery["file_mode"],
            "directory_mode": delivery["directory_mode"],
            "asset_gid": delivery["asset_gid"],
            "world_readable": False,
            "atomic_promotion": True,
            "import_verified": True,
            "evidence_digest": "f" * 64,
        }
        receipt.update(overrides)
        return self.write_receipt(asset_id, "install", receipt)

    def runtime_receipt(self, asset_id: str, **overrides: Any) -> Path:
        contract = self.contract_document
        spec = contract["assets"][asset_id]
        offline = spec["runtime"]["offline_validation"]
        loader = offline["kind"] == "official-parameter-loader"
        installed = (
            offline["expect_version"] if offline["kind"] == "python-import" else spec["artifact"]["version"]
        )
        declared = (spec["runtime"].get("runtime_image") or {}).get("digest")
        receipt = {
            "schema": "fs2-serve.nebius.ai/academic-runtime-receipt/v3",
            "asset_id": asset_id,
            "artifact_sha256": spec["artifact"]["sha256"],
            "observed_at": "2026-09-02T22:15:00Z",
            "tenant_id": TENANT,
            "institution_id": None,
            # Where the contract pins a published runtime image, the validation must
            # have run against exactly that image.
            "image_digest": declared or ("sha256:" + "1" * 64),
            "image_contains_licensed_bytes": False,
            "asset_delivery_mode": "tenant-private-volume",
            "offline_validation_kind": offline["kind"],
            "network_disabled": True,
            "validation_passed": True,
            "python_version": "3.10.21",
            "installed_distribution_version": installed,
            "loaded_parameter_arrays": offline["expect_min_parameter_arrays"] if loader else None,
            "loader_source_revision": offline["source_revision"] if loader else None,
            "inference_performed": loader,
            "predicted_atom_records": 113 if loader else None,
            "predicted_structure_sha256": ("e" * 64) if loader else None,
            "evidence_digest": "d" * 64,
        }
        receipt.update(overrides)
        return self.write_receipt(asset_id, "runtime", receipt)

    def deployment_receipt(self, asset_id: str, **overrides: Any) -> Path:
        spec = self.contract_document["assets"][asset_id]
        receipt = {
            "schema": "fs2-serve.nebius.ai/academic-deployment-receipt/v3",
            "asset_id": asset_id,
            "artifact_sha256": spec["artifact"]["sha256"],
            "observed_at": "2026-09-02T22:20:00Z",
            "tenant_id": TENANT,
            "institution_id": None,
            "model_id": spec["model_id"],
            "image_digest": (spec["runtime"].get("runtime_image") or {}).get("digest")
            or ("sha256:" + "1" * 64),
            "deployed": True,
            "resource_uid": "uid-test",
        }
        receipt.update(overrides)
        return self.write_receipt(asset_id, "deployment", receipt)

    def semantic_receipt(self, asset_id: str, **overrides: Any) -> Path:
        spec = self.contract_document["assets"][asset_id]
        receipt = {
            "schema": "fs2-serve.nebius.ai/academic-semantic-receipt/v3",
            "asset_id": asset_id,
            "artifact_sha256": spec["artifact"]["sha256"],
            "observed_at": "2026-09-02T22:25:00Z",
            "tenant_id": TENANT,
            "institution_id": None,
            "model_id": spec["model_id"],
            "image_digest": (spec["runtime"].get("runtime_image") or {}).get("digest")
            or ("sha256:" + "1" * 64),
            "passed": True,
            "validator_digest": "sha256:" + "2" * 64,
        }
        receipt.update(overrides)
        return self.write_receipt(asset_id, "semantic", receipt)

    def write_receipt(self, asset_id: str, stage: str, receipt: dict[str, Any]) -> Path:
        path = self.workspace / f"{asset_id}-{stage}.json"
        path.write_text(json.dumps(receipt))
        return path

    def record(self, asset_id: str, stage: str, receipt: Path) -> tuple[int, dict[str, Any]]:
        return self.run_cli(
            "record",
            "--state-dir",
            str(self.state_dir),
            "--asset-id",
            asset_id,
            "--stage",
            stage,
            "--receipt",
            str(receipt),
        )

    def drive_to_ready(self, asset_id: str) -> dict[str, Any]:
        self.record(asset_id, "cache", self.cache_receipt(asset_id))
        if self.contract_document["assets"][asset_id]["delivery"]["install_mode"] != "none":
            self.record(asset_id, "install", self.install_receipt(asset_id))
        self.record(asset_id, "runtime", self.runtime_receipt(asset_id))
        self.record(asset_id, "deployment", self.deployment_receipt(asset_id))
        code, projection = self.record(asset_id, "semantic", self.semantic_receipt(asset_id))
        self.assertEqual(0, code)
        return projection

    def assert_schema_valid(self, projection: dict[str, Any]) -> None:
        from jsonschema import Draft202012Validator

        schema = json.loads(READINESS_SCHEMA.read_text())
        errors = sorted(Draft202012Validator(schema).iter_errors(projection), key=str)
        self.assertEqual([], [str(error) for error in errors])


class ContractAndSchemaTests(AcademicAssetTestCase):
    def test_published_schemas_are_valid_and_match_the_python_validators(self) -> None:
        """Guards the v2 defect where the validator and its schema were unsatisfiable together."""
        from jsonschema import Draft202012Validator

        stage_schema = json.loads(STAGE_SCHEMA.read_text())
        for path in (READINESS_SCHEMA, STAGE_SCHEMA, AUTHORIZATION_SCHEMA, ACCEPTANCE_SCHEMA):
            Draft202012Validator.check_schema(json.loads(path.read_text()))

        for stage, keys in aa.STAGE_RECEIPT_KEYS.items():
            definition = stage_schema["$defs"][stage]
            with self.subTest(stage=stage):
                self.assertEqual(sorted(keys), sorted(definition["required"]))
                self.assertEqual(sorted(keys), sorted(definition["properties"]))
                self.assertFalse(definition["additionalProperties"])
                self.assertEqual(
                    f"fs2-serve.nebius.ai/academic-{stage}-receipt/v3",
                    definition["properties"]["schema"]["const"],
                )

    def test_committed_authorizations_cite_the_official_licence_digests(self) -> None:
        """Each authorization must name the exact terms it was granted against."""

        contract = aa.load_contract(REAL_CONTRACT)
        for asset_id, spec in contract["assets"].items():
            document = json.loads(
                (ASSET_ROOT / "contracts" / f"{asset_id}-use-authorization.json").read_text()
            )
            cited = {(r["document_id"], r["sha256"]) for r in document["licence_references"]}
            contracted = {
                (term["document_id"].split("@", 1)[0], term["sha256"])
                for term in spec["acceptance"]["terms"]
            }
            with self.subTest(asset=asset_id):
                self.assertTrue(contracted.issubset(cited))
                self.assertTrue(document["non_exportable"])
                self.assertEqual("academic-non-commercial", document["use_class"])
                self.assertEqual(
                    spec["artifact"]["sha256"], document["artifact_reference"]["sha256"]
                )
                for reference in document["licence_references"]:
                    self.assertTrue(reference["url"].startswith("https://"))

    def test_committed_contract_and_authorizations_are_accepted(self) -> None:
        contract = aa.load_contract(REAL_CONTRACT)
        self.assertEqual(aa.CONTRACT_SCHEMA, contract["schema"])
        for asset_id, spec in contract["assets"].items():
            with self.subTest(asset=asset_id):
                self.assertFalse(spec["delivery"]["embed_in_image"])
                self.assertEqual("tenant-private-volume", spec["delivery"]["mode"])
                path = ASSET_ROOT / "contracts" / f"{asset_id}-use-authorization.json"
                summary = aa.validate_use_authorization(asset_id, spec, path)
                self.assertEqual(TENANT, summary["tenant_id"])

    def test_committed_authorizations_validate_against_their_schema(self) -> None:
        from jsonschema import Draft202012Validator

        schema = json.loads(AUTHORIZATION_SCHEMA.read_text())
        for asset_id in ("alphafold3", "pyrosetta-bindcraft"):
            document = json.loads((ASSET_ROOT / "contracts" / f"{asset_id}-use-authorization.json").read_text())
            with self.subTest(asset=asset_id):
                self.assertEqual([], [str(e) for e in Draft202012Validator(schema).iter_errors(document)])

    def test_committed_formal_acceptance_templates_are_not_usable_as_acceptance(self) -> None:
        contract = aa.load_contract(REAL_CONTRACT)
        for asset_id in ("alphafold3", "pyrosetta-bindcraft"):
            path = ASSET_ROOT / "contracts" / f"{asset_id}-formal-acceptance.template.json"
            with self.subTest(asset=asset_id):
                self.assertIn("REPLACE_", path.read_text())
                with self.assertRaises(aa.IngestionError) as caught:
                    aa.validate_acceptance(asset_id, contract["assets"][asset_id], path)
                self.assertEqual("InvalidInput", caught.exception.state)

    def test_placeholder_digest_and_image_embedding_are_rejected_in_the_contract(self) -> None:
        document = copy.deepcopy(self.contract_document)
        document["assets"]["alphafold3"]["artifact"]["sha256"] = "0" * 64
        self.write_contract(document)
        with self.assertRaises(aa.IngestionError) as caught:
            aa.load_contract(self.contract_path)
        self.assertIn("placeholder", caught.exception.message)

        document = copy.deepcopy(self.contract_document)
        document["assets"]["pyrosetta-bindcraft"]["delivery"]["embed_in_image"] = True
        self.write_contract(document)
        with self.assertRaises(aa.IngestionError):
            aa.load_contract(self.contract_path)

    def test_wheel_validation_rules_are_enforced_not_merely_declared(self) -> None:
        spec = self.contract_document["assets"]["pyrosetta-bindcraft"]["artifact"]
        good = dict(spec)
        aa._validate_python_wheel(self.wheel_path, good["wheel_expectations"])

        wrong_version = self.sources / "wrong-version.whl"
        write_wheel(wrong_version, metadata_version="2025.24+release.8e1e5e54f0")
        with self.assertRaises(aa.IngestionError) as caught:
            aa._validate_python_wheel(wrong_version, good["wheel_expectations"])
        self.assertIn("version", caught.exception.message)

        wrong_tag = self.sources / "wrong-tag.whl"
        write_wheel(wrong_tag, tag="cp311-cp311-linux_x86_64")
        with self.assertRaises(aa.IngestionError) as caught:
            aa._validate_python_wheel(wrong_tag, good["wheel_expectations"])
        self.assertIn("tag", caught.exception.message)

        no_record = self.sources / "no-record.whl"
        write_wheel(no_record, include_record=False)
        with self.assertRaises(aa.IngestionError):
            aa._validate_python_wheel(no_record, good["wheel_expectations"])

    def test_runtime_cache_must_differ_from_the_historical_quarantine(self) -> None:
        document = copy.deepcopy(self.contract_document)
        document["runtime_cache"]["pvc_namespace"] = document["quarantine_cache"]["pvc_namespace"]
        document["runtime_cache"]["pvc_name"] = document["quarantine_cache"]["pvc_name"]
        self.write_contract(document)
        with self.assertRaises(aa.IngestionError) as caught:
            aa.load_contract(self.contract_path)
        self.assertIn("distinct", caught.exception.message)

    def test_fallbacks_stay_independent_alternatives(self) -> None:
        document = copy.deepcopy(self.contract_document)
        document["fallbacks"]["openfold3"]["aliases"] = ["alphafold3"]
        self.write_contract(document)
        with self.assertRaises(aa.IngestionError):
            aa.load_contract(self.contract_path)


class ReadinessStateMachineTests(AcademicAssetTestCase):
    def test_absent_state_is_explicit_and_alternatives_are_distinct(self) -> None:
        code, projection = self.run_cli("status", "--state-dir", str(self.workspace / "missing"))
        self.assertEqual(0, code)
        self.assert_schema_valid(projection)
        self.assertEqual("Blocked", projection["state"])
        self.assertEqual("Blocked", projection["runtime_path_state"])
        self.assertEqual("Pending", projection["formal_license_state"])
        self.assertIsNone(projection["generation"])
        for item in projection["assets"]:
            self.assertEqual("UseAuthorizationMissing", item["state"])
            self.assertEqual("Missing", item["use_authorization_status"])
            self.assertEqual("FormalAcceptancePending", item["formal_license_status"])
            self.assertFalse(item["embed_in_image"])
        alternatives = {item["model_id"]: item for item in projection["fallbacks"]}
        self.assertEqual({"openfold3", "open-binder"}, set(alternatives))
        self.assertEqual(["alphafold3"], alternatives["openfold3"]["does_not_satisfy"])
        self.assertEqual(["bindcraft"], alternatives["open-binder"]["does_not_satisfy"])
        self.assertEqual([], alternatives["openfold3"]["aliases"])

    def test_artifact_without_authorization_is_quarantined_and_cannot_resolve(self) -> None:
        code, projection = self.ingest(
            "quarantine-1", af3_authorization=False, wheel_authorization=False
        )
        self.assertEqual(0, code)
        self.assert_schema_valid(projection)
        for item in projection["assets"]:
            # Presence is reported truthfully; the grant is what stays missing.
            self.assertEqual("UseAuthorizationMissing", item["state"])
            self.assertEqual("ArtifactVerified", item["artifact_status"])
            self.assertEqual(64, len(item["artifact_sha256"]))
            self.assertEqual("Missing", item["use_authorization_status"])
        code, error = self.run_cli(
            "resolve", "--state-dir", str(self.state_dir), "--asset-id", "alphafold3", "--for-tenant-volume"
        )
        self.assertEqual(2, code)
        self.assertEqual("UseAuthorizationMissing", error["state"])
        self.assertIn("quarantined", error["message"])

    def test_authorization_without_artifact_reports_missing_artifact(self) -> None:
        code, projection = self.ingest("authorized-1", af3_artifact=False, wheel_artifact=False)
        self.assertEqual(0, code)
        self.assert_schema_valid(projection)
        for item in projection["assets"]:
            self.assertEqual("MissingArtifact", item["state"])
            self.assertEqual("Granted", item["use_authorization_status"])
            self.assertEqual("Authorized", item["execution_authorization_status"])
            self.assertIsNotNone(item["authorization_receipt_sha256"])

    def test_authorized_artifacts_reach_the_tenant_cache_gate(self) -> None:
        code, projection = self.ingest("poc-1")
        self.assertEqual(0, code)
        self.assert_schema_valid(projection)
        for item in projection["assets"]:
            self.assertEqual("MissingTenantCache", item["state"])
            self.assertEqual("ArtifactVerified", item["artifact_status"])
            self.assertEqual(64, len(item["artifact_sha256"]))
        resolved = self.run_cli(
            "resolve", "--state-dir", str(self.state_dir), "--asset-id", "alphafold3", "--for-tenant-volume"
        )[1]
        self.assertEqual("tenant-private-volume", resolved["delivery_mode"])
        self.assertTrue(resolved["mount_path"].startswith("/opt/fs2/academic/"))

    def test_image_embedding_is_always_refused(self) -> None:
        self.ingest("poc-embed")
        for asset_id in ("alphafold3", "pyrosetta-bindcraft"):
            code, error = self.run_cli(
                "resolve",
                "--state-dir",
                str(self.state_dir),
                "--asset-id",
                asset_id,
                "--for-image-embedding",
            )
            with self.subTest(asset=asset_id):
                self.assertEqual(2, code)
                self.assertIn("never embedded", error["message"])

    def test_full_chain_reaches_ready_and_records_the_runtime_image(self) -> None:
        self.ingest("poc-ready")
        for asset_id in ("alphafold3", "pyrosetta-bindcraft"):
            projection = self.drive_to_ready(asset_id)
        self.assert_schema_valid(projection)
        self.assertEqual("Ready", projection["state"])
        self.assertEqual("Ready", projection["runtime_path_state"])
        self.assertEqual("Pending", projection["formal_license_state"])
        for item in projection["assets"]:
            self.assertEqual("Ready", item["state"])
            self.assertEqual("TenantCacheReady", item["tenant_cache_status"])
            self.assertEqual("RuntimeReady", item["runtime_status"])
            declared = (
                self.contract_document["assets"][item["asset_id"]]["runtime"].get("runtime_image") or {}
            ).get("digest")
            # A validation environment is never reported as a published image.
            self.assertEqual(declared, item["runtime_image_digest"])
            self.assertIsNotNone(item["runtime_environment_digest"])

    def test_runtime_path_is_ready_before_deployment_and_semantic_evidence(self) -> None:
        self.ingest("poc-runtime")
        for asset_id in ("alphafold3", "pyrosetta-bindcraft"):
            self.record(asset_id, "cache", self.cache_receipt(asset_id))
            if self.contract_document["assets"][asset_id]["delivery"]["install_mode"] != "none":
                self.record(asset_id, "install", self.install_receipt(asset_id))
            code, projection = self.record(asset_id, "runtime", self.runtime_receipt(asset_id))
            self.assertEqual(0, code)
        self.assert_schema_valid(projection)
        self.assertEqual("Blocked", projection["state"])
        self.assertEqual("Ready", projection["runtime_path_state"])
        for item in projection["assets"]:
            self.assertEqual("MissingDeployment", item["state"])

    def test_image_identity_mismatch_holds_short_of_ready(self) -> None:
        """A passing runtime proof against a mislabelled image is not a finished runtime."""

        document = copy.deepcopy(self.contract_document)
        image = document["assets"]["alphafold3"]["runtime"]["runtime_image"]
        image["packaged_distribution_version"] = "3.0.3.dev1+g85c4d2050"
        image["expected_distribution_version"] = "3.0.4"
        image["identity_mismatch"] = "the build checkout lacked the release tag"
        image["revalidation_required"] = True
        self.write_contract(document)
        self.contract_document = document

        self.ingest("poc-image-mismatch")
        for asset_id in ("alphafold3", "pyrosetta-bindcraft"):
            self.record(asset_id, "cache", self.cache_receipt(asset_id))
            if self.contract_document["assets"][asset_id]["delivery"]["install_mode"] != "none":
                self.record(asset_id, "install", self.install_receipt(asset_id))
            code, projection = self.record(asset_id, "runtime", self.runtime_receipt(asset_id))
            self.assertEqual(0, code)
        self.assert_schema_valid(projection)
        alphafold3 = self.asset(projection, "alphafold3")
        pyrosetta = self.asset(projection, "pyrosetta-bindcraft")
        self.assertEqual("ImageRebuildPending", alphafold3["state"])
        self.assertEqual("RuntimeSemanticPassedImageRebuildPending", alphafold3["runtime_status"])
        self.assertEqual("RuntimeReady", pyrosetta["runtime_status"])
        # One asset awaiting a rebuild must block the whole runtime path.
        self.assertEqual("Blocked", projection["runtime_path_state"])

    def test_undeclared_image_identity_mismatch_is_refused(self) -> None:
        document = copy.deepcopy(self.contract_document)
        image = document["assets"]["alphafold3"]["runtime"]["runtime_image"]
        image["packaged_distribution_version"] = "3.0.3.dev1+g85c4d2050"
        image["expected_distribution_version"] = "3.0.4"
        image["revalidation_required"] = False
        self.write_contract(document)
        with self.assertRaises(aa.IngestionError) as caught:
            aa.load_contract(self.contract_path)
        self.assertIn("revalidation", caught.exception.message)

    def test_validation_environment_is_never_reported_as_a_published_image(self) -> None:
        """PyRosetta has no published runtime image, so its image digest stays null."""

        self.ingest("poc-digest-separation")
        for asset_id in ("alphafold3", "pyrosetta-bindcraft"):
            projection = self.drive_to_ready(asset_id)
        self.assert_schema_valid(projection)
        pyrosetta = self.asset(projection, "pyrosetta-bindcraft")
        alphafold3 = self.asset(projection, "alphafold3")
        self.assertIsNone(pyrosetta["runtime_image_digest"])
        self.assertIsNotNone(pyrosetta["runtime_environment_digest"])
        declared = self.contract_document["assets"]["alphafold3"]["runtime"]["runtime_image"]["digest"]
        self.assertEqual(declared, alphafold3["runtime_image_digest"])

    def test_evidence_only_image_is_not_published_as_a_runtime_image(self) -> None:
        """A stock image that proved the asset works is evidence, not the shipped wrapper."""

        document = copy.deepcopy(self.contract_document)
        image = document["assets"]["alphafold3"]["runtime"]["runtime_image"]
        image["role"] = "historical-semantic-evidence"
        image["final_wrapper"] = False
        self.write_contract(document)
        self.contract_document = document

        self.ingest("poc-evidence-image")
        self.record("alphafold3", "cache", self.cache_receipt("alphafold3"))
        code, projection = self.record("alphafold3", "runtime", self.runtime_receipt("alphafold3"))
        self.assertEqual(0, code)
        self.assert_schema_valid(projection)
        alphafold3 = self.asset(projection, "alphafold3")
        self.assertIsNone(alphafold3["runtime_image_digest"])
        self.assertEqual(
            document["assets"]["alphafold3"]["runtime"]["runtime_image"]["digest"],
            alphafold3["runtime_environment_digest"],
        )

    def test_runtime_evidence_must_run_against_the_pinned_image(self) -> None:
        self.ingest("poc-wrong-image")
        self.record("alphafold3", "cache", self.cache_receipt("alphafold3"))
        code, error = self.record(
            "alphafold3", "runtime", self.runtime_receipt("alphafold3", image_digest="sha256:" + "9" * 64)
        )
        self.assertEqual(2, code)
        self.assertIn("pinned runtime image", error["message"])

    def test_licence_terms_are_not_a_per_request_admission_gate(self) -> None:
        """An authorized, runtime-ready asset must be servable without a caller receipt."""

        self.ingest("poc-admission")
        for asset_id in ("alphafold3", "pyrosetta-bindcraft"):
            self.record(asset_id, "cache", self.cache_receipt(asset_id))
            if self.contract_document["assets"][asset_id]["delivery"]["install_mode"] != "none":
                self.record(asset_id, "install", self.install_receipt(asset_id))
            code, projection = self.record(asset_id, "runtime", self.runtime_receipt(asset_id))
            self.assertEqual(0, code)
        self.assert_schema_valid(projection)
        for item in projection["assets"]:
            with self.subTest(asset=item["asset_id"]):
                self.assertEqual("AdmittedNoPerRequestLicenseReceipt", item["serving_admission"])
                # Still truthfully pending on the formal axis, and still admitted.
                self.assertEqual("FormalAcceptancePending", item["formal_license_status"])

    def test_serving_is_not_admitted_before_the_runtime_is_proven(self) -> None:
        self.ingest("poc-not-admitted")
        code, projection = self.record("alphafold3", "cache", self.cache_receipt("alphafold3"))
        self.assertEqual(0, code)
        self.assert_schema_valid(projection)
        for item in projection["assets"]:
            self.assertEqual("PendingRuntimeReadiness", item["serving_admission"])

    def test_contract_cannot_demand_a_licence_receipt_per_request(self) -> None:
        document = copy.deepcopy(self.contract_document)
        document["activation_policy"]["request_time_license_receipt_required"] = True
        self.write_contract(document)
        with self.assertRaises(aa.IngestionError):
            aa.load_contract(self.contract_path)

        document = copy.deepcopy(self.contract_document)
        document["assets"]["alphafold3"]["delivery"]["runtime_consumption"][
            "request_time_license_receipt_required"
        ] = True
        self.write_contract(document)
        with self.assertRaises(aa.IngestionError) as caught:
            aa.load_contract(self.contract_path)
        self.assertIn("every inference request", caught.exception.message)

    def test_stage_order_is_enforced(self) -> None:
        self.ingest("poc-order")
        code, error = self.record("alphafold3", "runtime", self.runtime_receipt("alphafold3"))
        self.assertEqual(2, code)
        self.assertEqual("InvalidEvidence", error["state"])
        self.assertIn("cache readiness must be recorded", error["message"])


class LicenseAxisTests(AcademicAssetTestCase):
    def test_formal_acceptance_is_reported_independently_of_operational_readiness(self) -> None:
        self.ingest("poc-formal", af3_acceptance=True, wheel_acceptance=True)
        for asset_id in ("alphafold3", "pyrosetta-bindcraft"):
            projection = self.drive_to_ready(asset_id)
        self.assert_schema_valid(projection)
        self.assertEqual("Recorded", projection["formal_license_state"])
        for item in projection["assets"]:
            self.assertEqual("FormalAcceptanceRecorded", item["formal_license_status"])
            self.assertIsNotNone(item["acceptance_receipt_sha256"])

    def test_operational_readiness_never_implies_formal_acceptance(self) -> None:
        self.ingest("poc-no-formal")
        projection = self.drive_to_ready("alphafold3")
        self.assert_schema_valid(projection)
        alphafold3 = self.asset(projection, "alphafold3")
        self.assertEqual("Ready", alphafold3["state"])
        self.assertEqual("FormalAcceptancePending", alphafold3["formal_license_status"])
        self.assertIsNone(alphafold3["acceptance_receipt_sha256"])
        self.assertEqual("Pending", projection["formal_license_state"])

    def test_authorization_claiming_to_supersede_acceptance_is_rejected(self) -> None:
        path = self.authorization("alphafold3", supersedes_formal_acceptance=True)
        contract = aa.load_contract(self.contract_path)
        with self.assertRaises(aa.IngestionError) as caught:
            aa.validate_use_authorization("alphafold3", contract["assets"]["alphafold3"], path)
        self.assertIn("supersedes_formal_acceptance", caught.exception.message)

    def test_authorization_cannot_grant_redistribution_or_embedding(self) -> None:
        contract = aa.load_contract(self.contract_path)
        for forbidden in ("redistribute", "embed-in-image"):
            path = self.authorization(
                "alphafold3",
                permitted_operations=[
                    "verify-artifact",
                    "stage-tenant-private-volume",
                    "validate-runtime",
                    forbidden,
                ],
            )
            with self.subTest(operation=forbidden):
                with self.assertRaises(aa.IngestionError) as caught:
                    aa.validate_use_authorization("alphafold3", contract["assets"]["alphafold3"], path)
                self.assertEqual("UseAuthorizationMissing", caught.exception.state)
                self.assertIn("never grantable", caught.exception.message)

    def test_execution_grant_is_separate_from_the_staging_grant(self) -> None:
        """Staging and validating an asset must not silently imply permission to run predictions."""
        staging_only = self.authorization(
            "alphafold3",
            permitted_operations=[
                "verify-artifact",
                "stage-tenant-private-volume",
                "validate-runtime",
            ],
        )
        code, projection = self.run_cli(
            "ingest",
            "--state-dir",
            str(self.state_dir),
            "--generation",
            "poc-staging-only",
            "--alphafold3-path",
            str(self.af3_path),
            "--alphafold3-authorization",
            str(staging_only),
        )
        self.assertEqual(0, code)
        self.assert_schema_valid(projection)
        alphafold3 = self.asset(projection, "alphafold3")
        self.assertEqual("Granted", alphafold3["use_authorization_status"])
        self.assertEqual("NotAuthorized", alphafold3["execution_authorization_status"])
        self.assertEqual("MissingTenantCache", alphafold3["state"])

    def test_committed_authorizations_permit_tenant_prediction_execution(self) -> None:
        contract = aa.load_contract(REAL_CONTRACT)
        for asset_id, spec in contract["assets"].items():
            path = ASSET_ROOT / "contracts" / f"{asset_id}-use-authorization.json"
            with self.subTest(asset=asset_id):
                summary = aa.validate_use_authorization(asset_id, spec, path)
                self.assertTrue(summary["execution_authorized"])

    def test_acceptance_requires_a_named_representative_and_institution(self) -> None:
        contract = aa.load_contract(self.contract_path)
        spec = contract["assets"]["alphafold3"]
        blank_institution = self.acceptance(
            "alphafold3",
            tenant={"tenant_id": TENANT, "institution_id": "", "institution_name": "Test Institution"},
        )
        with self.assertRaises(aa.IngestionError) as caught:
            aa.validate_acceptance("alphafold3", spec, blank_institution)
        self.assertIn("institution", caught.exception.message)

        wrong_role = self.acceptance(
            "alphafold3",
            actor={"actor_id": "a", "display_name": "b", "role": "platform-owner"},
        )
        with self.assertRaises(aa.IngestionError) as caught:
            aa.validate_acceptance("alphafold3", spec, wrong_role)
        self.assertIn("representative", caught.exception.message)


class EvidenceValidationTests(AcademicAssetTestCase):
    def test_invalid_digest_is_rejected_without_activation(self) -> None:
        tampered = self.sources / "af3.bin.zst"
        document = copy.deepcopy(self.contract_document)
        document["assets"]["alphafold3"]["artifact"]["sha256"] = "e" * 64
        self.write_contract(document)
        code, error = self.run_cli(
            "ingest",
            "--state-dir",
            str(self.state_dir),
            "--generation",
            "bad-digest",
            "--alphafold3-path",
            str(tampered),
        )
        self.assertEqual(2, code)
        self.assertEqual("ArtifactInvalid", error["state"])
        self.assertFalse((self.state_dir / "generations" / "bad-digest").exists())

    def test_cache_receipt_must_target_the_runtime_claim_and_never_a_shared_cache(self) -> None:
        self.ingest("poc-cache")
        quarantine = self.contract_document["quarantine_cache"]
        code, error = self.record(
            "alphafold3",
            "cache",
            self.cache_receipt(
                "alphafold3",
                pvc_namespace=quarantine["pvc_namespace"],
                pvc_name=quarantine["pvc_name"],
            ),
        )
        self.assertEqual(2, code)
        self.assertEqual("InvalidEvidence", error["state"])

        code, error = self.record(
            "alphafold3", "cache", self.cache_receipt("alphafold3", general_shared_cache=True)
        )
        self.assertEqual(2, code)
        self.assertIn("general shared cache", error["message"])

    def test_cache_receipt_is_bound_to_tenant_artifact_and_size(self) -> None:
        self.ingest("poc-binding")
        code, error = self.record("alphafold3", "cache", self.cache_receipt("alphafold3", tenant_id="tenant-other"))
        self.assertEqual(2, code)
        self.assertIn("authorized tenant", error["message"])

        code, error = self.record("alphafold3", "cache", self.cache_receipt("alphafold3", file_size_bytes=7))
        self.assertEqual(2, code)
        self.assertIn("size", error["message"])

        code, error = self.record(
            "alphafold3", "cache", self.cache_receipt("alphafold3", artifact_sha256="f" * 64)
        )
        self.assertEqual(2, code)
        self.assertIn("active artifact", error["message"])

    def test_absent_institution_metadata_is_accepted_on_the_operational_path(self) -> None:
        self.ingest("poc-null-institution")
        code, projection = self.record("alphafold3", "cache", self.cache_receipt("alphafold3", institution_id=None))
        self.assertEqual(0, code)
        self.assert_schema_valid(projection)
        self.assertEqual("TenantCacheReady", self.asset(projection, "alphafold3")["tenant_cache_status"])

    def test_runtime_receipt_must_prove_offline_mounted_consumption(self) -> None:
        self.ingest("poc-runtime-evidence")
        self.record("alphafold3", "cache", self.cache_receipt("alphafold3"))
        for override, fragment in (
            ({"network_disabled": False}, "egress disabled"),
            ({"image_contains_licensed_bytes": True}, "must not contain licensed bytes"),
            ({"validation_passed": False}, "did not pass"),
            ({"installed_distribution_version": "9.9.9"}, "pinned version"),
            ({"loaded_parameter_arrays": 0}, "expected parameter arrays"),
            ({"loader_source_revision": "deadbeef"}, "pinned upstream revision"),
            ({"offline_validation_kind": "zstd-stream-integrity"}, "different validation"),
            ({"inference_performed": False, "predicted_atom_records": 113,
              "predicted_structure_sha256": "e" * 64}, "without a prediction"),
            ({"predicted_atom_records": 0}, "atom records"),
        ):
            with self.subTest(override=override):
                code, error = self.record("alphafold3", "runtime", self.runtime_receipt("alphafold3", **override))
                self.assertEqual(2, code)
                self.assertEqual("InvalidEvidence", error["state"])
                self.assertIn(fragment, error["message"])

    def test_pinned_cp310_wheel_requires_a_cpython_310_runtime(self) -> None:
        self.ingest("poc-abi")
        self.record("pyrosetta-bindcraft", "cache", self.cache_receipt("pyrosetta-bindcraft"))
        self.record("pyrosetta-bindcraft", "install", self.install_receipt("pyrosetta-bindcraft"))
        code, error = self.record(
            "pyrosetta-bindcraft", "runtime", self.runtime_receipt("pyrosetta-bindcraft", python_version="3.12.7")
        )
        self.assertEqual(2, code)
        self.assertIn("CPython 3.10", error["message"])

    def test_invalid_receipt_states_still_satisfy_the_published_schema(self) -> None:
        """The v2 projection emitted Invalid* states that its own schema forbade."""
        self.ingest("poc-invalid-states")
        self.record("alphafold3", "cache", self.cache_receipt("alphafold3"))
        generation = self.state_dir / "generations" / "poc-invalid-states"
        target = generation / "receipts" / "alphafold3" / "cache.json"
        corrupted = json.loads(target.read_text())
        corrupted["file_size_bytes"] = 3
        target.write_text(json.dumps(corrupted))
        code, projection = self.run_cli("status", "--state-dir", str(self.state_dir))
        self.assertEqual(0, code)
        self.assert_schema_valid(projection)
        alphafold3 = self.asset(projection, "alphafold3")
        self.assertEqual("InvalidTenantCacheReceipt", alphafold3["state"])
        self.assertEqual("InvalidEvidence", alphafold3["tenant_cache_status"])

    def test_receipts_are_immutable_within_a_generation(self) -> None:
        self.ingest("poc-immutable")
        self.record("alphafold3", "cache", self.cache_receipt("alphafold3"))
        code, error = self.record(
            "alphafold3", "cache", self.cache_receipt("alphafold3", observed_at="2026-09-02T23:00:00Z")
        )
        self.assertEqual(2, code)
        self.assertEqual("InvalidState", error["state"])
        self.assertIn("immutable", error["message"])


class LifecycleTests(AcademicAssetTestCase):
    def test_contract_rotation_fails_closed(self) -> None:
        self.ingest("poc-rotate")
        document = copy.deepcopy(self.contract_document)
        document["observed_at"] = "2026-09-03T00:00:00Z"
        self.write_contract(document)
        code, projection = self.run_cli("status", "--state-dir", str(self.state_dir))
        self.assertEqual(0, code)
        self.assert_schema_valid(projection)
        for item in projection["assets"]:
            self.assertEqual("InvalidContract", item["state"])
        code, error = self.record("alphafold3", "cache", self.cache_receipt("alphafold3"))
        self.assertEqual(2, code)
        self.assertEqual("InvalidState", error["state"])

    def test_rotation_and_rollback_preserve_earlier_evidence(self) -> None:
        self.ingest("poc-first")
        self.drive_to_ready("alphafold3")
        code, projection = self.ingest("poc-second", wheel_artifact=False, wheel_authorization=False)
        self.assertEqual(0, code)
        self.assertEqual("MissingTenantCache", self.asset(projection, "alphafold3")["state"])

        code, projection = self.run_cli("rollback", "--state-dir", str(self.state_dir), "--to-generation", "poc-first")
        self.assertEqual(0, code)
        self.assert_schema_valid(projection)
        self.assertEqual("poc-first", projection["generation"])
        self.assertEqual("Ready", self.asset(projection, "alphafold3")["state"])

    def test_corrective_revocation_tombstones_before_removing_payload(self) -> None:
        self.ingest("poc-keep")
        self.ingest("poc-drop")
        code, projection = self.run_cli("rollback", "--state-dir", str(self.state_dir), "--to-generation", "poc-keep")
        self.assertEqual(0, code)
        code, tombstone = self.run_cli(
            "revoke",
            "--state-dir",
            str(self.state_dir),
            "--generation",
            "poc-drop",
            "--reason",
            "invalid-license-acceptance-attribution",
        )
        self.assertEqual(0, code)
        self.assertEqual("invalid-license-acceptance-attribution", tombstone["reason"])
        self.assertFalse((self.state_dir / "generations" / "poc-drop").exists())
        recorded = json.loads((self.state_dir / "revocations" / "poc-drop.json").read_text())
        self.assertEqual(tombstone["manifest_sha256"], recorded["manifest_sha256"])

        from jsonschema import Draft202012Validator

        schema = json.loads((ASSET_ROOT / "schemas" / "revocation.schema.json").read_text())
        self.assertEqual([], [str(e) for e in Draft202012Validator(schema).iter_errors(recorded)])

    def test_active_generation_cannot_be_revoked(self) -> None:
        self.ingest("poc-active")
        code, error = self.run_cli(
            "revoke",
            "--state-dir",
            str(self.state_dir),
            "--generation",
            "poc-active",
            "--reason",
            "license-terminated",
        )
        self.assertEqual(2, code)
        self.assertEqual("InvalidState", error["state"])
        self.assertTrue((self.state_dir / "generations" / "poc-active").exists())

    def test_environment_references_work_without_emitting_paths(self) -> None:
        os.environ["FS2_TEST_STATE_DIR"] = str(self.state_dir)
        os.environ["FS2_TEST_AF3"] = str(self.af3_path)
        self.addCleanup(os.environ.pop, "FS2_TEST_STATE_DIR", None)
        self.addCleanup(os.environ.pop, "FS2_TEST_AF3", None)
        code, projection = self.run_cli(
            "ingest",
            "--state-dir-env",
            "FS2_TEST_STATE_DIR",
            "--generation",
            "poc-env",
            "--alphafold3-path-env",
            "FS2_TEST_AF3",
            "--alphafold3-authorization",
            str(self.authorization("alphafold3")),
        )
        self.assertEqual(0, code)
        self.assert_schema_valid(projection)
        serialized = json.dumps(projection)
        self.assertNotIn(str(self.af3_path), serialized)
        self.assertNotIn(str(self.state_dir), serialized)

    def test_state_directory_must_stay_owner_only(self) -> None:
        self.ingest("poc-perms")
        self.state_dir.chmod(0o755)
        code, error = self.run_cli("status", "--state-dir", str(self.state_dir))
        self.assertEqual(2, code)
        self.assertEqual("InvalidState", error["state"])


if __name__ == "__main__":
    unittest.main()
