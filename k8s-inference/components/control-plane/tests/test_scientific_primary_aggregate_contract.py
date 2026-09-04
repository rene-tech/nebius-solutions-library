from __future__ import annotations

import hashlib
import json

from conftest import CATALOG_ROOT
from jsonschema import Draft202012Validator, FormatChecker

SOLUTION_ROOT = CATALOG_ROOT.parents[1]
EXECUTION_MAP_SHA256 = "8f1e1cd972db55989c57cc5be6da971ad8e28dffd336c36571fcd7fbf601da3e"
PRIMARY_ACTIVE_BRIDGE = {
    "boltzgen": {
        "digest": "sha256:9c3230424e02d725dc145b8f21a18f283910e1beba1f37466598ee832813820e",
        "artifact_manifest_digest": "8305400f8711388cbc5d9937a120d7ca4de390170082047511e859f636a2dd66",
        "receipt": ("models/cancer-immunotherapy/runtime-images/boltzgen/evidence/h100-qualification-receipt.json"),
        "receipt_sha256": "f4a36ad7b8367e3383f584f96377313d39a26b5a08343d789311a41b04b79a46",
        "receipt_image_path": ("runtime", "image"),
        "qualified_at": "2026-09-03T07:34:56Z",
        "fragment": "models/cancer-immunotherapy/runtime-images/boltzgen/activation/fragment.json",
        "access": {
            "profile": "standard",
            "state": "not-required",
            "receipt_digest": None,
            "credentials_embedded": False,
        },
        "variant": "upstream-v0-3-2",
        "namespace": "fs2-models",
        "stages": (
            "configure",
            "design",
            "inverse-folding",
            "folding",
            "design-folding",
            "affinity",
            "analysis",
            "filtering",
        ),
        "artifacts": {"boltzgen-checkpoints", "boltzgen-inference-molecules"},
    },
    "proteina-complexa": {
        "digest": "sha256:f4e06b6025a74c924749420f2fce01fb9511aba606a2266c85a9d9e92e3679ca",
        "artifact_manifest_digest": "2d84dbda753a108e2e10f1d0aab1d9bc6dd5017f8f86ed94173d933a9884d4eb",
        "receipt": (
            "models/cancer-immunotherapy/runtime-images/proteina-complexa/evidence/h100-semantic-qualification.json"
        ),
        "receipt_sha256": "380441981a4170c8f2427a47ef4b25898f0ab14e33cc5efe7fdd65ebc1b45c4b",
        "receipt_image_path": ("image_digest",),
        "qualified_at": "2026-09-03T13:53:21Z",
        "fragment": ("models/cancer-immunotherapy/runtime-images/proteina-complexa/activation/fragment.json"),
        "access": {
            "profile": "standard",
            "state": "not-required",
            "receipt_digest": None,
            "credentials_embedded": False,
        },
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
        "artifact_manifest_digest": "a933ad20fd8c9e065c3ba77e96ff36c78303841b79a33d1b0308ec7d9873d641",
        "receipt": "models/cancer-immunotherapy/images/bindcraft-native/evidence/live-h100-qualification-20260903.json",
        "receipt_sha256": "162a1705d3f8b301cfb98426c3717fea63f9c9cb4c5c8ed4f21e2a60a1250944",
        "receipt_image_path": ("image", "index_digest"),
        "qualified_at": "2026-09-03T07:04:28Z",
        "fragment": "models/cancer-immunotherapy/images/bindcraft-native/activation/fragment.json",
        "access": {
            "profile": "academic",
            "state": "verified",
            "receipt_digest": "2b5a21f8eca6d8e465f29c508a6717915b84e73cb351d24811223a70228a3e36",
            "credentials_embedded": False,
        },
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
        "artifact_manifest_digest": "d8c039502375c5b67fafa1123743214fa774ca00cf52fa262f8bfa64fe22c641",
        "receipt": (
            "models/cancer-immunotherapy/runtime-images/mosaic/evidence/h100-v6-split-root-qualification-20260904.json"
        ),
        "receipt_sha256": "9eeef6c219c3ee0e9754e9d45be9de3f77ccec7824ce9e6b3f0fa17405aef339",
        "receipt_image_path": ("image", "requested_digest"),
        "qualified_at": "2026-09-04T15:25:31Z",
        "fragment": "models/cancer-immunotherapy/runtime-images/mosaic/activation/fragment.json",
        "access": {
            "profile": "standard",
            "state": "not-required",
            "receipt_digest": None,
            "credentials_embedded": False,
        },
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
        "artifact_manifest_digest": "931ae8f26343abe60d3fd4efb71da9ca2822899bde61357044eea09ca85bfc2d",
        "receipt": (
            "models/cancer-immunotherapy/runtime-images/rfdiffusion/"
            "evidence/h100-r13-split-root-qualification-20260904.json"
        ),
        "receipt_sha256": "1f392018020143dbc81695632cdfef02b9582f49e0187f72e6a172c0e8b80765",
        "receipt_image_path": ("image", "requested_digest"),
        "qualified_at": "2026-09-04T15:21:42Z",
        "fragment": "models/cancer-immunotherapy/runtime-images/rfdiffusion/activation/fragment.json",
        "access": {
            "profile": "standard",
            "state": "not-required",
            "receipt_digest": None,
            "credentials_embedded": False,
        },
        "variant": "rfdiffusion-v1-1-0",
        "namespace": "fs2-models",
        "stages": ("inference", "collect"),
        "artifacts": {"rfdiffusion-base-checkpoint"},
    },
}


def _canonical_sha256(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _nested(value: object, path: tuple[str, ...]) -> object:
    for key in path:
        assert isinstance(value, dict)
        value = value[key]
    return value


def test_primary_active_bridge_is_schema_valid_and_exactly_evidence_anchored() -> None:
    profiles_document = json.loads(
        (CATALOG_ROOT / "contracts/scientific-workload-profiles.json").read_text(encoding="utf-8")
    )
    execution_document = json.loads(
        (CATALOG_ROOT / "contracts/scientific-execution-map.json").read_text(encoding="utf-8")
    )
    profiles = {item["model_id"]: item for item in profiles_document["profiles"]}
    executions = {item["model_id"]: item for item in execution_document["models"]}
    profile_schema = json.loads(
        (CATALOG_ROOT / "schema/scientific-workload-profile.schema.json").read_text(encoding="utf-8")
    )
    profile_validator = Draft202012Validator(profile_schema, format_checker=FormatChecker())
    assert _canonical_sha256(execution_document) == EXECUTION_MAP_SHA256
    assert set(PRIMARY_ACTIVE_BRIDGE).issubset(profiles)
    assert set(PRIMARY_ACTIVE_BRIDGE).issubset(executions)

    for model_id, expected in PRIMARY_ACTIVE_BRIDGE.items():
        profile = profiles[model_id]
        execution = executions[model_id]
        fragment = json.loads((SOLUTION_ROOT / expected["fragment"]).read_text(encoding="utf-8"))
        projected_profile = fragment["profile_projection"]["profile"]
        artifact_manifest_digest = _canonical_sha256(execution["runtime_artifacts"])

        for candidate in (profile, projected_profile):
            profile_validator.validate(candidate)
            identity = candidate["execution_identity"]
            mcp = candidate["interface"]["mcp"]
            qualification = candidate["qualification"]
            assert candidate["state"] == "active"
            assert candidate["route_exposed"] is True
            assert candidate["source"]["classification"] == "qualified-input"
            assert candidate["access"] == expected["access"]
            assert mcp["discoverable"] is True
            assert mcp["invocable"] is True
            assert candidate["semantic_validation"]["state"] == "active"
            assert identity["runtime_image_digest"] == expected["digest"]
            assert artifact_manifest_digest == expected["artifact_manifest_digest"]
            assert identity["artifact_manifest_digest"] == artifact_manifest_digest
            identity_payload = dict(identity)
            recorded_identity = identity_payload.pop("execution_identity_sha256")
            assert recorded_identity == _canonical_sha256(identity_payload)
            assert qualification == {
                "h100_semantic_receipt_sha256": expected["receipt_sha256"],
                "public_completion_receipt_sha256": None,
                "scheduler_eligibility_receipt_sha256": None,
                "execution_map_sha256": EXECUTION_MAP_SHA256,
                "qualified_at": expected["qualified_at"],
            }

        receipt_path = SOLUTION_ROOT / expected["receipt"]
        receipt_bytes = receipt_path.read_bytes()
        receipt = json.loads(receipt_bytes)
        assert hashlib.sha256(receipt_bytes).hexdigest() == expected["receipt_sha256"]
        receipt_image = _nested(receipt, expected["receipt_image_path"])
        if model_id == "boltzgen":
            assert isinstance(receipt_image, str) and receipt_image.endswith("@" + expected["digest"])
            assert receipt["status"] == "passed"
        else:
            assert receipt_image == expected["digest"]
        if model_id == "boltzgen":
            pass
        elif model_id == "proteina-complexa":
            assert receipt["all_variants_passed"] is True
        elif model_id == "bindcraft":
            assert receipt["verdict"]["state"] == "passed"
        elif model_id == "mosaic":
            assert receipt["semantic_validation"]["status"] == "passed"
        else:
            assert {item["operation"] for item in receipt["runs"]} == {"design-backbone", "scaffold-motif"}
            assert all(item["status"] == "succeeded" and item["cuda_execution_confirmed"] for item in receipt["runs"])

        assert execution["variant_id"] == expected["variant"]
        assert execution["workload_namespace"] == expected["namespace"]
        assert execution["execution_identity_sha256"] == profile["execution_identity"]["execution_identity_sha256"]
        assert tuple(stage["stage_id"] for stage in execution["stages"]) == expected["stages"]
        assert {item["artifact_id"] for item in execution["runtime_artifacts"]} == expected["artifacts"]
        profile_artifacts = {item["artifact_id"]: item for item in profile["runtime_artifacts"]}
        for localization in execution["runtime_artifacts"]:
            requirement = profile_artifacts[localization["artifact_id"]]
            assert localization["content_digest"] == "sha256:" + requirement["content_identity"]["digest_sha256"]
            if "aggregate_tree" in localization:
                assert localization["aggregate_tree"]["expanded_bytes"] == requirement["content_identity"]["size_bytes"]
                assert localization["aggregate_tree"]["manifest_sha256"] == requirement["readiness_manifest_sha256"]
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
    assert "file_manifest" not in af2
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
