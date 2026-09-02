from __future__ import annotations

import contextlib
import hashlib
import importlib.util
import io
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).parents[1] / "scripts" / "academic_assets.py"
SPEC = importlib.util.spec_from_file_location("academic_assets", SCRIPT)
assert SPEC and SPEC.loader
academic_assets = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(academic_assets)


def sha(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


class AcademicAssetTests(unittest.TestCase):
    maxDiff = None

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.inputs = self.root / "inputs"
        self.inputs.mkdir()
        self.state = self.root / "state"
        self.contract_path = self.root / "contract.json"
        self.contents = {
            "alphafold3": bytes.fromhex("28b52ffd") + b"approved-af3-fixture",
            "pyrosetta-bindcraft": bytes.fromhex("504b0304") + b"approved-pyrosetta-fixture",
        }
        self.filenames = {
            "alphafold3": "af3.bin.zst",
            "pyrosetta-bindcraft": "pyrosetta-fixture.conda",
        }
        self.contract = self.make_contract()
        self.write_json(self.contract_path, self.contract)
        self.artifacts: dict[str, Path] = {}
        self.acceptances: dict[str, Path] = {}
        for asset_id, content in self.contents.items():
            artifact = self.inputs / self.filenames[asset_id]
            artifact.write_bytes(content)
            self.artifacts[asset_id] = artifact
            receipt = self.inputs / f"{asset_id}.receipt.json"
            self.write_json(receipt, self.make_acceptance(asset_id, organization=True))
            self.acceptances[asset_id] = receipt

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def make_contract(self) -> dict:
        assets = {}
        for asset_id, model_id in (("alphafold3", "alphafold3"), ("pyrosetta-bindcraft", "bindcraft")):
            assets[asset_id] = {
                "model_id": model_id,
                "display_name": asset_id,
                "artifact": {
                    "filename": self.filenames[asset_id],
                    "version": "fixture-v1",
                    "source_revision": f"fixture://{asset_id}/v1",
                    "size_bytes": len(self.contents[asset_id]),
                    "sha256": sha(self.contents[asset_id]),
                    "magic_hex": self.contents[asset_id][:4].hex(),
                    "structural_validation": "none",
                    "source_url": f"https://example.invalid/{self.filenames[asset_id]}",
                    "source_metadata": {},
                },
                "license": {
                    "license_id": "fixture-license",
                    "allowed_users": "fixture",
                    "redistribution": "forbidden",
                    "containerization": "private only",
                    "access_procedure": "fixture",
                },
                "acceptance": {
                    "scope": "academic-noncommercial",
                    "accepted_by_roles": ["authorized-individual", "authorized-organization-representative"],
                    "distribution_scopes": ["individual-only", "organization-internal"],
                    "terms": [{"document_id": f"{asset_id}-terms", "sha256": sha(f"{asset_id}-terms".encode())}],
                    "source_claims": {
                        "received_directly_from_licensor": True,
                        "source": f"fixture-{asset_id}",
                        "revision": "v1",
                    },
                },
                "private_layer": {
                    "allowed": asset_id == "pyrosetta-bindcraft",
                    "destination": f"/opt/fs2/academic-assets/{self.filenames[asset_id]}",
                    "redistributable": False,
                },
            }
        return {
            "schema": "fs2-serve.nebius.ai/academic-assets/v1",
            "observed_at": "2026-09-02T00:00:00Z",
            "private_registry": {
                "project_id": "project-fixture",
                "region": "eu-north1",
                "registry_id": "registry-fixture",
                "repository_prefix": "cr.eu-north1.nebius.cloud/fixture/academic/",
            },
            "private_cache": {
                "project_id": "project-fixture",
                "region": "eu-north1",
                "cluster_id": "cluster-fixture",
                "filesystem_id": "filesystem-fixture",
                "pvc_namespace": "fs2-models",
                "pvc_name": "academic-assets-fixture",
                "distribution_scope": "organization-internal",
            },
            "assets": assets,
            "fallbacks": {
                "open-binder": {
                    "model_id": "open-binder",
                    "relationship": "independent-operational-fallback",
                    "aliases": [],
                    "does_not_satisfy": ["bindcraft"],
                },
                "openfold3": {
                    "model_id": "openfold3",
                    "relationship": "independent-operational-fallback",
                    "aliases": [],
                    "does_not_satisfy": ["alphafold3"],
                },
            },
        }

    def make_acceptance(self, asset_id: str, *, organization: bool) -> dict:
        accepted = self.contract["assets"][asset_id]["acceptance"]
        return {
            "schema": "fs2-serve.nebius.ai/academic-license-acceptance/v1",
            "asset_id": asset_id,
            "accepted": True,
            "accepted_at": "2026-09-02T00:00:00Z",
            "accepted_by_role": (
                "authorized-organization-representative" if organization else "authorized-individual"
            ),
            "scope": accepted["scope"],
            "distribution_scope": "organization-internal" if organization else "individual-only",
            "terms": accepted["terms"],
            "source_claims": accepted["source_claims"],
        }

    @staticmethod
    def write_json(path: Path, value: dict) -> None:
        path.write_text(json.dumps(value), encoding="utf-8")

    def call(self, arguments: list[str], *, env: dict[str, str] | None = None) -> tuple[int, dict]:
        output = io.StringIO()
        previous = os.environ.copy()
        if env:
            os.environ.update(env)
        try:
            with contextlib.redirect_stdout(output):
                code = academic_assets.main(["--contract", str(self.contract_path), *arguments])
        finally:
            os.environ.clear()
            os.environ.update(previous)
        return code, json.loads(output.getvalue())

    def ingest_arguments(self, generation: str, *, include_artifacts: bool = True) -> list[str]:
        args = ["ingest", "--state-dir", str(self.state), "--generation", generation]
        for asset_id in ("alphafold3", "pyrosetta-bindcraft"):
            args.extend([f"--{asset_id}-acceptance", str(self.acceptances[asset_id])])
            if include_artifacts:
                args.extend([f"--{asset_id}-path", str(self.artifacts[asset_id])])
        return args

    def record(self, asset_id: str, stage: str, value: dict) -> tuple[int, dict]:
        receipt = self.inputs / f"{asset_id}-{stage}.json"
        self.write_json(receipt, value)
        return self.call(
            [
                "record",
                "--state-dir",
                str(self.state),
                "--asset-id",
                asset_id,
                "--stage",
                stage,
                "--receipt",
                str(receipt),
            ]
        )

    def test_absent_state_is_explicit_and_alternatives_are_distinct(self) -> None:
        code, result = self.call(["status", "--state-dir", str(self.state)])
        self.assertEqual(code, 0)
        self.assertEqual([item["state"] for item in result["assets"]], ["MissingLicenseAcceptance"] * 2)
        self.assertEqual(
            [(item["model_id"], item["does_not_satisfy"]) for item in result["fallbacks"]],
            [("open-binder", ["bindcraft"]), ("openfold3", ["alphafold3"])],
        )

    def test_acceptance_without_files_reports_missing_artifact(self) -> None:
        code, result = self.call(self.ingest_arguments("accepted-only", include_artifacts=False))
        self.assertEqual(code, 0)
        self.assertEqual([item["state"] for item in result["assets"]], ["MissingArtifact"] * 2)

    def test_artifact_without_acceptance_is_quarantined_and_cannot_resolve(self) -> None:
        code, result = self.call(
            [
                "ingest",
                "--state-dir",
                str(self.state),
                "--generation",
                "no-license",
                "--alphafold3-path",
                str(self.artifacts["alphafold3"]),
            ]
        )
        self.assertEqual(code, 0)
        by_id = {item["asset_id"]: item for item in result["assets"]}
        self.assertEqual(by_id["alphafold3"]["state"], "MissingLicenseAcceptance")
        self.assertEqual(by_id["alphafold3"]["stages"]["artifact"], "verified")
        code, result = self.call(
            ["resolve", "--state-dir", str(self.state), "--asset-id", "alphafold3"]
        )
        self.assertEqual(code, 2)
        self.assertEqual(result["state"], "MissingLicenseAcceptance")

    def test_invalid_digest_is_rejected_without_activation(self) -> None:
        self.artifacts["alphafold3"].write_bytes(bytes.fromhex("28b52ffd") + b"tampered-af3-fixtur")
        code, result = self.call(self.ingest_arguments("invalid"))
        self.assertEqual(code, 2)
        self.assertEqual(result["state"], "ArtifactInvalid")
        self.assertFalse((self.state / "active.json").exists())

    def test_contract_rotation_fails_closed(self) -> None:
        code, _ = self.call(self.ingest_arguments("old-contract"))
        self.assertEqual(code, 0)
        self.contract["observed_at"] = "2026-09-03T00:00:00Z"
        self.write_json(self.contract_path, self.contract)
        code, result = self.call(["status", "--state-dir", str(self.state)])
        self.assertEqual(code, 0)
        self.assertEqual([item["state"] for item in result["assets"]], ["InvalidContract"] * 2)

    def test_valid_chain_rotation_and_rollback(self) -> None:
        code, result = self.call(self.ingest_arguments("generation-one"))
        self.assertEqual(code, 0)
        self.assertEqual([item["state"] for item in result["assets"]], ["MissingCache"] * 2)
        self.assertEqual(stat.S_IMODE(self.state.stat().st_mode), 0o700)
        manifest_text = (self.state / "generations" / "generation-one" / "generation.json").read_text()
        self.assertNotIn(str(self.inputs), manifest_text)
        for asset_id in ("alphafold3", "pyrosetta-bindcraft"):
            staged = self.state / "generations" / "generation-one" / "assets" / asset_id / self.filenames[asset_id]
            self.assertEqual(stat.S_IMODE(staged.stat().st_mode), 0o400)

        image_digests = {}
        for index, asset_id in enumerate(("alphafold3", "pyrosetta-bindcraft"), start=1):
            artifact_sha = sha(self.contents[asset_id])
            image_digest = f"sha256:{str(index) * 64}"
            image_digests[asset_id] = image_digest
            code, _ = self.record(
                asset_id,
                "cache",
                {
                    "schema": "fs2-serve.nebius.ai/academic-cache-receipt/v1",
                    "asset_id": asset_id,
                    "artifact_sha256": artifact_sha,
                    "observed_at": "2026-09-02T00:00:30Z",
                    **{
                        key: value
                        for key, value in self.contract["private_cache"].items()
                        if key != "distribution_scope"
                    },
                    "file_size_bytes": len(self.contents[asset_id]),
                    "verified": True,
                },
            )
            self.assertEqual(code, 0)
            code, _ = self.record(
                asset_id,
                "image",
                {
                    "schema": "fs2-serve.nebius.ai/academic-image-receipt/v1",
                    "asset_id": asset_id,
                    "artifact_sha256": artifact_sha,
                    "observed_at": "2026-09-02T00:01:00Z",
                    "repository": f"cr.eu-north1.nebius.cloud/fixture/academic/{asset_id}",
                    "image_digest": image_digest,
                    "visibility": "private",
                    "redistributable": False,
                    "asset_delivery_mode": (
                        "external-private-cache" if asset_id == "alphafold3" else "embedded-private-layer"
                    ),
                    "contains_licensed_asset": asset_id == "pyrosetta-bindcraft",
                    "builder": "fixture",
                },
            )
            self.assertEqual(code, 0)
            code, _ = self.record(
                asset_id,
                "deployment",
                {
                    "schema": "fs2-serve.nebius.ai/academic-deployment-receipt/v1",
                    "asset_id": asset_id,
                    "artifact_sha256": artifact_sha,
                    "observed_at": "2026-09-02T00:02:00Z",
                    "model_id": self.contract["assets"][asset_id]["model_id"],
                    "image_digest": image_digest,
                    "deployed": True,
                    "resource_uid": f"uid-{asset_id}",
                },
            )
            self.assertEqual(code, 0)
            code, result = self.record(
                asset_id,
                "semantic",
                {
                    "schema": "fs2-serve.nebius.ai/academic-semantic-receipt/v1",
                    "asset_id": asset_id,
                    "artifact_sha256": artifact_sha,
                    "observed_at": "2026-09-02T00:03:00Z",
                    "model_id": self.contract["assets"][asset_id]["model_id"],
                    "image_digest": image_digest,
                    "passed": True,
                    "validator_digest": f"sha256:{'a' * 64}",
                },
            )
            self.assertEqual(code, 0)
        self.assertEqual(result["state"], "Ready")

        code, rotated = self.call(self.ingest_arguments("generation-two"))
        self.assertEqual(code, 0)
        self.assertEqual(rotated["generation"], "generation-two")
        self.assertTrue((self.state / "generations" / "generation-one").is_dir())
        code, rolled_back = self.call(
            ["rollback", "--state-dir", str(self.state), "--to-generation", "generation-one"]
        )
        self.assertEqual(code, 0)
        self.assertEqual(rolled_back["generation"], "generation-one")
        self.assertEqual(rolled_back["state"], "Ready")

        code, tombstone = self.call(
            [
                "revoke",
                "--state-dir",
                str(self.state),
                "--generation",
                "generation-two",
                "--reason",
                "invalid-license-acceptance-attribution",
            ]
        )
        self.assertEqual(code, 0)
        self.assertEqual(tombstone["generation"], "generation-two")
        self.assertFalse((self.state / "generations" / "generation-two").exists())
        self.assertTrue((self.state / "revocations" / "generation-two.json").is_file())

    def test_private_layer_requires_organization_scope(self) -> None:
        for asset_id in self.acceptances:
            self.write_json(self.acceptances[asset_id], self.make_acceptance(asset_id, organization=False))
        code, _ = self.call(self.ingest_arguments("individual"))
        self.assertEqual(code, 0)
        code, result = self.call(
            [
                "resolve",
                "--state-dir",
                str(self.state),
                "--asset-id",
                "pyrosetta-bindcraft",
                "--for-private-layer",
            ]
        )
        self.assertEqual(code, 2)
        self.assertEqual(result["state"], "MissingLicenseAcceptance")
        artifact_sha = sha(self.contents["alphafold3"])
        code, result = self.record(
            "alphafold3",
            "cache",
            {
                "schema": "fs2-serve.nebius.ai/academic-cache-receipt/v1",
                "asset_id": "alphafold3",
                "artifact_sha256": artifact_sha,
                "observed_at": "2026-09-02T00:00:30Z",
                **{
                    key: value
                    for key, value in self.contract["private_cache"].items()
                    if key != "distribution_scope"
                },
                "file_size_bytes": len(self.contents["alphafold3"]),
                "verified": True,
            },
        )
        self.assertEqual(code, 2)
        self.assertEqual(result["state"], "MissingLicenseAcceptance")
        code, result = self.call(
            [
                "resolve",
                "--state-dir",
                str(self.state),
                "--asset-id",
                "alphafold3",
                "--for-shared-cache",
            ]
        )
        self.assertEqual(code, 2)
        self.assertEqual(result["state"], "MissingLicenseAcceptance")

    def test_invalid_cache_identity_and_af3_embedding_fail_closed(self) -> None:
        code, _ = self.call(self.ingest_arguments("receipt-gates"))
        self.assertEqual(code, 0)
        artifact_sha = sha(self.contents["alphafold3"])
        invalid_cache = {
            "schema": "fs2-serve.nebius.ai/academic-cache-receipt/v1",
            "asset_id": "alphafold3",
            "artifact_sha256": artifact_sha,
            "observed_at": "2026-09-02T00:00:30Z",
            **{
                key: value
                for key, value in self.contract["private_cache"].items()
                if key != "distribution_scope"
            },
            "filesystem_id": "wrong-filesystem",
            "file_size_bytes": len(self.contents["alphafold3"]),
            "verified": True,
        }
        code, result = self.record("alphafold3", "cache", invalid_cache)
        self.assertEqual(code, 2)
        self.assertEqual(result["state"], "InvalidEvidence")

        invalid_cache["filesystem_id"] = self.contract["private_cache"]["filesystem_id"]
        code, _ = self.record("alphafold3", "cache", invalid_cache)
        self.assertEqual(code, 0)
        code, result = self.record(
            "alphafold3",
            "image",
            {
                "schema": "fs2-serve.nebius.ai/academic-image-receipt/v1",
                "asset_id": "alphafold3",
                "artifact_sha256": artifact_sha,
                "observed_at": "2026-09-02T00:01:00Z",
                "repository": "cr.eu-north1.nebius.cloud/fixture/academic/alphafold3",
                "image_digest": f"sha256:{'1' * 64}",
                "visibility": "private",
                "redistributable": False,
                "asset_delivery_mode": "embedded-private-layer",
                "contains_licensed_asset": True,
                "builder": "fixture",
            },
        )
        self.assertEqual(code, 2)
        self.assertEqual(result["state"], "InvalidEvidence")

    def test_environment_path_references_work_without_outputting_paths(self) -> None:
        env = {
            "FIXTURE_STATE": str(self.state),
            "FIXTURE_AF3": str(self.artifacts["alphafold3"]),
            "FIXTURE_AF3_ACCEPTANCE": str(self.acceptances["alphafold3"]),
        }
        code, result = self.call(
            [
                "ingest",
                "--state-dir-env",
                "FIXTURE_STATE",
                "--generation",
                "env-input",
                "--alphafold3-path-env",
                "FIXTURE_AF3",
                "--alphafold3-acceptance-env",
                "FIXTURE_AF3_ACCEPTANCE",
            ],
            env=env,
        )
        self.assertEqual(code, 0)
        rendered = json.dumps(result)
        self.assertNotIn(str(self.inputs), rendered)
        by_id = {item["asset_id"]: item for item in result["assets"]}
        self.assertEqual(by_id["alphafold3"]["state"], "MissingCache")
        self.assertEqual(by_id["pyrosetta-bindcraft"]["state"], "MissingLicenseAcceptance")


if __name__ == "__main__":
    unittest.main()
