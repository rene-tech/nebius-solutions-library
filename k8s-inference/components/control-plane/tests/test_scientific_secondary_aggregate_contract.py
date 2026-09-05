from __future__ import annotations

import hashlib
import json
import re
from pathlib import PurePosixPath

from conftest import CATALOG_ROOT, SOLUTION_ROOT

from fs2_serve.scientific_batch.execution import FileScientificManifestRenderer
from fs2_serve.scientific_batch.profile_catalog import ScientificProfileCatalog

SECONDARY_ACTIVE = {
    "esmfold2": {
        "digest": "sha256:b372dd7e34e464680a82456ca31b403b0ac0d0851511930d471b67041adbbde3",
        "variant": "biohub-v3-4-0",
        "namespace": "fs2-models",
        "stages": ("prepare-input", "fold"),
        "artifacts": ("esmfold2-trunk", "esmc-6b", "esmfold2-ccd"),
        "stage_artifacts": {"prepare-input": (), "fold": ("esmfold2-trunk", "esmc-6b", "esmfold2-ccd")},
        "cache_stages": (),
        "uid": 10001,
        "gid": 10001,
        "receipt": "e52236de244cfb52ec5e6d6486812e1518d0f6c9e0ade18608b9d063cc8971c2",
        "evidence": "esmfold2-h100-semantic-qualification.json",
        "qualified_at": "2026-09-05T00:11:48Z",
    },
    "esmfold2-fast": {
        "digest": "sha256:6eaf386a9bb4453d5048e16c28b8ca4236ae0f222185e33d5a7a49a1e1c8fa35",
        "variant": "biohub-v3-4-0",
        "namespace": "fs2-models",
        "stages": ("prepare-input", "fold"),
        "artifacts": ("esmfold2-fast-trunk", "esmc-6b", "esmfold2-ccd"),
        "stage_artifacts": {
            "prepare-input": (),
            "fold": ("esmfold2-fast-trunk", "esmc-6b", "esmfold2-ccd"),
        },
        "cache_stages": (),
        "uid": 10001,
        "gid": 10001,
        "receipt": "b78dce5a942472b9427ca4f84a2a0b00baed1c3beca40d230913f1745c7df545",
        "evidence": "esmfold2-fast-h100-semantic-qualification.json",
        "qualified_at": "2026-09-05T00:11:48Z",
    },
    "protenix-v2": {
        "digest": "sha256:ac8f7c2c35d2bc911281f9d4a8aa9779e2cb955cdb1c2c2d37eb31d89669980e",
        "variant": "upstream-v2-0-0",
        "namespace": "fs2-models",
        "stages": ("prepare-data", "sample-structure"),
        "artifacts": ("protenix-v2",),
        "stage_artifacts": {"prepare-data": ("protenix-v2",), "sample-structure": ("protenix-v2",)},
        "cache_stages": ("prepare-data", "sample-structure"),
        "uid": 10001,
        "gid": 10001,
        "receipt": "e2935d0c8596573d7934a25468a6740dace8cb1e53fec3b8bf8cd5c5e52cb33a",
        "evidence": "protenix-v2-h100-semantic-qualification.json",
        "qualified_at": "2026-09-05T00:12:44Z",
    },
    "openfold3-openbind": {
        "digest": "sha256:6b15da4b2258c0c385adc1dbc7799493f3768cb4881f7990cb957f2c3b6759e4",
        "variant": "upstream-openbind-v0-5-0",
        "namespace": "fs2-models",
        "stages": ("data-pipeline", "inference"),
        "artifacts": ("openfold3-openbind-0", "openfold3-components-bcif"),
        "stage_artifacts": {
            "data-pipeline": (),
            "inference": ("openfold3-openbind-0", "openfold3-components-bcif"),
        },
        "cache_stages": ("inference",),
        "uid": 10001,
        "gid": 10001,
        "receipt": "2590708d8932ddef795957a91215dd20ca7e8f8666b4aa9d50e782b212029d09",
        "evidence": "openfold3-openbind-h100-semantic-qualification.json",
        "qualified_at": "2026-09-05T00:12:15Z",
    },
    "alphafold3": {
        "digest": "sha256:ecc3e7352da7984e854f67d8024ed28fa6dbbbf7cfae39aa5a50f8a29eda85e7",
        "variant": "upstream-v3-0-4",
        "namespace": "fs2-academic-poc",
        "stages": ("data-pipeline", "inference"),
        "artifacts": ("alphafold3-parameters", "alphafold3-public-databases-v3.0"),
        "stage_artifacts": {
            "data-pipeline": ("alphafold3-public-databases-v3.0",),
            "inference": ("alphafold3-parameters",),
        },
        "cache_stages": ("inference",),
        "uid": 1001,
        "gid": 1001,
        "receipt": "095e4d8da54621329d65a1bdae0e9f5b70d9bb305165d7d3f8e26315fe604b75",
        "evidence": "alphafold3-h100-semantic-qualification.json",
        "qualified_at": "2026-09-04T16:57:43Z",
    },
}

COMPLETE_FLEET = {
    "boltzgen",
    "proteina-complexa",
    "bindcraft",
    "mosaic",
    "rfdiffusion",
    *SECONDARY_ACTIVE,
}


def _documents() -> tuple[dict[str, object], dict[str, object]]:
    profiles = json.loads((CATALOG_ROOT / "contracts/scientific-workload-profiles.json").read_text(encoding="utf-8"))
    executions = json.loads((CATALOG_ROOT / "contracts/scientific-execution-map.json").read_text(encoding="utf-8"))
    return profiles, executions


def _covers(mount_path: str, artifact_path: str) -> bool:
    mount = PurePosixPath(mount_path)
    artifact = PurePosixPath(artifact_path)
    return mount == artifact or mount in artifact.parents


def test_complete_fleet_has_consistent_public_acceptance_evidence_state() -> None:
    profiles_document, execution_document = _documents()
    profiles = {item["model_id"]: item for item in profiles_document["profiles"]}
    executions = {item["model_id"]: item for item in execution_document["models"]}

    assert set(profiles) == COMPLETE_FLEET
    assert set(executions) == COMPLETE_FLEET
    catalog = ScientificProfileCatalog.load(CATALOG_ROOT)
    assert {profile.model_id for profile in catalog.list()} == COMPLETE_FLEET

    for model_id, expected in SECONDARY_ACTIVE.items():
        profile = profiles[model_id]
        identity = profile["execution_identity"]
        assert profile["state"] in {"active", "qualified"}
        assert profile["route_exposed"] is True
        assert profile["source"]["classification"] == "qualified-input"
        assert profile["interface"]["mcp"]["discoverable"] is True
        assert profile["interface"]["mcp"]["invocable"] is True
        assert profile["semantic_validation"]["state"] == profile["state"]
        qualification = profile["qualification"]
        assert qualification["h100_semantic_receipt_sha256"] == expected["receipt"]
        assert qualification["execution_map_sha256"] == profiles["boltzgen"]["qualification"]["execution_map_sha256"]
        if profile["state"] == "active":
            assert qualification["public_completion_receipt_sha256"] is None
            assert qualification["scheduler_eligibility_receipt_sha256"] is None
            assert qualification["qualified_at"] == expected["qualified_at"]
        else:
            assert re.fullmatch(
                r"[a-f0-9]{64}", qualification["public_completion_receipt_sha256"]
            )
            assert re.fullmatch(
                r"[a-f0-9]{64}",
                qualification["scheduler_eligibility_receipt_sha256"],
            )
        evidence_directory = "live-h100-20260904" if model_id == "alphafold3" else "live-h100-20260905"
        evidence = (
            SOLUTION_ROOT
            / "models/cancer-immunotherapy/images/structure-secondary/evidence"
            / evidence_directory
            / expected["evidence"]
        )
        assert hashlib.sha256(evidence.read_bytes()).hexdigest() == expected["receipt"]
        assert identity["runtime_image_digest"] == expected["digest"]
        assert re.fullmatch(r"[a-f0-9]{64}", identity["artifact_manifest_digest"])
        assert re.fullmatch(r"[a-f0-9]{64}", identity["execution_identity_sha256"])
        assert profile["resources"]["compatible_pool_ids"] == ["h100-reserved-8x", "h100-1x"]
        assert "accelerator.fs2.nebius/pool-id" not in profile["resources"]["required_node_labels"]
        assert tuple(item["artifact_id"] for item in profile["runtime_artifacts"]) == expected["artifacts"]
        assert catalog.get(model_id).model_id == model_id

        execution = executions[model_id]
        assert execution["variant_id"] == expected["variant"]
        assert execution["workload_namespace"] == expected["namespace"]
        assert execution["execution_identity_sha256"] == identity["execution_identity_sha256"]
        expected_artifact_identity = hashlib.sha256(
            json.dumps(execution["runtime_artifacts"], sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        assert identity["artifact_manifest_digest"] == expected_artifact_identity
        assert tuple(stage["stage_id"] for stage in execution["stages"]) == expected["stages"]
        assert tuple(item["artifact_id"] for item in execution["runtime_artifacts"]) == expected["artifacts"]
        assert all(
            "accelerator.fs2.nebius/pool-id" not in stage["required_node_labels"] for stage in execution["stages"]
        )

    renderer = FileScientificManifestRenderer(
        path=CATALOG_ROOT / "contracts/scientific-execution-map.json",
        profiles=catalog,
    )
    assert set(renderer.variants) == COMPLETE_FLEET


def test_secondary_localizations_and_stage_mounts_are_exact() -> None:
    profiles_document, execution_document = _documents()
    profiles = {item["model_id"]: item for item in profiles_document["profiles"]}
    executions = {item["model_id"]: item for item in execution_document["models"]}

    for model_id, expected in SECONDARY_ACTIVE.items():
        profile_artifacts = {item["artifact_id"]: item for item in profiles[model_id]["runtime_artifacts"]}
        execution = executions[model_id]
        localizations = {item["artifact_id"]: item for item in execution["runtime_artifacts"]}
        assert set(localizations) == set(expected["artifacts"])
        for artifact_id, localization in localizations.items():
            requirement = profile_artifacts[artifact_id]
            assert localization["content_digest"] == "sha256:" + requirement["content_identity"]["digest_sha256"]
            assert re.fullmatch(r"sha256:[a-f0-9]{64}", localization["localization_receipt_digest"])
            assert ("file_manifest" in localization) != ("aggregate_tree" in localization)
            if "file_manifest" in localization:
                assert localization["file_manifest"] == requirement["file_manifest"]
                assert {item["path"] for item in localization["file_manifest"]} == set(requirement["required_files"])
            else:
                assert localization["aggregate_tree"]["expanded_bytes"] == requirement["content_identity"]["size_bytes"]
                assert localization["aggregate_tree"]["manifest_sha256"] == requirement["readiness_manifest_sha256"]

        for stage in execution["stages"]:
            stage_id = stage["stage_id"]
            assert stage["image"].endswith("@" + expected["digest"])
            assert stage["workspace_uid"] == expected["uid"]
            assert stage["workspace_gid"] == expected["gid"]
            assert sum(item["kind"] == "artifact-workspace" for item in stage["mounts"]) == 1
            cache_mounts = [item for item in stage["mounts"] if item["kind"] == "runtime-cache"]
            assert bool(cache_mounts) == (stage_id in expected["cache_stages"])
            if cache_mounts:
                assert cache_mounts == [
                    {
                        "name": "runtime-cache",
                        "kind": "runtime-cache",
                        "claim_name": "fs2-scientific-runtime-cache",
                        "host_path": None,
                        "mount_path": "/cache",
                        "sub_path": None,
                        "read_only": False,
                    }
                ]
                assert any(value.startswith("/cache/") for value in stage["environment"].values())

            artifact_mounts = [item for item in stage["mounts"] if item["kind"] in {"reference", "private"}]
            expected_artifacts = expected["stage_artifacts"][stage_id]
            assert len(artifact_mounts) == len(expected_artifacts)
            for artifact_id in expected_artifacts:
                assert any(
                    _covers(item["mount_path"], localizations[artifact_id]["mount_path"]) for item in artifact_mounts
                )
            if any(item["host_path"] is not None for item in artifact_mounts):
                assert stage["required_node_labels"]["storage.fs2.nebius/reference-data"] == "true"


def test_alphafold3_keeps_public_and_academic_planes_separate() -> None:
    _, execution_document = _documents()
    execution = next(item for item in execution_document["models"] if item["model_id"] == "alphafold3")
    stages = {item["stage_id"]: item for item in execution["stages"]}
    data_mounts = stages["data-pipeline"]["mounts"]
    inference_mounts = stages["inference"]["mounts"]

    assert execution["access_profile"] == "academic"
    assert stages["data-pipeline"]["service_account_name"] == "fs2-academic-runner"
    assert stages["inference"]["service_account_name"] == "fs2-academic-runner"
    assert any(
        item["kind"] == "reference"
        and item["host_path"] == "/mnt/fs2-reference-data/data"
        and item["mount_path"] == "/reference-data"
        and item["sub_path"] is None
        for item in data_mounts
    )
    assert all(item["kind"] != "private" for item in data_mounts)
    assert any(
        item["kind"] == "private"
        and item["claim_name"] == "academic-assets-runtime-rwx"
        and item["mount_path"] == "/models/af3.bin.zst"
        and item["sub_path"] == "alphafold3/af3.bin.zst"
        and item["read_only"] is True
        for item in inference_mounts
    )
    assert all(item["kind"] != "reference" and item["host_path"] is None for item in inference_mounts)

    artifacts = {item["artifact_id"]: item for item in execution["runtime_artifacts"]}
    parameters = artifacts["alphafold3-parameters"]
    assert parameters["content_digest"] == ("sha256:74d0258616917cd122f5eab6d076afe4a8930e96823851e65e4f777dfb1f33ff")
    assert parameters["localization_receipt_digest"] == (
        "sha256:9c89f122ea3616efe70e07c2c27dddf236d347cab4002498c4dab9677d138bd4"
    )

    reference = artifacts["alphafold3-public-databases-v3.0"]
    assert reference["aggregate_tree"] == {
        "storage_kind": "reference-data-plane",
        "tree_sha256": "d27b8956170b5b0cf0f7daadf53a34e38cbe725dafbe9c91af86c671b32dfaea",
        "manifest_sha256": "aa585259ce05393cd38db1693299ed9ec7f9c421aa4e1159f8d5aa0eb0ba9748",
        "inventory_sha256": "38af3baa89a66cd24dec785279670a2e37597f98d206f555a04c138c6be71579",
        "manifest_algorithm": "fs2-serve.nebius.ai/reference-data-manifest/v1",
        "file_count": 195867,
        "directory_count": 1,
        "expanded_bytes": 672435030513,
        "canonical_path": (
            "datasets/alphafold3-public-databases-v3.0/v3.0-paper-snapshot-2022-09-28/sha256/"
            "d27b8956170b5b0cf0f7daadf53a34e38cbe725dafbe9c91af86c671b32dfaea"
        ),
        "marker_relative_path": ".fs2-manifest-sha256",
    }
    receipt_path = (
        SOLUTION_ROOT
        / "models/cancer-immunotherapy/images/structure-secondary/evidence/live-h100-20260904"
        / "alphafold3-reference-terminal-receipt.json"
    )
    receipt_bytes = receipt_path.read_bytes()
    canonical_receipt = json.dumps(
        reference["verification_receipt"], sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode()
    assert receipt_bytes == canonical_receipt
    assert len(receipt_bytes) == 1207
    assert reference["localization_receipt_digest"] == "sha256:" + hashlib.sha256(receipt_bytes).hexdigest()
    assert reference["localization_receipt_digest"] == (
        "sha256:b049e69846867caa75ef140e105a962fcf14e5c78ec8bfd97741cced32a8f6a6"
    )

    assert stages["inference"]["environment"] == {
        "FS2_AF3_CACHE_ROOT": "/cache/alphafold3",
        "FS2_AF3_JAX_CACHE_DIR": "/cache/alphafold3/jax",
        "FS2_AF3_TRITON_CACHE_DIR": "/cache/alphafold3/triton",
        "FS2_AF3_XDG_CACHE_DIR": "/cache/alphafold3/xdg",
    }


def test_openfold3_uses_the_exact_live_localizations_and_shared_cache() -> None:
    profiles_document, execution_document = _documents()
    profile = next(item for item in profiles_document["profiles"] if item["model_id"] == "openfold3-openbind")
    execution = next(item for item in execution_document["models"] if item["model_id"] == "openfold3-openbind")

    expected = {
        "openfold3-openbind-0": {
            "content": "f954e2f2e3d0bdba297ac8009f6d590b3e2c28ca2985742c9bbd8167f276f6b5",
            "manifest": "8afc057f877ae42aecbaeb56c0be74987e920dcf016c3d7a12dc7ea2370df806",
            "receipt": "fd39c6ef471c575f0b797e5e7777c948d04151d59b31c5c32f4f0ede728a780c",
            "file": "of3-ob-2025-06-30-174k.pt",
            "file_sha256": "bd43301c011d5f87580d3e8b548658869433e4488399feb03035ba248f8e29e4",
            "size": 2287872989,
            "mount": "/models/openfold3",
        },
        "openfold3-components-bcif": {
            "content": "ff75f66793c11d7cb63531c758b210fa6fe33d5a39378bb0ab89094278e95e3b",
            "manifest": "1b5e78ab84e9cf8c3807554cd7cf6af15668046bc5031b7d234cd1330c7ba055",
            "receipt": "c8195ee346b3bffad1940c0ff600e52bb2e1ba3046e579627f3ad0329ee0fcc6",
            "file": "components.bcif",
            "file_sha256": "473d845c8b250b188dbed9bf505ae206692a178a2a7c4869bf8f9de707ffcc0c",
            "size": 63393643,
            "mount": "/databases/openfold3",
        },
    }
    profile_artifacts = {item["artifact_id"]: item for item in profile["runtime_artifacts"]}
    localizations = {item["artifact_id"]: item for item in execution["runtime_artifacts"]}
    assert set(profile_artifacts) == set(expected) == set(localizations)
    for artifact_id, values in expected.items():
        requirement = profile_artifacts[artifact_id]
        localization = localizations[artifact_id]
        assert requirement["content_identity"] == {
            "digest_sha256": values["content"],
            "size_bytes": values["size"],
        }
        assert requirement["readiness_manifest_sha256"] == values["manifest"]
        assert requirement["required_files"] == [values["file"]]
        assert requirement["file_manifest"] == [
            {"path": values["file"], "sha256": values["file_sha256"], "size_bytes": values["size"]}
        ]
        assert localization == {
            "artifact_id": artifact_id,
            "mount_path": values["mount"],
            "content_digest": "sha256:" + values["content"],
            "localization_receipt_digest": "sha256:" + values["receipt"],
            "file_manifest": requirement["file_manifest"],
        }

    inference = next(item for item in execution["stages"] if item["stage_id"] == "inference")
    reference_mounts = [item for item in inference["mounts"] if item["kind"] == "reference"]
    assert {item["host_path"] for item in reference_mounts} == {"/mnt/fs2-reference-data/data"}
    assert {item["mount_path"] for item in reference_mounts} == {
        "/models/openfold3",
        "/databases/openfold3",
    }
    assert all(item["sub_path"].startswith("model-artifacts/public/v1/objects/") for item in reference_mounts)
    assert inference["environment"] == {
        "TRITON_CACHE_DIR": "/cache/openfold3/triton",
        "TORCH_EXTENSIONS_DIR": "/cache/openfold3/torch-extensions",
        "XDG_CACHE_HOME": "/cache/openfold3/xdg",
    }


def test_protenix_uses_the_exact_composite_tree_receipt_and_cache() -> None:
    profiles_document, execution_document = _documents()
    profile = next(item for item in profiles_document["profiles"] if item["model_id"] == "protenix-v2")
    execution = next(item for item in execution_document["models"] if item["model_id"] == "protenix-v2")
    requirement = profile["runtime_artifacts"][0]
    localization = execution["runtime_artifacts"][0]
    prepare_profile = next(stage for stage in profile["workload"]["stages"] if stage["id"] == "prepare-data")

    assert prepare_profile["placement"] == {"class": "model-reference-data"}

    assert requirement["artifact_id"] == localization["artifact_id"] == "protenix-v2"
    assert requirement["content_identity"] == {
        "digest_sha256": "5e1c3b548af40752bb15f9f2ba06590e20e2b165e3fe9ab3fa99af9977574d48",
        "size_bytes": 2514897184,
    }
    assert requirement["readiness_manifest_sha256"] == (
        "a093d28ecfc8374f143cc32ff713b0e6ad1124c095dbbca5af6e51b4f7dcc6b7"
    )
    assert localization["content_digest"] == "sha256:" + requirement["content_identity"]["digest_sha256"]
    assert localization["localization_receipt_digest"] == (
        "sha256:ea301407e2ac427bf87d4617136629f178a75d55f67e3aea882de4bc19ce04b4"
    )
    assert localization["file_manifest"] == requirement["file_manifest"]
    assert {item["path"] for item in localization["file_manifest"]} == set(requirement["required_files"])
    assert sum(item["size_bytes"] for item in localization["file_manifest"]) == 2514897184

    expected_cache = {
        "TRITON_CACHE_DIR": "/cache/protenix/triton",
        "CUEQ_TRITON_CACHE_DIR": "/cache/protenix/cueq-triton",
        "TORCH_EXTENSIONS_DIR": "/cache/protenix/torch-extensions",
        "XDG_CACHE_HOME": "/cache/protenix/xdg",
    }
    for stage in execution["stages"]:
        assert stage["environment"] == expected_cache
        reference = next(item for item in stage["mounts"] if item["kind"] == "reference")
        assert reference == {
            "name": "protenix-v2",
            "kind": "reference",
            "claim_name": None,
            "host_path": "/mnt/fs2-reference-data/data",
            "mount_path": "/models/protenix-v2",
            "sub_path": (
                "scientific-localization/public/generations/protenix-v2/sha256/"
                "5e1c3b548af40752bb15f9f2ba06590e20e2b165e3fe9ab3fa99af9977574d48"
            ),
            "read_only": True,
        }
