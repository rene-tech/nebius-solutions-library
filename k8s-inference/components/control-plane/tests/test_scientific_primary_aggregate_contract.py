from __future__ import annotations

import json

import pytest
from conftest import CATALOG_ROOT

from fs2_serve.scientific_batch.execution import FileScientificManifestRenderer
from fs2_serve.scientific_batch.profile_catalog import ScientificProfileCatalog, ScientificProfileError


PRIMARY_CANDIDATES = {
    "proteina-complexa": {
        "digest": "sha256:f4e06b6025a74c924749420f2fce01fb9511aba606a2266c85a9d9e92e3679ca",
        "variant": "upstream-dev-20260827",
        "namespace": "fs2-models",
        "stages": ("generate", "filter", "evaluate", "analyze"),
        "artifacts": {
            "complexa-protein",
            "complexa-ligand",
            "complexa-ame",
            "rosettafold3-checkpoint",
            "alphafold2-params",
        },
    },
    "bindcraft": {
        "digest": "sha256:806760cde59f1eb47de2735cd6415e176277586e022bbfb33f8658221c3f672d",
        "variant": "v1-5-3-pyrosetta-academic",
        "namespace": "fs2-academic-poc",
        "stages": ("design", "aggregate"),
        "artifacts": {
            "alphafold2-params-bindcraft",
            "colabdesign-mpnn-weights-vanilla",
            "colabdesign-mpnn-weights-soluble",
            "bindcraft-pyrosetta-installed-tree",
        },
    },
    "mosaic": {
        "digest": "sha256:853cb34b36e940303c126e11e9e66c7643efa15c4ab48861c73013018e477a92",
        "variant": "mosaic-boltz2-proteinmpnn-v1",
        "namespace": "fs2-models",
        "stages": ("design", "aggregate"),
        "artifacts": {
            "mosaic-boltz2-conf",
            "boltzgen-inference-molecules",
            "mosaic-components",
        },
    },
    "rfdiffusion": {
        "digest": "sha256:f31902e0fbece8e7f823b36e47b79ec02fe0bc545a44131188f9194f13711f19",
        "variant": "rfdiffusion-v1-1-0",
        "namespace": "fs2-models",
        "stages": ("inference", "collect"),
        "artifacts": {"rfdiffusion-base-checkpoint"},
    },
}


def test_primary_candidates_are_serialized_but_remain_fail_closed() -> None:
    catalog = ScientificProfileCatalog.load(CATALOG_ROOT)
    profiles_document = json.loads(
        (CATALOG_ROOT / "contracts/scientific-workload-profiles.json").read_text(encoding="utf-8")
    )
    execution_document = json.loads(
        (CATALOG_ROOT / "contracts/scientific-execution-map.json").read_text(encoding="utf-8")
    )
    profiles = {item["model_id"]: item for item in profiles_document["profiles"]}
    executions = {item["model_id"]: item for item in execution_document["models"]}

    assert set(PRIMARY_CANDIDATES).issubset(profiles)
    assert set(PRIMARY_CANDIDATES).issubset(executions)
    assert {profile.model_id for profile in catalog.list()} == {"boltzgen"}

    for model_id, expected in PRIMARY_CANDIDATES.items():
        profile = profiles[model_id]
        identity = profile["execution_identity"]
        mcp = profile["interface"]["mcp"]
        assert profile["state"] == "candidate-unqualified"
        assert profile["route_exposed"] is False
        assert mcp["discoverable"] is True
        assert mcp["invocable"] is False
        assert "qualification" not in profile
        assert identity["runtime_image_digest"] == expected["digest"]
        assert identity["artifact_manifest_digest"] is None
        assert identity["execution_identity_sha256"] is None
        assert {item["artifact_id"] for item in profile["runtime_artifacts"]} == expected["artifacts"]
        with pytest.raises(ScientificProfileError, match="not runnable"):
            catalog.get(model_id)

        execution = executions[model_id]
        assert execution["variant_id"] == expected["variant"]
        assert execution["workload_namespace"] == expected["namespace"]
        assert execution["execution_identity_sha256"] is None
        assert tuple(stage["stage_id"] for stage in execution["stages"]) == expected["stages"]
        assert {item["artifact_id"] for item in execution["runtime_artifacts"]} == expected["artifacts"]
        profile_artifacts = {item["artifact_id"]: item for item in profile["runtime_artifacts"]}
        for localization in execution["runtime_artifacts"]:
            requirement = profile_artifacts[localization["artifact_id"]]
            assert localization["content_digest"] == "sha256:" + requirement["content_identity"]["digest_sha256"]
            if "aggregate_tree" in localization:
                assert localization["aggregate_tree"]["expanded_bytes"] == requirement["content_identity"][
                    "size_bytes"
                ]
                assert localization["aggregate_tree"]["manifest_sha256"] == requirement[
                    "readiness_manifest_sha256"
                ]
            if "file_manifest" in localization:
                assert localization["file_manifest"] == requirement["file_manifest"]
        for stage in execution["stages"]:
            assert stage["image"].endswith("@" + expected["digest"])
            assert stage["workspace_uid"] == 10001
            assert stage["workspace_gid"] == 10001
            assert stage["collector_id"]
            assert stage["validator_id"]
            assert stage["active_deadline_seconds"] > stage["termination_grace_seconds"] > 0
            assert set(stage["resources"]) == {"requests", "limits"}
            assert sum(mount["kind"] == "artifact-workspace" for mount in stage["mounts"]) == 1

    # Loading the complete map proves candidate entries are structurally valid,
    # but public dispatch still resolves only the one active profile above.
    renderer = FileScientificManifestRenderer(
        path=CATALOG_ROOT / "contracts/scientific-execution-map.json",
        profiles=catalog,
    )
    assert set(PRIMARY_CANDIDATES).issubset(renderer.variants)


def test_bindcraft_private_runtime_tree_is_exact_and_read_only() -> None:
    execution_document = json.loads(
        (CATALOG_ROOT / "contracts/scientific-execution-map.json").read_text(encoding="utf-8")
    )
    bindcraft = next(item for item in execution_document["models"] if item["model_id"] == "bindcraft")
    stages = {item["stage_id"]: item for item in bindcraft["stages"]}

    assert bindcraft["access_profile"] == "academic"
    af2 = next(item for item in bindcraft["runtime_artifacts"] if item["artifact_id"] == "alphafold2-params-bindcraft")
    assert af2["aggregate_tree"]["file_count"] == 17
    assert af2["aggregate_tree"]["expanded_bytes"] == 5_587_959_437
    assert af2["file_manifest"] == [
        {
            "path": "manifest.json",
            "sha256": "9d0b7e45378ed707cfc31585f3ae960282dc76f3e2c4f60b545b02dbc728423b",
            "size_bytes": 2866,
        }
    ]
    for stage in stages.values():
        assert stage["service_account_name"] == "fs2-academic-runner"
        pyrosetta = next(mount for mount in stage["mounts"] if mount["name"] == "pyrosetta")
        assert pyrosetta == {
            "name": "pyrosetta",
            "kind": "private",
            "claim_name": "academic-assets-runtime-rwx",
            "host_path": None,
            "mount_path": "/opt/fs2/academic/pyrosetta-bindcraft/site-packages",
            "sub_path": (
                "scientific-localization/private/generations/"
                "bindcraft-pyrosetta-installed-tree/sha256/"
                "a93d68e198c81cbb87926e012dff6b50a73e99d9a41261e65f73d264c792aa8d"
            ),
            "read_only": True,
        }
    assert {mount["name"] for mount in stages["design"]["mounts"]} >= {
        "mpnn-weights",
        "mpnn-soluble",
    }
    assert {mount["name"] for mount in stages["aggregate"]["mounts"]} == {
        "artifact-workspace",
        "alphafold2-params",
        "pyrosetta",
    }


def test_mosaic_design_uses_the_shared_persistent_jax_cache() -> None:
    execution_document = json.loads(
        (CATALOG_ROOT / "contracts/scientific-execution-map.json").read_text(encoding="utf-8")
    )
    mosaic = next(item for item in execution_document["models"] if item["model_id"] == "mosaic")
    stages = {item["stage_id"]: item for item in mosaic["stages"]}
    design = stages["design"]
    cache_mounts = [item for item in design["mounts"] if item["kind"] == "runtime-cache"]

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
    assert design["environment"] == {
        "JAX_COMPILATION_CACHE_DIR": "/cache/mosaic/jax",
        "JAX_PERSISTENT_CACHE_MIN_COMPILE_TIME_SECS": "0",
    }
    assert all(item["kind"] != "runtime-cache" for item in stages["aggregate"]["mounts"])
