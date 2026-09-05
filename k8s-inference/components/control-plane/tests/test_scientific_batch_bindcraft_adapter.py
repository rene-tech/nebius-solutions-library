"""Adversarial tests for the native BindCraft/PyRosetta scientific adapter."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
from argparse import Namespace
from dataclasses import replace
from pathlib import Path
from types import ModuleType
from typing import Any
from uuid import UUID

import pytest

from fs2_serve.scientific_batch import ResourceClass, ScientificAdapterError, compile_adapter_run
from fs2_serve.scientific_batch.adapters import bindcraft
from fs2_serve.scientific_batch.adapters.materialization import safe_extract_tar
from fs2_serve.scientific_batch.adapters.staged_workspace import STAGE_COMPLETION_SCHEMA
from fs2_serve.scientific_batch.execution import FileScientificManifestRenderer, ScientificExecutionMapError
from fs2_serve.scientific_batch.models import (
    ArtifactAccessContext,
    RuntimeArtifactAggregateTree,
    RuntimeArtifactFile,
    RuntimeArtifactLocalization,
    RuntimeArtifactTreeKind,
    ScientificInputArtifact,
    StagePlacementClass,
)

SOLUTION_ROOT = Path(__file__).resolve().parents[3]
ADAPTER_ROOT = SOLUTION_ROOT / "models/structure/batch-adapters/bindcraft"
LOCALIZATION_CONTRACT = SOLUTION_ROOT / "catalog/runtime/contracts/scientific-artifact-localization.json"
PROFILE_PATH = SOLUTION_ROOT / "catalog/runtime/contracts/scientific-workload-profiles.json"
CURRENT_RUNTIME_SHA256 = "0f3841cab240c48c0aea9793b0df60222cc53f2637290873d1067321f8c0e227"
# This suite exercises the current source contract without claiming a registry
# or accelerator qualification. Publication identity is verified by the image
# package's source-bound receipt tests.
CONTRACT_TEST_IMAGE_DIGEST = "sha256:" + "d" * 64


def fixture(name: str) -> dict[str, Any]:
    value = json.loads((ADAPTER_ROOT / "fixtures" / f"{name}.json").read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


GRANTED_AUTHORIZATION = {
    "authorization_id": "fs2-cancer-immunotherapy-academic-poc-2026-09-02",
    "tenant_id": "tenant-academic",
    "use_authorization_status": "Granted",
    "execution_authorization_status": "Authorized",
}


def profile(*, authorization: dict[str, Any] | None = None) -> dict[str, Any]:
    """Build the catalog profile the adapter validates against.

    The BindCraft profile is not in the published set yet; that belongs to the
    runtime-onboarding task. `test_the_profile_matches_the_catalog_when_published`
    binds this shape to the real one as soon as it lands.
    """

    access: dict[str, Any] = {
        "profile": "academic",
        "state": "verified",
        "receipt_digest": None,
        "credentials_embedded": False,
        "request_time_license_receipt_required": False,
    }
    if authorization is not None:
        access["authorization"] = authorization
    return {
        "schema": "fs2-serve.nebius.ai/scientific-workload-profile/v1",
        "model_id": "bindcraft",
        "display_name": "BindCraft",
        "execution_mode": "scientific-batch",
        "state": "candidate-unqualified",
        "route_exposed": False,
        "source": {
            "kind": "git",
            "repository": bindcraft.SOURCE_REPOSITORY,
            "revision": bindcraft.SOURCE_REVISION,
            "review_url": f"https://github.com/{bindcraft.SOURCE_REPOSITORY}/tree/{bindcraft.SOURCE_REVISION}",
            "classification": "candidate-input",
        },
        "execution_identity": {
            "model_revision": bindcraft.SOURCE_REVISION,
            "runtime_image_digest": None,
            "runtime_recipe_sha256": "5" * 64,
            "workload_recipe_sha256": "6" * 64,
            "execution_map_revision": "7" * 64,
            "artifact_manifest_digest": None,
            "execution_identity_sha256": None,
        },
        "interface": {
            "protocol": "scientific-batch-v1",
            "submit_endpoint": "/v1/models/bindcraft:submit",
            "request_schema": "fs2-serve.nebius.ai/scientific-run-request/v1",
            "result_schema": "fs2-serve.nebius.ai/scientific-run-result/v1",
            "parameter_schema": bindcraft.PARAMETER_SCHEMA,
            "operations": ["design-binder"],
            "service_classes": ["customer-batch", "interactive"],
            "mcp": {"discoverable": True, "invocable": False, "tool_name": "submit_bindcraft", "description": "x"},
        },
        "access": access,
        "resources": {
            "gpu_count": 1,
            "gpu_topology": "single-gpu",
            "host_architectures": ["amd64"],
            "compatible_pool_ids": ["h100"],
            "required_node_labels": {},
        },
        "workload": {
            "stages": [
                {
                    "id": "design",
                    "needs": [],
                    "resource_class": "gpu",
                    "admission_mode": "independent-jobs",
                    "min_parallelism": 1,
                    "max_parallelism": bindcraft.MAX_DESIGN_SHARDS,
                    "checkpoint_mode": "restart",
                    "preemption_mode": "restartable",
                },
                {
                    "id": "aggregate",
                    "needs": ["design"],
                    "resource_class": "cpu",
                    "admission_mode": "independent-jobs",
                    "min_parallelism": 1,
                    "max_parallelism": 1,
                    "checkpoint_mode": "none",
                    "preemption_mode": "non_preemptible",
                    "placement": {"class": "reference-data"},
                    "resources": {
                        "cpu_millis": 2000,
                        "memory_bytes": 8 * 1024**3,
                        "ephemeral_storage_bytes": 16 * 1024**3,
                        "limits": {
                            "cpu_millis": 4000,
                            "memory_bytes": 16 * 1024**3,
                            "ephemeral_storage_bytes": 32 * 1024**3,
                        },
                    },
                },
            ],
            "retry": {"max_attempts": 2, "retryable_exit_codes": [137, 143]},
            "cancellation": {"mode": "terminate-attempt", "grace_seconds": 60},
        },
        "semantic_validation": {"validator_id": "bindcraft", "state": "candidate-unqualified"},
        "policy": {"commercial_use": "prohibited", "non_clinical": True, "limitations": ["academic only"]},
    }


def granted() -> dict[str, Any]:
    return profile(authorization=GRANTED_AUTHORIZATION)


def verified_target(**overrides: object) -> ScientificInputArtifact:
    values: dict[str, object] = {
        "logical_artifact_id": bindcraft.TARGET_INPUT_ID,
        "semantic_type": bindcraft.TARGET_SEMANTIC_TYPE,
        "artifact_id": UUID("30000000-0000-4000-8000-000000000001"),
        "digest": "sha256:" + "d" * 64,
        "size_bytes": 24_576,
        "media_type": bindcraft.TARGET_MEDIA_TYPE,
        "compression": "none",
    }
    values.update(overrides)
    return ScientificInputArtifact(**values)  # type: ignore[arg-type]


def compile_fixture(name: str, *, operation_id: str = "op-bindcraft-01") -> Any:
    return bindcraft.compile_run(
        granted(),
        fixture(name),
        operation_id=operation_id,
        input_artifacts=(verified_target(),),
    )


def contract_artifacts() -> dict[str, dict[str, Any]]:
    document = json.loads(LOCALIZATION_CONTRACT.read_text(encoding="utf-8"))
    return {item["artifact_id"]: item for item in document["artifacts"]}


def test_every_tree_binding_matches_the_merged_localization_contract() -> None:
    """The adapter names no tree identity the accepted contract does not.

    Archive provenance and extracted-tree inventory are separate digests, and
    the contract is the authority for both. This runs against the real merged
    contract, not a copy.
    """

    artifacts = contract_artifacts()
    for binding in bindcraft.tree_bindings():
        entry = artifacts[binding.artifact_id]
        assert binding.archive_sha256 == entry["archive"]["sha256"]
        assert binding.tree_inventory_sha256 == entry["tree"]["inventory_sha256"]
        assert binding.entry_count == entry["tree"]["entry_count"]
        assert binding.mount_path in entry["tree"]["mount_paths"]
        # The two identities must never collapse into one.
        assert binding.archive_sha256 != binding.tree_inventory_sha256


def test_the_four_trees_span_two_planes_not_one_claim() -> None:
    """Only the licensed tree is tenant-private; the other three are public."""

    artifacts = contract_artifacts()
    visibility = {
        binding.artifact_id: artifacts[binding.artifact_id].get("visibility", "public")
        for binding in bindcraft.tree_bindings()
    }
    assert visibility[bindcraft.PYROSETTA_ARTIFACT] == bindcraft.PYROSETTA_VISIBILITY == "tenant-private"
    assert visibility[bindcraft.AF2_ARTIFACT] == "public"
    assert visibility[bindcraft.MPNN_VANILLA_ARTIFACT] == "public"
    assert visibility[bindcraft.MPNN_SOLUBLE_ARTIFACT] == "public"
    # The two ColabDesign trees come from one archive and differ only as trees.
    vanilla, soluble = (
        next(item for item in bindcraft.tree_bindings() if item.artifact_id == artifact_id)
        for artifact_id in (bindcraft.MPNN_VANILLA_ARTIFACT, bindcraft.MPNN_SOLUBLE_ARTIFACT)
    )
    assert vanilla.archive_sha256 == soluble.archive_sha256
    assert vanilla.tree_inventory_sha256 != soluble.tree_inventory_sha256


def test_each_stage_binds_the_trees_it_actually_needs() -> None:
    """Design reads all four; aggregate carries what the outer gate demands.

    The wrapper admits trees only in run-trajectory, but the shared outer
    entrypoint verifies AlphaFold2 and binds PyRosetta on every non-smoke
    command before exec'ing it, so two of four is the floor for aggregate.
    """

    plan = compile_fixture("positive-default-lane")
    assert set(plan.required_model_artifacts) == set(bindcraft.DESIGN_RUNTIME_ARTIFACTS)
    aggregate = plan.invocation("aggregate", "main")
    assert set(aggregate.runtime_artifacts) == set(bindcraft.AGGREGATE_RUNTIME_ARTIFACTS)
    assert {item.artifact_id for item in aggregate.runtime_mounts} == set(bindcraft.AGGREGATE_RUNTIME_ARTIFACTS)
    aggregate_paths = {item.mount_path for item in aggregate.runtime_trees}
    assert aggregate_paths == {bindcraft.AF2_PATH, bindcraft.PYROSETTA_PATH}
    assert bindcraft.MPNN_VANILLA_PATH not in aggregate_paths
    for shard in plan.controller_plan.stages[0].shards:
        design = plan.invocation("design", shard)
        assert set(design.runtime_artifacts) == set(bindcraft.DESIGN_RUNTIME_ARTIFACTS)
        assert len(design.runtime_trees) == 4


def test_execution_verifier_accepts_the_exact_stage_specific_bindcraft_mount_sets() -> None:
    """The CPU aggregate must not be forced to carry unused ProteinMPNN trees."""

    plan = compile_fixture("positive-default-lane")
    af2 = RuntimeArtifactLocalization(
        logical_artifact_id=bindcraft.AF2_ARTIFACT,
        mount_path=bindcraft.AF2_PATH,
        content_digest="sha256:" + "1" * 64,
        files=(RuntimeArtifactFile(path="manifest.json", digest="sha256:" + "2" * 64, size_bytes=1),),
        localization_receipt_digest="sha256:" + "3" * 64,
    )
    pyrosetta_digest = "sha256:" + bindcraft.PYROSETTA_INVENTORY_SHA256
    pyrosetta = RuntimeArtifactLocalization(
        logical_artifact_id=bindcraft.PYROSETTA_ARTIFACT,
        mount_path=bindcraft.PYROSETTA_PATH,
        content_digest=pyrosetta_digest,
        files=(),
        localization_receipt_digest="sha256:" + "4" * 64,
        aggregate_tree=RuntimeArtifactAggregateTree(
            tree_digest=pyrosetta_digest,
            manifest_digest="sha256:" + "5" * 64,
            inventory_digest=pyrosetta_digest,
            file_count=8_697,
            directory_count=100,
            expanded_bytes=3_287_122_494,
            canonical_path=f"bindcraft/pyrosetta/sha256/{bindcraft.PYROSETTA_INVENTORY_SHA256}",
            storage_kind=RuntimeArtifactTreeKind.LOCALIZATION_GENERATION,
            manifest_algorithm="fs2-tree-inventory/v2",
            marker_relative_path=".fs2-runtime-tree.json",
        ),
    )

    FileScientificManifestRenderer._verify_bindcraft_runtime(plan, (af2, pyrosetta))


def test_execution_verifier_accepts_the_published_bindcraft_af2_aggregate_tree() -> None:
    """The immutable 17-file inventory proves manifest.json without enumeration."""

    plan = compile_fixture("positive-default-lane")
    af2_digest = "sha256:" + bindcraft.AF2_INVENTORY_SHA256
    af2_tree = RuntimeArtifactAggregateTree(
        tree_digest=af2_digest,
        manifest_digest="sha256:25cad364aa28e5cf282a877d123ad938ea048a957ad8185307b5542c301406e0",
        inventory_digest=af2_digest,
        file_count=17,
        directory_count=0,
        expanded_bytes=5_587_959_437,
        canonical_path=f"bindcraft/alphafold2/sha256/{bindcraft.AF2_INVENTORY_SHA256}",
        storage_kind=RuntimeArtifactTreeKind.LOCALIZATION_GENERATION,
        manifest_algorithm="fs2-flat-tree-inventory/v1",
        marker_relative_path=".fs2-runtime-tree.json",
    )
    af2 = RuntimeArtifactLocalization(
        logical_artifact_id=bindcraft.AF2_ARTIFACT,
        mount_path=bindcraft.AF2_PATH,
        content_digest=af2_digest,
        files=(),
        localization_receipt_digest="sha256:" + "3" * 64,
        aggregate_tree=af2_tree,
    )
    pyrosetta_digest = "sha256:" + bindcraft.PYROSETTA_INVENTORY_SHA256
    pyrosetta = RuntimeArtifactLocalization(
        logical_artifact_id=bindcraft.PYROSETTA_ARTIFACT,
        mount_path=bindcraft.PYROSETTA_PATH,
        content_digest=pyrosetta_digest,
        files=(),
        localization_receipt_digest="sha256:" + "4" * 64,
        aggregate_tree=RuntimeArtifactAggregateTree(
            tree_digest=pyrosetta_digest,
            manifest_digest="sha256:" + "5" * 64,
            inventory_digest=pyrosetta_digest,
            file_count=8_697,
            directory_count=779,
            expanded_bytes=3_287_122_494,
            canonical_path=f"bindcraft/pyrosetta/sha256/{bindcraft.PYROSETTA_INVENTORY_SHA256}",
            storage_kind=RuntimeArtifactTreeKind.LOCALIZATION_GENERATION,
            manifest_algorithm="fs2-tree-inventory/v2",
            marker_relative_path=".fs2-runtime-tree.json",
        ),
    )

    FileScientificManifestRenderer._verify_bindcraft_runtime(plan, (af2, pyrosetta))

    wrong_marker = replace(af2, aggregate_tree=replace(af2_tree, manifest_digest="sha256:" + "6" * 64))
    with pytest.raises(ScientificExecutionMapError, match="require manifest.json"):
        FileScientificManifestRenderer._verify_bindcraft_runtime(plan, (wrong_marker, pyrosetta))


def test_every_bound_tree_is_reachable_through_the_plan() -> None:
    """A localized tree nobody names is a mount the model cannot find."""

    plan = compile_fixture("positive-default-lane")
    for binding in bindcraft.tree_bindings():
        assert any(item.names_tree_path(binding) for item in plan.invocations), binding.artifact_id
    environment = dict(plan.invocation("design", "design-000").environment)
    # PYTHONPATH is a concatenation, so each tree also gets an exact-value name.
    assert environment["FS2_ARTIFACT_ROOT"] == bindcraft.AF2_PATH
    assert environment["FS2_BINDCRAFT_PYROSETTA_TREE"] == bindcraft.PYROSETTA_PATH
    assert environment["PYTHONPATH"].split(":", 1)[0] == bindcraft.PYROSETTA_PATH


def test_stage_argv_is_shell_free_and_uses_the_reviewed_outer_entrypoint() -> None:
    plan = compile_fixture("positive-default-lane")
    for invocation in plan.invocations:
        argv = invocation.argv
        assert argv[:3] == ("python", f"{invocation.working_directory}/.fs2/stage-runner.py", "--")
        assert argv[3:5] == ("python", "/opt/fs2/runtime_entrypoint.py")
        assert argv[5] == "/opt/fs2/bin/bindcraft-batch"
        assert argv[6] in {"run-trajectory", "aggregate"}
        assert argv[0] not in {"sh", "bash", "/bin/sh", "/bin/bash"}
        assert not any(token in {"-c", ";", "&&", "|"} or "$(" in token for token in argv)
        assert "--runtime-localization-marker" in argv
    design = plan.invocation("design", "design-000")
    assert design.argv[design.argv.index("--settings-sha256") + 1] == bindcraft.SETTINGS_SHA256
    assert design.argv[design.argv.index("--filters-sha256") + 1] == bindcraft.FILTERS_SHA256
    assert "--pyrosetta-required" in design.argv
    aggregate = plan.invocation("aggregate", "main")
    assert aggregate.argv[aggregate.argv.index("--expected-shards") + 1] == "4"
    assert "--atomic-rename" in aggregate.argv


def test_each_invocation_carries_its_exact_native_request_and_input_manifest() -> None:
    request = fixture("positive-default-lane")
    request["parameters"]["designs"] = 33
    target = verified_target()
    plan = bindcraft.compile_run(
        granted(),
        request,
        operation_id="op-bindcraft-native-documents",
        input_artifacts=(target,),
    )
    public, parameters = bindcraft._request(request)
    expected_manifest = bindcraft.native_input_manifest(target)
    expected_manifest_bytes = bindcraft._canonical_json(expected_manifest).encode()

    for index, shard_id in enumerate(parameters.shard_ids):
        invocation = plan.invocation("design", shard_id)
        environment = dict(invocation.environment)
        native = json.loads(environment["FS2_BINDCRAFT_REQUEST_JSON"])
        assert native == bindcraft.native_request(public, parameters, expected_manifest, shard_index=index)
        assert native["parameters"]["accepted_designs_per_shard"] == parameters.accepted_designs(index)
        assert native["parameters"]["max_trajectories_per_shard"] == parameters.max_trajectories(index)
        assert native["parameters"]["target_chains"] == ["A"]
        assert native["parameters"]["hotspots"] == [{"chain": "A", "residue": residue} for residue in (56, 66, 115)]
        assert environment["FS2_BINDCRAFT_INPUT_MANIFEST_JSON"].encode() == expected_manifest_bytes
        assert native["input_manifest"]["sha256"] == hashlib.sha256(expected_manifest_bytes).hexdigest()
        assert environment["FS2_BINDCRAFT_TARGET_PDB"].endswith("/inputs/target_structure.pdb")
        assert environment["FS2_BINDCRAFT_EXTERNAL_TREES"].endswith("/.fs2/external-trees.json")
        roles = json.loads(environment["FS2_BINDCRAFT_EXTERNAL_TREE_ROLES"])
        assert {item["role"] for item in roles["trees"]} == set(bindcraft.EXTERNAL_TREE_ROLES)
        assert environment["FS2_SCIENTIFIC_COLLECTOR_ID"] == bindcraft.DESIGN_COLLECTOR_ID

    aggregate = dict(plan.invocation("aggregate", "main").environment)
    assert json.loads(aggregate["FS2_BINDCRAFT_REQUEST_JSON"]) == bindcraft.native_request(
        public,
        parameters,
        expected_manifest,
        shard_index=0,
    )
    assert "FS2_BINDCRAFT_TARGET_PDB" not in aggregate
    assert aggregate["FS2_SCIENTIFIC_COLLECTOR_ID"] == bindcraft.AGGREGATE_COLLECTOR_ID


def test_target_must_come_from_one_verified_manifest_entry() -> None:
    direct = fixture("positive-default-lane")
    direct["input_manifest"]["media_type"] = bindcraft.TARGET_MEDIA_TYPE
    with pytest.raises(ScientificAdapterError, match="scientific manifest"):
        bindcraft.compile_run(
            granted(),
            direct,
            operation_id="op-bindcraft-direct-pdb",
            input_artifacts=(verified_target(),),
        )

    compressed_manifest = fixture("positive-default-lane")
    compressed_manifest["input_manifest"]["compression"] = "zstd"
    with pytest.raises(ScientificAdapterError, match="must not be compressed"):
        bindcraft.compile_run(
            granted(),
            compressed_manifest,
            operation_id="op-bindcraft-compressed-manifest",
            input_artifacts=(verified_target(),),
        )

    with pytest.raises(ScientificAdapterError, match="exactly one verified"):
        bindcraft.compile_run(
            granted(),
            fixture("positive-default-lane"),
            operation_id="op-bindcraft-missing-entry",
            input_artifacts=(),
        )
    with pytest.raises(ScientificAdapterError, match="exactly one verified"):
        bindcraft.compile_run(
            granted(),
            fixture("positive-default-lane"),
            operation_id="op-bindcraft-extra-entry",
            input_artifacts=(verified_target(), verified_target(artifact_id=UUID(int=2))),
        )

    for override, expected in (
        ({"logical_artifact_id": "wrong_target"}, "exactly one verified"),
        ({"semantic_type": "protein-sequence-fasta/v1"}, "semantic type"),
        ({"media_type": "application/json"}, "media or compression"),
        ({"compression": "zstd"}, "media or compression"),
    ):
        with pytest.raises(ScientificAdapterError, match=expected):
            bindcraft.compile_run(
                granted(),
                fixture("positive-default-lane"),
                operation_id="op-bindcraft-invalid-entry",
                input_artifacts=(verified_target(**override),),
            )


def test_verified_target_is_the_only_materialization_source() -> None:
    request = fixture("positive-default-lane")
    target = verified_target(
        artifact_id=UUID("30000000-0000-4000-8000-000000000099"),
        digest="sha256:" + "9" * 64,
        size_bytes=74_614,
    )
    plan = bindcraft.compile_run(
        granted(),
        request,
        operation_id="op-bindcraft-verified-target",
        input_artifacts=(target,),
    )
    design = plan.invocation("design", "design-000")
    assert design.consumes == (bindcraft.TARGET_INPUT_ID,)
    assert design.materializations[0].artifact_id == bindcraft.TARGET_INPUT_ID
    assert request["input_manifest"]["artifact_id"] not in design.consumes
    manifest = json.loads(dict(design.environment)["FS2_BINDCRAFT_INPUT_MANIFEST_JSON"])
    pointer = manifest["entries"][0]["artifact"]
    assert pointer == {
        "artifact_id": str(target.artifact_id),
        "sha256": target.digest.removeprefix("sha256:"),
        "size_bytes": target.size_bytes,
        "media_type": bindcraft.TARGET_MEDIA_TYPE,
        "compression": "none",
    }


def test_runtime_document_materialization_is_idempotent_and_refuses_drift(tmp_path: Path) -> None:
    invocation = compile_fixture("positive-default-lane").invocation("design", "design-000")
    workspace = tmp_path / "attempt"
    workspace.mkdir()
    first = bindcraft.materialize_runtime_documents(invocation, workspace)
    assert bindcraft.materialize_runtime_documents(invocation, workspace) == first
    first[0].chmod(0o640)
    first[0].write_text("{}\n", encoding="ascii")
    with pytest.raises(ScientificAdapterError, match="different bytes"):
        bindcraft.materialize_runtime_documents(invocation, workspace)


def test_the_default_mpnn_lane_is_vanilla_and_soluble_is_never_implicit() -> None:
    assert bindcraft.DEFAULT_MPNN_LANE == "vanilla"
    assert "mpnn_lane" not in fixture("positive-default-lane")["parameters"]
    plan = compile_fixture("positive-default-lane")
    for invocation in plan.invocations:
        assert dict(invocation.environment)["FS2_BINDCRAFT_MPNN_WEIGHTS"] == "original"
    soluble = compile_fixture("positive-soluble-lane")
    for invocation in soluble.invocations:
        assert dict(invocation.environment)["FS2_BINDCRAFT_MPNN_WEIGHTS"] == "soluble"
    # Both trees stay mounted whichever lane is chosen.
    for plan_under_test in (plan, soluble):
        design = plan_under_test.invocation("design", "design-000")
        paths = {item.mount_path for item in design.runtime_trees}
        assert {bindcraft.MPNN_VANILLA_PATH, bindcraft.MPNN_SOLUBLE_PATH} <= paths

    request = fixture("positive-soluble-lane")
    request["parameters"]["mpnn_lane"] = "SOLUBLE"
    with pytest.raises(ScientificAdapterError, match="mpnn_lane"):
        bindcraft.compile_run(granted(), request, operation_id="op-bindcraft-01", input_artifacts=(verified_target(),))


def test_the_adapter_refuses_what_the_native_schema_cannot_express() -> None:
    cases = (
        ({"target": {"chain": "AB", "hotspot_residues": [56]}}, "one alphanumeric character"),
        ({"target": {"chain": "A", "hotspot_residues": [10000]}}, "hotspot residue"),
        ({"binder_length": {"minimum": 39, "maximum": 90}}, "binder_length.minimum"),
        ({"binder_length": {"minimum": 55, "maximum": 201}}, "binder_length.maximum"),
        ({"seed": 2**31}, "seed"),
    )
    for override, expected in cases:
        request = fixture("positive-default-lane")
        request["parameters"].update(override)
        with pytest.raises(ScientificAdapterError, match=expected):
            bindcraft.compile_run(
                granted(), request, operation_id="op-bindcraft-01", input_artifacts=(verified_target(),)
            )


def test_every_requested_design_is_assigned_to_exactly_one_shard() -> None:
    for designs in (1, 2, 31, 32, 33, 100, 319, 320):
        request = fixture("positive-default-lane")
        request["parameters"]["designs"] = designs
        plan = bindcraft.compile_run(
            granted(), request, operation_id="op-bindcraft-01", input_artifacts=(verified_target(),)
        )
        shards = plan.controller_plan.stages[0].shards
        assert len(shards) == min(designs, bindcraft.MAX_DESIGN_SHARDS)
        quotas = [
            int(dict(plan.invocation("design", shard).environment)["FS2_BINDCRAFT_ACCEPTED_DESIGNS"])
            for shard in shards
        ]
        assert sum(quotas) == designs, f"{designs} designs lost {designs - sum(quotas)}"
        assert min(quotas) >= 1
        assert max(quotas) <= bindcraft.MAX_DESIGNS_PER_SHARD


def test_a_request_above_the_executable_design_ceiling_fails_closed() -> None:
    assert bindcraft.EXECUTABLE_DESIGN_CEILING == 320
    for designs in (321, 641, 1024):
        request = fixture("positive-default-lane")
        request["parameters"]["designs"] = designs
        with pytest.raises(ScientificAdapterError, match="at most 320 designs"):
            bindcraft.compile_run(
                granted(), request, operation_id="op-bindcraft-01", input_artifacts=(verified_target(),)
            )


def test_admission_is_deployment_bound_and_takes_no_request_receipt() -> None:
    payload = json.dumps(fixture("positive-default-lane"))
    assert "receipt" not in payload and "licen" not in payload
    assert compile_fixture("positive-default-lane").model_id == "bindcraft"

    for authorization in (
        None,
        {**GRANTED_AUTHORIZATION, "use_authorization_status": "Missing"},
        {**GRANTED_AUTHORIZATION, "execution_authorization_status": "NotAuthorized"},
    ):
        with pytest.raises(ScientificAdapterError, match="authoriz"):
            bindcraft.compile_run(
                profile(authorization=authorization),
                fixture("positive-default-lane"),
                operation_id="op-1",
                input_artifacts=(verified_target(),),
            )
    receipt_required = granted()
    receipt_required["access"]["request_time_license_receipt_required"] = True
    with pytest.raises(ScientificAdapterError, match="per-request licence receipt"):
        bindcraft.compile_run(
            receipt_required,
            fixture("positive-default-lane"),
            operation_id="op-1",
            input_artifacts=(verified_target(),),
        )

    with pytest.raises(ScientificAdapterError, match="unknown \\['license_receipt'\\]"):
        bindcraft.compile_run(
            granted(),
            fixture("negative-caller-execution-fields"),
            operation_id="op-1",
            input_artifacts=(verified_target(),),
        )


def test_the_stage_topology_is_a_gpu_fanout_and_one_cpu_aggregate() -> None:
    plan = compile_fixture("positive-default-lane")
    stages = plan.controller_plan.stages
    assert tuple(stage.stage_id for stage in stages) == ("design", "aggregate")
    assert stages[0].resource_class is ResourceClass.GPU
    assert stages[1].resource_class is ResourceClass.CPU
    assert stages[1].placement_class is StagePlacementClass.REFERENCE_DATA_CPU
    assert stages[1].depends_on == ("design",)
    assert stages[1].shards == ("main",)


def test_dispatch_reaches_the_adapter_through_the_public_allow_list() -> None:
    access_context = ArtifactAccessContext(
        profile="academic",
        receipt_digest="sha256:" + "a" * 64,
        tenant_id="tenant-academic",
    )
    routed_profile = granted()
    routed_profile["access"]["receipt_digest"] = "a" * 64
    plan = compile_adapter_run(
        "bindcraft",
        routed_profile,
        fixture("positive-default-lane"),
        operation_id="op-bindcraft-01",
        variant_id=bindcraft.VARIANT_ID,
        access_context=access_context,
        input_artifacts=(verified_target(),),
    )
    assert plan.model_id == "bindcraft"
    assert plan.variant_id == "v1-5-3-pyrosetta-academic"
    with pytest.raises(ScientificAdapterError, match="variant_id"):
        compile_adapter_run(
            "bindcraft",
            granted(),
            fixture("positive-default-lane"),
            operation_id="op-bindcraft-01",
            variant_id="upstream-pyrosetta",
            access_context=access_context,
            input_artifacts=(verified_target(),),
        )


def test_public_catalog_profile_uses_the_deployment_access_handoff() -> None:
    published = json.loads(PROFILE_PATH.read_text(encoding="utf-8"))
    catalog_profile = next(item for item in published["profiles"] if item["model_id"] == "bindcraft")
    receipt_digest = catalog_profile["access"]["receipt_digest"]
    access_context = ArtifactAccessContext(
        profile="academic",
        receipt_digest=f"sha256:{receipt_digest}",
        tenant_id="tenant-academic",
    )

    plan = compile_adapter_run(
        "bindcraft",
        catalog_profile,
        fixture("positive-default-lane"),
        operation_id="op-bindcraft-public-profile",
        variant_id=bindcraft.VARIANT_ID,
        access_context=access_context,
        input_artifacts=(verified_target(),),
    )
    assert plan.model_id == "bindcraft"

    with pytest.raises(ScientificAdapterError, match="authorization is absent or mismatched"):
        compile_adapter_run(
            "bindcraft",
            catalog_profile,
            fixture("positive-default-lane"),
            operation_id="op-bindcraft-public-profile-mismatch",
            variant_id=bindcraft.VARIANT_ID,
            access_context=ArtifactAccessContext(
                profile="academic",
                receipt_digest="sha256:" + "0" * 64,
                tenant_id="tenant-academic",
            ),
            input_artifacts=(verified_target(),),
        )


def _pdb(*, complex_structure: bool) -> str:
    chains = ("A", "A", "B", "B") if complex_structure else ("B", "B", "B", "B")
    return "\n".join(
        [
            (
                f"ATOM  {index:5d}  CA  ALA {chain}{index:4d}    "
                f"{index:8.3f}{index + 1:8.3f}{index + 2:8.3f}  1.00 20.00           C"
            )
            for index, chain in enumerate(chains, start=1)
        ]
        + ["TER", "END", ""]
    )


def _pointer(path: Path, artifact_id: str, media_type: str) -> dict[str, Any]:
    content = path.read_bytes()
    return {
        "artifact_id": artifact_id,
        "sha256": hashlib.sha256(content).hexdigest(),
        "size_bytes": len(content),
        "media_type": media_type,
        "compression": "none",
    }


def publish_stage_completion(invocation: Any, workspace: Path) -> str:
    """Materialize controller documents and the runner-owned terminal marker."""

    metadata = workspace / ".fs2"
    metadata.mkdir(parents=True, exist_ok=True)
    for document in invocation.workspace_documents:
        destination = workspace.joinpath(*Path(document.relative_path).parts)
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            assert destination.read_text(encoding="utf-8") == document.canonical_json
        else:
            destination.write_text(document.canonical_json, encoding="utf-8")
    argv = invocation.argv[3:]
    payload = json.dumps(
        {
            "schema": STAGE_COMPLETION_SCHEMA,
            "status": "passed",
            "stage_id": invocation.stage_id,
            "shard_id": invocation.shard_id,
            "logical_output_id": invocation.produces,
            "collector_id": invocation.collector_id,
            "validator_id": invocation.validator_id,
            "argv_sha256": hashlib.sha256(
                json.dumps(argv, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
            ).hexdigest(),
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    (metadata / "stage-complete.json").write_bytes(payload)
    return hashlib.sha256(payload).hexdigest()


def publish_shard(
    workspace: Path,
    *,
    index: int,
    request: dict[str, Any] | None = None,
    designs: int | None = None,
    sequence: str | None = None,
    scoring_engine: str = "pyrosetta",
    backend_id: str = bindcraft.BACKEND_ID,
    source_revision: str = bindcraft.SOURCE_REVISION,
    status: str = "succeeded",
    generation: str = "generation-test",
    interface_residue_count: int | float = 3,
    contacted_hotspot_position: int | None = 0,
) -> None:
    request = request or fixture("positive-default-lane")
    parameters = bindcraft.BindCraftParameters.parse(request["parameters"])
    accepted = parameters.accepted_designs(index) if designs is None else designs
    output = workspace / "output"
    artifacts = output / "artifacts"
    artifacts.mkdir(parents=True, exist_ok=True)
    shard_path = artifacts / "shard.json"
    shard_path.write_text(
        json.dumps(
            {
                "backend_id": backend_id,
                "source_revision": source_revision,
                "index": index,
                "seed": parameters.seed(index),
                "status": status,
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    shard_id = f"artifact.bindcraft.native.shard.{index:03d}"
    shard_entry = {
        "name": f"shard-{index:03d}",
        "semantic_type": "bindcraft-native-shard-result-json/v1",
        "artifact": _pointer(shard_path, shard_id, "application/json"),
    }
    body = sequence or "AAAGGGCCCWWWYYYFFFLLLVVVIIIMMMTTTSSSNNNQQQHHHKKKRRRDDDEEEPPPAAAGG"
    entries: list[dict[str, Any]] = []
    paths = {shard_id: str(shard_path.relative_to(output))}
    requested_contacts = [
        {
            "chain": parameters.target_chain,
            "residue": residue,
            "closest_binder_atom_angstrom": 3.0 + position / 100,
            "in_contact": position == contacted_hotspot_position,
        }
        for position, residue in enumerate(parameters.hotspot_residues)
    ]
    for ordinal in range(accepted):
        metrics_path = artifacts / f"candidate-{ordinal:03d}-metrics.json"
        structure_path = artifacts / f"candidate-{ordinal:03d}.pdb"
        relaxed_path = artifacts / f"candidate-{ordinal:03d}-relaxed-complex.pdb"
        metrics_path.write_text(
            json.dumps(
                {
                    "candidate_id": f"native-s{index:03d}-c{ordinal:03d}",
                    "shard_index": index,
                    "sequence": body,
                    "scoring_engine": scoring_engine,
                    "filter_set_sha256": bindcraft.FILTERS_SHA256,
                    "iptm": 0.82,
                    "interface_residue_count": interface_residue_count,
                    "buried_interface_area": 750.0,
                    "binder_energy_score": -42.0,
                    "hotspot_geometry": {
                        "contact_cutoff_angstrom": 4.0,
                        "requested": requested_contacts,
                        "contacted": 0 if contacted_hotspot_position is None else 1,
                    },
                },
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        structure_path.write_text(_pdb(complex_structure=False), encoding="ascii")
        relaxed_path.write_text(_pdb(complex_structure=True), encoding="ascii")
        specifications = (
            (
                f"candidate-{ordinal:03d}-metrics",
                "bindcraft-native-design-metrics-json/v1",
                metrics_path,
                f"artifact.bindcraft.native.s{index:03d}.c{ordinal:03d}.metrics",
                "application/json",
            ),
            (
                f"candidate-{ordinal:03d}-structure",
                "protein-structure-pdb/v1",
                structure_path,
                f"artifact.bindcraft.native.s{index:03d}.c{ordinal:03d}.pdb",
                "chemical/x-pdb",
            ),
            (
                f"candidate-{ordinal:03d}-relaxed-complex",
                "bindcraft-native-relaxed-complex-pdb/v1",
                relaxed_path,
                f"artifact.bindcraft.native.s{index:03d}.c{ordinal:03d}.relaxed-complex",
                "chemical/x-pdb",
            ),
        )
        for name, semantic_type, path, artifact_id, media_type in specifications:
            entries.append(
                {
                    "name": name,
                    "semantic_type": semantic_type,
                    "artifact": _pointer(path, artifact_id, media_type),
                }
            )
            paths[artifact_id] = str(path.relative_to(output))
    (output / "shard-output.json").write_text(
        json.dumps(
            {
                "schema": "fs2-serve.nebius.ai/bindcraft-native-shard-output/v1",
                "shard": shard_entry,
                "candidates": entries,
                "artifact_paths": paths,
                "pyrosetta": {
                    "version": bindcraft.PYROSETTA_EXPECTED_VERSION,
                    "tree_manifest_sha256": bindcraft.PYROSETTA_INVENTORY_SHA256,
                },
                "external_trees": {
                    "schema": "fs2.nebius.ai/bindcraft-external-tree-admission/v1",
                    "localization_generation": generation,
                    "trees": {
                        role: {
                            "artifact_id": artifact_id,
                            "root": root,
                            (
                                "tree_manifest_sha256" if role == "pyrosetta-site-packages" else "inventory_sha256"
                            ): digest,
                        }
                        for role, (artifact_id, root, digest) in bindcraft.EXTERNAL_TREE_ROLES.items()
                    },
                },
                "runtime_localization_marker": {"generation": generation},
                "resolved_settings": {},
                "trajectories_recorded": accepted,
                "timings": {},
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )


def test_design_collector_publishes_one_deterministic_bounded_overlay_bundle(tmp_path: Path) -> None:
    """The artifact service receives exactly the directory aggregate consumes."""

    request = fixture("positive-default-lane")
    request["parameters"]["designs"] = 33
    workspace = tmp_path / "design-000"
    (workspace / "output").mkdir(parents=True)
    with pytest.raises(ScientificAdapterError, match="has not been published"):
        bindcraft.collect_output(request, workspace)

    publish_shard(workspace, index=0, request=request)
    first = bindcraft.collect_design_output(request, workspace)
    second = bindcraft.collect_stage_output(bindcraft.DESIGN_COLLECTOR_ID, request, workspace)
    assert first == second
    assert len(first.manifest["entries"]) == 1  # type: ignore[arg-type]
    entry = first.manifest["entries"][0]  # type: ignore[index]
    pointer = entry["artifact"]
    assert entry["semantic_type"] == "bindcraft-native-shard-bundle-tar/v1"
    assert pointer["compression"] == "zstd"
    content = first.blobs[pointer["artifact_id"]]
    assert pointer["sha256"] == hashlib.sha256(content).hexdigest()

    destination = tmp_path / "overlay"
    extracted = safe_extract_tar(content, destination, compression="zstd")
    relatives = {path.relative_to(destination).as_posix() for path in extracted}
    assert relatives == {
        "shard-output.json",
        "artifacts/shard.json",
        "artifacts/candidate-000-metrics.json",
        "artifacts/candidate-000.pdb",
        "artifacts/candidate-000-relaxed-complex.pdb",
        "artifacts/candidate-001-metrics.json",
        "artifacts/candidate-001.pdb",
        "artifacts/candidate-001-relaxed-complex.pdb",
    }


def test_design_collector_keeps_oversupply_as_a_runtime_contract_failure(tmp_path: Path) -> None:
    request = fixture("positive-default-lane")
    workspace = tmp_path / "design-000"
    publish_shard(workspace, index=0, request=request, designs=2)

    with pytest.raises(ScientificAdapterError, match="exact assigned candidate quota"):
        bindcraft.collect_design_output(request, workspace)


def test_design_companion_binds_completion_request_and_exact_handoff(tmp_path: Path) -> None:
    request = fixture("positive-default-lane")
    request["parameters"]["designs"] = 2
    invocation = bindcraft.compile_run(
        granted(),
        request,
        operation_id="op-bindcraft-companion",
        input_artifacts=(verified_target(),),
    ).invocation("design", "design-001")
    workspace = tmp_path / "design-001"
    publish_shard(workspace, index=1, request=request)
    completion_sha256 = publish_stage_completion(invocation, workspace)

    collected = bindcraft.collect_companion_output(invocation, workspace)

    assert tuple(item.name for item in collected.artifacts) == (invocation.handoff_name,)
    assert collected.artifacts[0].semantic_type == "bindcraft-native-shard-bundle-tar/v1"
    assert collected.artifacts[0].compression == "zstd"
    assert collected.validation["completion_marker_sha256"] == completion_sha256
    assert collected.validation["shard_id"] == "design-001"
    assert collected.validation["status"] == "passed"


def test_design_collector_accepts_a_fractional_cross_model_interface_average(tmp_path: Path) -> None:
    request = fixture("positive-default-lane")
    fractional = tmp_path / "fractional-average"
    publish_shard(fractional, index=0, request=request, interface_residue_count=7.4)
    assert bindcraft.collect_design_output(request, fractional).manifest["manifest_id"] == (
        "bindcraft.shard.000.handoff"
    )

    empty = tmp_path / "empty-average"
    publish_shard(empty, index=0, request=request, interface_residue_count=0)
    with pytest.raises(ScientificAdapterError, match="no interface residues"):
        bindcraft.collect_design_output(request, empty)


def test_design_collector_requires_atomic_contact_with_the_named_hotspot_face(tmp_path: Path) -> None:
    request = fixture("positive-soluble-lane")
    request["parameters"]["designs"] = 1

    later_face_residue = tmp_path / "later-face-residue"
    publish_shard(
        later_face_residue,
        index=0,
        request=request,
        contacted_hotspot_position=2,
    )
    assert bindcraft.collect_design_output(request, later_face_residue).manifest["manifest_id"] == (
        "bindcraft.shard.000.handoff"
    )

    no_atomic_contact = tmp_path / "no-atomic-contact"
    publish_shard(
        no_atomic_contact,
        index=0,
        request=request,
        contacted_hotspot_position=None,
    )
    with pytest.raises(ScientificAdapterError, match="contacts none of the requested hotspots"):
        bindcraft.collect_design_output(request, no_atomic_contact)


def test_design_collector_rejects_semantic_and_content_address_tampering(tmp_path: Path) -> None:
    request = fixture("positive-default-lane")
    workspace = tmp_path / "wrong-engine"
    publish_shard(workspace, index=0, request=request, scoring_engine="rosettapy")
    with pytest.raises(ScientificAdapterError, match="PyRosetta"):
        bindcraft.collect_design_output(request, workspace)

    workspace = tmp_path / "tampered"
    publish_shard(workspace, index=0, request=request)
    metrics = workspace / "output" / "artifacts" / "candidate-000-metrics.json"
    metrics.write_text(metrics.read_text(encoding="utf-8") + " ", encoding="utf-8")
    with pytest.raises(ScientificAdapterError, match="content-addressed pointer"):
        bindcraft.collect_design_output(request, workspace)


def test_the_collector_holds_candidates_to_the_requested_binder_length(tmp_path: Path) -> None:
    request = fixture("positive-default-lane")
    window = request["parameters"]["binder_length"]
    workspace = tmp_path / "design-000"
    (workspace / "output").mkdir(parents=True)
    for length in (window["minimum"] - 1, window["maximum"] + 1):
        publish_shard(workspace, index=0, request=request, sequence="A" * length)
        with pytest.raises(ScientificAdapterError, match="binder length"):
            bindcraft.collect_output(request, workspace)
    publish_shard(workspace, index=0, request=request, sequence="A" * window["maximum"])
    assert bindcraft.collect_output(request, workspace).manifest["manifest_id"] == "bindcraft.shard.000.handoff"


def test_a_shard_from_another_backend_or_revision_is_refused(tmp_path: Path) -> None:
    request = fixture("positive-default-lane")
    workspace = tmp_path / "design-000"
    (workspace / "output").mkdir(parents=True)
    for override, expected in (
        ({"status": "failed"}, "successful immutable run"),
        ({"backend_id": "open-binder"}, "successful immutable run"),
        ({"source_revision": "0" * 40}, "successful immutable run"),
    ):
        publish_shard(workspace, index=0, request=request, **override)
        with pytest.raises(ScientificAdapterError, match=expected):
            bindcraft.collect_output(request, workspace)


def test_the_profile_matches_the_catalog_when_published() -> None:
    """Bind the in-test profile to the real one once onboarding publishes it."""

    published = json.loads(PROFILE_PATH.read_text(encoding="utf-8"))
    entry = next((item for item in published["profiles"] if item["model_id"] == "bindcraft"), None)
    if entry is None:
        pytest.skip("the BindCraft workload profile is not in the published catalog yet")
    assert entry["source"]["revision"] == bindcraft.SOURCE_REVISION
    assert entry["interface"]["parameter_schema"] == bindcraft.PARAMETER_SCHEMA
    assert [stage["id"] for stage in entry["workload"]["stages"]] == ["design", "aggregate"]
    assert entry["workload"]["stages"][0]["max_parallelism"] == bindcraft.MAX_DESIGN_SHARDS


def _current_successor_runtime() -> ModuleType:
    configured = os.environ.get("FS2_BINDCRAFT_RUNTIME_SOURCE")
    candidates = [
        Path(configured) if configured else None,
        SOLUTION_ROOT / "models/cancer-immunotherapy/images/bindcraft-native/runtime/bindcraft_runtime_entrypoint.py",
    ]
    source = next((candidate for candidate in candidates if candidate is not None and candidate.is_file()), None)
    if source is None:
        pytest.skip("the reviewed BindCraft successor runtime source is not present on this branch")
    assert hashlib.sha256(source.read_bytes()).hexdigest() == CURRENT_RUNTIME_SHA256
    spec = importlib.util.spec_from_file_location("fs2_bindcraft_successor_contract_test", source)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_compiled_argv_and_native_document_execute_the_current_successor_parser_contract(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _current_successor_runtime()
    request = fixture("positive-default-lane")
    target = tmp_path / "target.pdb"
    target.write_text(_pdb(complex_structure=True), encoding="ascii")
    target_bytes = target.read_bytes()
    admitted = verified_target(
        digest="sha256:" + hashlib.sha256(target_bytes).hexdigest(),
        size_bytes=len(target_bytes),
    )
    input_manifest = bindcraft.native_input_manifest(admitted)
    plan = bindcraft.compile_run(
        granted(),
        request,
        operation_id="op-bindcraft-successor-parser",
        input_artifacts=(admitted,),
    )
    public, parameters = bindcraft._request(request)
    design = plan.invocation("design", "design-000")
    aggregate = plan.invocation("aggregate", "main")
    assert runtime.parser().parse_args(list(design.argv[6:])).mode == "run-trajectory"
    assert runtime.parser().parse_args(list(aggregate.argv[6:])).mode == "aggregate"

    native = bindcraft.native_request(public, parameters, input_manifest, shard_index=0)
    native["parameters"]["_shard_index"] = 0
    monkeypatch.setenv("FS2_BINDCRAFT_TARGET_PDB", str(target))
    admitted_target, admitted_pointer = runtime._target_artifact(
        native,
        input_manifest,
    )
    assert admitted_target == target
    assert admitted_pointer["sha256"] == hashlib.sha256(target_bytes).hexdigest()
    template = tmp_path / "advanced.json"
    template.write_text(
        json.dumps(
            {
                "num_recycles_design": 3,
                "num_recycles_validation": 3,
                "num_seqs": 20,
                "max_mpnn_sequences": 2,
                "model_path": "multimer",
                "mpnn_weights": "original",
            }
        ),
        encoding="utf-8",
    )
    output = tmp_path / "settings-output"
    output.mkdir()
    settings_path, _advanced_path, _resolved = runtime._settings(
        native,
        target,
        output,
        template,
        bindcraft.AF2_PATH,
    )
    settings = json.loads(settings_path.read_text(encoding="utf-8"))
    assert settings["number_of_final_designs"] == parameters.accepted_designs(0)
    assert settings["target_hotspot_residues"] == "56,66,115"
    assert settings["chains"] == "A"


def test_successor_aggregate_consumes_the_bundle_and_final_collector_returns_every_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _current_successor_runtime()
    request = fixture("positive-default-lane")
    request["parameters"]["designs"] = 2
    plan = bindcraft.compile_run(
        granted(),
        request,
        operation_id="op-successor-aggregate",
        input_artifacts=(verified_target(),),
    )
    aggregate = tmp_path / "aggregate"
    for index in range(2):
        design = tmp_path / f"design-{index:03d}"
        publish_shard(design, index=index, request=request)
        collected = bindcraft.collect_design_output(request, design)
        entry = collected.manifest["entries"][0]  # type: ignore[index]
        bundle = collected.blobs[entry["artifact"]["artifact_id"]]
        safe_extract_tar(bundle, aggregate / "shards" / f"{index:03d}", compression="zstd")

    request_path, manifest_path = bindcraft.materialize_runtime_documents(
        plan.invocation("aggregate", "main"),
        aggregate,
    )
    assert bindcraft.materialize_runtime_documents(plan.invocation("aggregate", "main"), aggregate) == (
        request_path,
        manifest_path,
    )
    metadata = aggregate / ".fs2"
    marker = metadata / "runtime-localization.json"
    marker.write_text(json.dumps({"generation": "generation-test"}, sort_keys=True) + "\n", encoding="ascii")
    (aggregate / "output").mkdir()
    monkeypatch.setenv("FS2_RUNTIME_IMAGE_DIGEST", CONTRACT_TEST_IMAGE_DIGEST)
    runtime.aggregate(
        Namespace(
            backend_id=bindcraft.BACKEND_ID,
            atomic_rename=True,
            runtime_localization_marker=str(marker),
            request=str(request_path),
            input_manifest=str(manifest_path),
            shards=str(aggregate / "shards"),
            expected_shards=2,
            staging_manifest=str(aggregate / "output/output-manifest.json.tmp"),
            output_manifest=str(aggregate / "output/output-manifest.json"),
        )
    )

    collected = bindcraft.collect_stage_output(
        bindcraft.AGGREGATE_COLLECTOR_ID,
        request,
        aggregate,
        runtime_image_digest=CONTRACT_TEST_IMAGE_DIGEST,
    )
    entries = collected.manifest["entries"]
    by_name = {entry["name"]: entry for entry in entries}  # type: ignore[union-attr]
    assert len(entries) == 10  # two shards, six candidate files, aggregate identity, and source manifest
    assert by_name["candidate-000-metrics"]["artifact"]["media_type"] == "application/json"
    assert by_name["candidate-001-metrics"]["artifact"]["media_type"] == "application/json"
    assert by_name["candidate-000-structure"]["artifact"]["media_type"] == "chemical/x-pdb"
    assert by_name["candidate-001-relaxed-complex"]["artifact"]["media_type"] == "chemical/x-pdb"
    assert by_name["output-manifest"]["artifact"]["media_type"] == "application/json"
    for entry in entries:  # type: ignore[union-attr]
        pointer = entry["artifact"]
        content = collected.blobs[pointer["artifact_id"]]
        assert pointer["sha256"] == hashlib.sha256(content).hexdigest()
        assert pointer["size_bytes"] == len(content)

    with pytest.raises(ScientificAdapterError, match="runtime_image_digest"):
        bindcraft.collect_aggregate_output(request, aggregate, runtime_image_digest="sha256:" + "f" * 64)

    aggregate_invocation = plan.invocation("aggregate", "main")
    completion_sha256 = publish_stage_completion(aggregate_invocation, aggregate)
    monkeypatch.delenv("FS2_RUNTIME_IMAGE_DIGEST")
    with pytest.raises(ScientificAdapterError, match="immutable execution image digest"):
        bindcraft.collect_companion_output(aggregate_invocation, aggregate)
    monkeypatch.setenv("FS2_RUNTIME_IMAGE_DIGEST", "latest")
    with pytest.raises(ScientificAdapterError, match="immutable runtime image digest"):
        bindcraft.collect_companion_output(aggregate_invocation, aggregate)
    monkeypatch.setenv("FS2_RUNTIME_IMAGE_DIGEST", CONTRACT_TEST_IMAGE_DIGEST)
    companion_output = bindcraft.collect_companion_output(aggregate_invocation, aggregate)
    assert len(companion_output.artifacts) == len(entries)
    assert companion_output.validation["completion_marker_sha256"] == completion_sha256
    assert companion_output.validation["design_count"] == 2
    assert companion_output.validation["shard_count"] == 2
    assert companion_output.validation["runtime_image_digest"] == CONTRACT_TEST_IMAGE_DIGEST
    assert companion_output.validation["status"] == "passed"
    with pytest.raises(ScientificAdapterError, match="unsupported BindCraft collector"):
        bindcraft.collect_stage_output("bindcraft-unknown-v1", request, aggregate)


def test_the_shared_collector_types_json_csv_and_structures_apart(tmp_path: Path) -> None:
    """Regression for the shared helper itself, independent of BindCraft."""

    from fs2_serve.scientific_batch.adapters.common import collect_output_files

    workspace = tmp_path / "shared"
    workspace.mkdir()
    (workspace / "metrics.json").write_text(json.dumps({"a": 1}), encoding="utf-8")
    (workspace / "binder.pdb").write_text("ATOM\nTER\nEND\n", encoding="utf-8")
    (workspace / "complex.cif").write_text("data_x\n", encoding="utf-8")
    (workspace / "ranking.csv").write_text("id,score\n1,0.5\n", encoding="utf-8")
    collected = collect_output_files(
        workspace,
        (
            ("metrics", "some-metrics-json/v1", workspace / "metrics.json", False),
            ("structure", "protein-structure-pdb/v1", workspace / "binder.pdb", False),
            ("complex", "protein-complex-structure/v1", workspace / "complex.cif", False),
            ("ranking", "some-ranking-csv/v1", workspace / "ranking.csv", True),
        ),
        manifest_id="shared.media.types",
        maximum_total_bytes=1 << 20,
    )
    observed = {
        entry["name"]: entry["artifact"]["media_type"]  # type: ignore[index]
        for entry in collected.manifest["entries"]  # type: ignore[union-attr]
    }
    assert observed == {
        "metrics": "application/json",
        "structure": "chemical/x-pdb",
        "complex": "chemical/x-mmcif",
        "ranking": "text/csv",
    }
