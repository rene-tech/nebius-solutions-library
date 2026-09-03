"""Cross-contract tests for the current-scheduler AlphaFold 3 adapter."""

from __future__ import annotations

import copy
import hashlib
import importlib.util
import io
import json
import sys
import tarfile
from collections.abc import Mapping
from pathlib import Path
from types import MappingProxyType, ModuleType
from typing import Any
from uuid import UUID

import pytest
from jsonschema import Draft202012Validator

from fs2_serve.scientific_batch import (
    ArtifactAccessContext,
    CheckpointMode,
    MaterializationMode,
    PreemptionMode,
    ResourceClass,
    ScientificAdapterError,
    ScientificInputArtifact,
    compile_adapter_run,
    profile_from_catalog,
)
from fs2_serve.scientific_batch.adapters import (
    CollectionPendingError,
    collect_stage_output,
)
from fs2_serve.scientific_batch.adapters import alphafold3 as af3
from fs2_serve.scientific_batch.adapters.common import runtime_recipe_sha256
from fs2_serve.scientific_batch.execution import FileScientificManifestRenderer
from fs2_serve.scientific_batch.profile_catalog import (
    ScientificProfileCatalog,
    ScientificWorkloadProfile,
)

SOLUTION_ROOT = Path(__file__).resolve().parents[3]
ADAPTER_ROOT = SOLUTION_ROOT / "models/structure/batch-adapters/alphafold3"
PROFILE_PATH = SOLUTION_ROOT / "catalog/runtime/contracts/scientific-workload-profiles.json"
PARAMETER_SCHEMA_PATH = SOLUTION_ROOT / "catalog/runtime/schema/alphafold3-parameters.schema.json"
COMMAND_CONTRACT_PATH = (
    SOLUTION_ROOT
    / "models/cancer-immunotherapy/images/alphafold3/contracts/af3-command-io-contract.json"
)
RUNTIME_HANDOFF_PATH = (
    SOLUTION_ROOT
    / "models/cancer-immunotherapy/images/alphafold3/contracts/af3-runtime-handoff.json"
)
RUNTIME_FIXTURES = SOLUTION_ROOT / "models/cancer-immunotherapy/images/alphafold3/fixtures"
ACADEMIC_READINESS_PATH = SOLUTION_ROOT / "catalog/runtime/contracts/academic-asset-readiness.json"
CPU_CLASS_PATH = SOLUTION_ROOT / "scheduling/cpu-class-contract.json"
REFERENCE_REQUIREMENTS_PATH = SOLUTION_ROOT / "reference-data/model-requirements.json"


def load(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def request(name: str = "positive-raw.json") -> dict[str, Any]:
    return load(ADAPTER_ROOT / "fixtures" / name)


def profile() -> Mapping[str, object]:
    return profile_from_catalog(load(PROFILE_PATH), af3.MODEL_ID)


def verified_input(
    *,
    logical_artifact_id: str = af3.FOLD_INPUT_ID,
    semantic_type: str = af3.FOLD_INPUT_SEMANTIC_TYPE,
    artifact_id: UUID | None = None,
    size_bytes: int = 867,
    media_type: str = af3.FOLD_INPUT_MEDIA_TYPE,
    compression: str | None = "none",
) -> ScientificInputArtifact:
    return ScientificInputArtifact(
        logical_artifact_id=logical_artifact_id,
        semantic_type=semantic_type,
        artifact_id=artifact_id or UUID("00000000-0000-0000-0000-000000000001"),
        digest="sha256:" + "a" * 64,
        size_bytes=size_bytes,
        media_type=media_type,
        compression=compression,
    )


def compile_plan(
    document: dict[str, Any] | None = None,
    *,
    profile_value: Mapping[str, object] | None = None,
    operation_id: str = "op-af3-current",
    inputs: tuple[ScientificInputArtifact, ...] | None = None,
) -> Any:
    return compile_adapter_run(
        af3.MODEL_ID,
        profile_value or profile(),
        document or request(),
        operation_id=operation_id,
        variant_id=af3.VARIANT_ID,
        access_context=ArtifactAccessContext(
            profile="academic",
            receipt_digest="sha256:" + "f" * 64,
            tenant_id="academic-poc",
        ),
        input_artifacts=inputs if inputs is not None else (verified_input(),),
    )


def reference_data_producer() -> ModuleType:
    path = SOLUTION_ROOT / "reference-data/reference_data.py"
    spec = importlib.util.spec_from_file_location("af3_reference_data_producer", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def runtime_producer() -> ModuleType:
    path = SOLUTION_ROOT / "models/cancer-immunotherapy/images/alphafold3/runtime/af3_runtime.py"
    name = "af3_runtime_for_current_adapter"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def terminal_image() -> dict[str, object]:
    return {
        "runtime_id": af3.MODEL_ID,
        "upstream_commit": af3.SOURCE_REVISION,
        "parameters_embedded": False,
        "reference_databases_embedded": False,
    }


def data_workspace(tmp_path: Path, *, status: str = "PASS") -> Path:
    output = tmp_path / af3.DATA_OUTPUT_DIR
    job = output / "pdl1-binder"
    job.mkdir(parents=True)
    payload = {"name": "pdl1-binder", "sequences": [{"protein": {"sequence": "ACDE"}}]}
    (job / "pdl1-binder_data.json").write_text(
        json.dumps(payload, separators=(",", ":")), encoding="utf-8"
    )
    handoff = runtime_producer().build_data_handoff(output)
    receipt = load(RUNTIME_FIXTURES / "data-stage-receipt.json")
    receipt.update(
        {
            "image": terminal_image(),
            "status": status,
            "execution": {
                "upstream": "/app/alphafold/run_alphafold.py",
                "exit_code": 0 if status == "PASS" else 1,
                "terminal_state": "succeeded" if status == "PASS" else "failed",
            },
            "handoff": handoff,
        }
    )
    (tmp_path / af3.DATA_RECEIPT_FILENAME).write_text(
        json.dumps(receipt, separators=(",", ":")), encoding="utf-8"
    )
    return tmp_path


def inference_workspace(tmp_path: Path, *, status: str = "PASS") -> Path:
    output = tmp_path / "outputs" / "pdl1-binder"
    output.mkdir(parents=True)
    (output / "pdl1-binder_model.cif").write_bytes(
        b"data_pdl1_binder\n#\nATOM 1 C CA A 1 0.0 0.0 0.0\n#\n"
    )
    (output / "pdl1-binder_summary_confidences.json").write_text(
        json.dumps({"ranking_score": 0.87}), encoding="utf-8"
    )
    receipt = load(RUNTIME_FIXTURES / "inference-stage-receipt.json")
    receipt.update(
        {
            "image": terminal_image(),
            "status": status,
            "execution": {
                "upstream": "/app/alphafold/run_alphafold.py",
                "exit_code": 0 if status == "PASS" else 1,
                "terminal_state": "succeeded" if status == "PASS" else "failed",
            },
        }
    )
    (tmp_path / af3.INFERENCE_RECEIPT_FILENAME).write_text(
        json.dumps(receipt, separators=(",", ":")), encoding="utf-8"
    )
    return tmp_path


def test_profile_and_public_parameters_are_current_candidate_contracts() -> None:
    candidate = profile()
    assert candidate["route_exposed"] is False
    assert candidate["state"] == "candidate-unqualified"
    assert candidate["execution_identity"]["runtime_image_digest"] == af3.RUNTIME_IMAGE_DIGEST  # type: ignore[index]
    assert "eaea560c" not in json.dumps(candidate)
    assert [item["artifact_id"] for item in candidate["runtime_artifacts"]] == [  # type: ignore[index]
        af3.PARAMETERS_ARTIFACT
    ]

    validator = Draft202012Validator(load(PARAMETER_SCHEMA_PATH))
    assert not list(validator.iter_errors(request()["parameters"]))
    assert not list(validator.iter_errors(request("positive-selected-job.json")["parameters"]))
    assert list(validator.iter_errors(request("negative-request-license.json")["parameters"]))

    workload = candidate["workload"]
    runtime_hash = runtime_recipe_sha256(SOLUTION_ROOT, af3.MODEL_ID)
    workload_hash = hashlib.sha256(
        json.dumps(workload, separators=(",", ":"), sort_keys=True).encode()
    ).hexdigest()
    identity = candidate["execution_identity"]
    assert identity["runtime_recipe_sha256"] == runtime_hash  # type: ignore[index]
    assert identity["workload_recipe_sha256"] == workload_hash  # type: ignore[index]


def test_commands_are_the_exact_supported_runtime_surface() -> None:
    contract = load(COMMAND_CONTRACT_PATH)
    plan = compile_plan()
    data = plan.invocation(af3.DATA_STAGE_ID, "main")
    inference = plan.invocation(af3.INFERENCE_STAGE_ID, "main")

    assert tuple(contract["entrypoint"]["command"]) == af3.RUNTIME_COMMAND
    assert data.argv[:3] == (*af3.RUNTIME_COMMAND, "data")
    assert inference.argv[:3] == (*af3.RUNTIME_COMMAND, "inference")
    assert data.argv[data.argv.index("--receipt") + 1].endswith(af3.DATA_RECEIPT_FILENAME)
    assert inference.argv[inference.argv.index("--receipt") + 1].endswith(
        af3.INFERENCE_RECEIPT_FILENAME
    )
    allowed = {
        flag
        for stage in contract["stages"].values()
        for key in ("runtime_args", "optional_runtime_args")
        for flag in stage[key]
        if flag.startswith("--")
    }
    emitted = {
        value for invocation in plan.invocations for value in invocation.argv if value.startswith("--")
    }
    assert emitted <= allowed
    assert all(
        invocation.argv[0] not in {"sh", "bash", "/bin/sh", "/bin/bash"}
        for invocation in plan.invocations
    )
    serialized = "\0".join(value for invocation in plan.invocations for value in invocation.argv)
    assert all(token not in serialized for token in af3.FORBIDDEN_ARGV_TOKENS)

    selected = compile_plan(request("positive-selected-job.json")).invocation(
        af3.INFERENCE_STAGE_ID, "main"
    )
    assert selected.argv[selected.argv.index("--fold-job") + 1] == "pdl1-binder"


def test_reference_root_and_private_parameter_file_are_never_co_mounted() -> None:
    command = load(COMMAND_CONTRACT_PATH)
    readiness = load(ACADEMIC_READINESS_PATH)
    academic = next(item for item in readiness["models"] if item["model_id"] == af3.MODEL_ID)
    plan = compile_plan()
    data = plan.invocation(af3.DATA_STAGE_ID, "main")
    inference = plan.invocation(af3.INFERENCE_STAGE_ID, "main")
    reference = data.runtime_mounts[0]
    parameters = inference.runtime_mounts[0]

    assert reference.mount_path == command["root_layout"]["reference_root"]["mount_path"]
    assert reference.sub_path is None and reference.read_only is True
    assert data.runtime_artifacts == (af3.REFERENCE_ARTIFACT,)
    assert parameters.artifact_id == academic["runtime_binding"]["artifact_id"]
    assert parameters.mount_path == academic["runtime_binding"]["consumer_path"]
    assert parameters.sub_path == academic["runtime_binding"]["source_sub_path"]
    assert parameters.expected_content_sha256 == academic["runtime_binding"]["content_digest_sha256"]
    assert parameters.supplemental_groups == (academic["delivery"]["asset_gid"],)
    assert inference.runtime_artifacts == (af3.PARAMETERS_ARTIFACT,)
    assert af3.PARAMETERS_MOUNT_PATH not in data.argv
    assert af3.REFERENCE_MOUNT_PATH not in inference.argv


def test_reference_receipt_path_is_derived_from_the_producer_contract() -> None:
    producer = reference_data_producer()
    receipt = producer.build_terminal_receipt(
        bundle_id=af3.REFERENCE_ARTIFACT,
        revision=af3.REFERENCE_REVISION,
        tree_sha256="a" * 64,
        manifest_sha256="b" * 64,
        inventory_sha256="c" * 64,
        file_count=5001,
        expanded_bytes=630_000_000_000,
        created_at="2026-09-03T00:00:00Z",
    )
    assert receipt["storage"]["host_root"] == af3.REFERENCE_HOST_ROOT
    assert receipt["storage"]["mount_path"] == af3.REFERENCE_MOUNT_PATH
    assert receipt["content"]["inventory_marker"] == af3.REFERENCE_INVENTORY_MARKER
    assert producer.derive_database_root(receipt).startswith(f"{af3.REFERENCE_MOUNT_PATH}/datasets/")
    invocation = compile_plan().invocation(af3.DATA_STAGE_ID, "main")
    assert invocation.argv[invocation.argv.index("--reference-receipt") + 1] == (
        f"{af3.REFERENCE_MOUNT_PATH}/receipts/{receipt['bundle_id']}/{receipt['revision']}.json"
    )


def test_current_scheduler_stage_identity_and_cpu_envelope_are_exact() -> None:
    cpu_contract = load(CPU_CLASS_PATH)
    bound = next(
        item
        for item in cpu_contract["classes"]["reference-data"]["bound_workloads"]
        if item["model_id"] == af3.MODEL_ID and item["stage"] == af3.DATA_STAGE_ID
    )
    requirements = load(REFERENCE_REQUIREMENTS_PATH)["models"][af3.MODEL_ID][
        "preprocessing_capacity"
    ]
    stages = {item["id"]: item for item in profile()["workload"]["stages"]}  # type: ignore[index]
    assert bound["capacity"] == {"cpu": af3.DATA_CPU, "memory": af3.DATA_MEMORY}
    assert stages[af3.DATA_STAGE_ID]["resources"] == {
        "cpu_millis": 16_000,
        "memory_bytes": 64 * 1024**3,
        "ephemeral_storage_bytes": 32 * 1024**3,
        "limits": {
            "cpu_millis": 16_000,
            "memory_bytes": 64 * 1024**3,
            "ephemeral_storage_bytes": 32 * 1024**3,
        },
    }
    assert requirements["cpu"] == af3.DATA_CPU
    assert requirements["memory"] == af3.DATA_MEMORY

    plan = compile_plan()
    assert [stage.stage_id for stage in plan.controller_plan.stages] == [
        af3.DATA_STAGE_ID,
        af3.INFERENCE_STAGE_ID,
    ]
    assert [stage.resource_class for stage in plan.controller_plan.stages] == [
        ResourceClass.CPU,
        ResourceClass.GPU,
    ]
    assert plan.controller_plan.stage(af3.INFERENCE_STAGE_ID).depends_on == (af3.DATA_STAGE_ID,)
    assert plan.controller_plan.stage(af3.DATA_STAGE_ID).checkpoint_mode is CheckpointMode.NONE
    assert plan.controller_plan.stage(af3.DATA_STAGE_ID).preemption_mode is PreemptionMode.NON_PREEMPTIBLE
    assert plan.controller_plan.stage(af3.INFERENCE_STAGE_ID).checkpoint_mode is CheckpointMode.RESTART
    assert plan.controller_plan.stage(af3.INFERENCE_STAGE_ID).preemption_mode is PreemptionMode.RESTARTABLE


def test_authorization_is_deployment_bound_and_request_has_no_receipt() -> None:
    readiness = load(ACADEMIC_READINESS_PATH)
    academic = next(item for item in readiness["models"] if item["model_id"] == af3.MODEL_ID)
    assert academic["use_authorization_status"] == "Granted"
    assert academic["execution_authorization_status"] == "Authorized"
    assert academic["serving_admission"] == "AdmittedNoPerRequestLicenseReceipt"
    assert readiness["request_time_license_receipt_required"] is False
    assert "license" not in json.dumps(request()).lower()
    assert compile_plan().model_id == af3.MODEL_ID

    for field, value in (
        ("profile", "standard"),
        ("state", "blocked"),
        ("receipt_digest", "f" * 64),
        ("credentials_embedded", True),
    ):
        denied = copy.deepcopy(dict(profile()))
        denied["access"][field] = value
        with pytest.raises(ScientificAdapterError, match=af3.ADMISSION_BLOCKER):
            compile_plan(profile_value=denied, operation_id=f"op-denied-{field}")

    with pytest.raises(ScientificAdapterError, match="unknown.*license_receipt"):
        compile_plan(request("negative-request-license.json"))


@pytest.mark.parametrize(
    ("inputs", "match"),
    [
        ((), "exactly one fold-input"),
        ((verified_input(logical_artifact_id="request"),), "canonical fold-input"),
        ((verified_input(semantic_type="request/v1"),), "canonical fold-input"),
        ((verified_input(media_type="application/gzip"),), "uncompressed application/json"),
        ((verified_input(compression="gzip"),), "uncompressed application/json"),
        ((verified_input(size_bytes=0),), "outside the adapter bound"),
        (
            (verified_input(artifact_id=UUID("11111111-1111-4111-8111-111111111111")),),
            "distinct artifacts",
        ),
    ],
)
def test_only_the_artifact_service_verified_fold_input_is_compiled(
    inputs: tuple[ScientificInputArtifact, ...], match: str
) -> None:
    with pytest.raises(ScientificAdapterError, match=match):
        compile_plan(inputs=inputs)


def test_handoff_is_operation_isolated_and_portable_between_stages() -> None:
    first = compile_plan(operation_id="op-af3-one")
    second = compile_plan(operation_id="op-af3-two")
    data = first.invocation(af3.DATA_STAGE_ID, "main")
    inference = first.invocation(af3.INFERENCE_STAGE_ID, "main")
    assert data.working_directory != inference.working_directory
    assert {item.working_directory for item in first.invocations}.isdisjoint(
        {item.working_directory for item in second.invocations}
    )
    assert data.materializations[0].mode is MaterializationMode.COPY_FILE
    assert inference.consumes == (data.produces,)
    assert inference.materializations[0].artifact_id == data.produces
    assert inference.materializations[0].mode is MaterializationMode.EXTRACT_TAR
    assert data.working_directory not in inference.argv


def test_data_collector_accepts_runtime_output_through_the_shared_registry(tmp_path: Path) -> None:
    workspace = data_workspace(tmp_path)
    invocation = compile_plan().invocation(af3.DATA_STAGE_ID, "main")
    first = collect_stage_output(invocation, workspace)
    second = collect_stage_output(invocation, workspace)
    assert first.validation == second.validation
    assert first.artifacts[0].name == "processed-input"
    blob = first.artifacts[0].path.read_bytes()
    assert first.validation["sha256"] == hashlib.sha256(blob).hexdigest()
    with tarfile.open(fileobj=io.BytesIO(blob), mode="r:") as archive:
        members = archive.getmembers()
        assert [member.name for member in members] == [
            af3.HANDOFF_INDEX,
            "pdl1-binder/pdl1-binder_data.json",
        ]
        assert all(member.uid == member.gid == member.mtime == 0 for member in members)


def test_data_collector_refuses_failed_tampered_and_symlinked_output(tmp_path: Path) -> None:
    invocation = compile_plan().invocation(af3.DATA_STAGE_ID, "main")
    with pytest.raises(ScientificAdapterError, match="terminal PASS"):
        collect_stage_output(invocation, data_workspace(tmp_path / "failed", status="FAIL"))

    tampered = data_workspace(tmp_path / "tampered")
    payload = (
        tampered
        / af3.DATA_OUTPUT_DIR
        / af3.HANDOFF_DIR_NAME
        / "pdl1-binder"
        / "pdl1-binder_data.json"
    )
    payload.write_bytes(payload.read_bytes() + b"\n")
    with pytest.raises(ScientificAdapterError, match="digest or size"):
        collect_stage_output(invocation, tampered)

    linked = data_workspace(tmp_path / "linked")
    payload = (
        linked
        / af3.DATA_OUTPUT_DIR
        / af3.HANDOFF_DIR_NAME
        / "pdl1-binder"
        / "pdl1-binder_data.json"
    )
    target = linked / "target.json"
    target.write_bytes(payload.read_bytes())
    payload.unlink()
    payload.symlink_to(target)
    with pytest.raises(ScientificAdapterError, match="must not use symlinks"):
        collect_stage_output(invocation, linked)


def test_result_collector_requires_terminal_receipt_and_semantic_outputs(tmp_path: Path) -> None:
    invocation = compile_plan().invocation(af3.INFERENCE_STAGE_ID, "main")
    workspace = inference_workspace(tmp_path / "pass")
    collected = collect_stage_output(invocation, workspace)
    assert [item.name for item in collected.artifacts] == ["structure", "summary-confidence"]
    assert collected.validation["status"] == "passed"
    assert collected.validation["ranking_score"] == 0.87

    with pytest.raises(ScientificAdapterError, match="terminal PASS"):
        collect_stage_output(invocation, inference_workspace(tmp_path / "failed", status="FAIL"))

    invalid = inference_workspace(tmp_path / "invalid")
    summary = invalid / "outputs/pdl1-binder/pdl1-binder_summary_confidences.json"
    summary.write_text('{"ranking_score": NaN}', encoding="utf-8")
    with pytest.raises(ScientificAdapterError, match="finite ranking_score"):
        collect_stage_output(invocation, invalid)


def test_collectors_wait_for_incomplete_workspaces(tmp_path: Path) -> None:
    with pytest.raises(CollectionPendingError):
        collect_stage_output(compile_plan().invocation(af3.DATA_STAGE_ID, "main"), tmp_path)
    with pytest.raises(CollectionPendingError):
        collect_stage_output(
            compile_plan().invocation(af3.INFERENCE_STAGE_ID, "main"),
            tmp_path / "missing-inference",
        )


def test_route_gate_tracks_reference_publication_without_downgrading_runtime_evidence() -> None:
    reference = load(
        SOLUTION_ROOT
        / "models/cancer-immunotherapy/images/alphafold3/contracts/af3-reference-data-binding.json"
    )
    handoff = load(RUNTIME_HANDOFF_PATH)
    candidate = profile()
    assert reference["state"] == "pending-publication"
    assert candidate["route_exposed"] is False
    assert handoff["readiness"]["state"] == "runtime-qualified"
    assert handoff["image"]["digest"] == af3.RUNTIME_IMAGE_DIGEST
    assert any("terminal receipt" in item for item in candidate["policy"]["limitations"])  # type: ignore[index]


def test_current_execution_map_binds_producer_receipt_and_private_parameter(
    tmp_path: Path,
) -> None:
    """Exercise the production v3 planner and localization verifier end to end."""

    tree = "1" * 64
    manifest = "2" * 64
    inventory = "3" * 64
    expanded_bytes = 630_000_000_000
    terminal = reference_data_producer().build_terminal_receipt(
        bundle_id=af3.REFERENCE_ARTIFACT,
        revision=af3.REFERENCE_REVISION,
        tree_sha256=tree,
        manifest_sha256=manifest,
        inventory_sha256=inventory,
        file_count=5001,
        expanded_bytes=expanded_bytes,
        created_at="2026-09-03T00:00:00Z",
    )
    terminal_json = json.dumps(terminal, sort_keys=True, separators=(",", ":"))

    qualified = copy.deepcopy(dict(profile()))
    qualified["state"] = "qualified"
    qualified["route_exposed"] = True
    qualified["source"]["classification"] = "qualified-input"
    qualified["semantic_validation"]["state"] = "qualified"
    qualified["execution_identity"]["artifact_manifest_digest"] = "4" * 64
    qualified["execution_identity"]["execution_identity_sha256"] = "5" * 64
    qualified["runtime_artifacts"].append(
        {
            "artifact_id": af3.REFERENCE_ARTIFACT,
            "content_identity": {"digest_sha256": tree, "size_bytes": expanded_bytes},
            "file_manifest": [],
            "required_files": [],
            "readiness_manifest_sha256": manifest,
        }
    )
    scientific_profile = ScientificWorkloadProfile(MappingProxyType(qualified))
    catalog_root = SOLUTION_ROOT / "catalog/runtime"
    loaded = ScientificProfileCatalog.load(catalog_root)
    profiles = ScientificProfileCatalog(
        profiles={af3.MODEL_ID: scientific_profile},
        validators=loaded._validators,  # type: ignore[attr-defined]
    )

    workspace_mount = {
        "name": "artifact-workspace",
        "kind": "artifact-workspace",
        "claim_name": None,
        "host_path": None,
        "mount_path": "/mnt/fs2-scientific",
        "sub_path": None,
        "read_only": False,
    }
    execution_map = {
        "schema": "fs2-serve.nebius.ai/scientific-execution-map/v3",
        "models": [
            {
                "model_id": af3.MODEL_ID,
                "variant_id": af3.VARIANT_ID,
                "workload_namespace": af3.EXECUTION_NAMESPACE,
                "access_profile": "academic",
                "execution_identity_sha256": "5" * 64,
                "plan_adapter": {
                    "module": "fs2_serve.scientific_batch.adapters",
                    "function": "compile_adapter_run",
                },
                "runtime_artifacts": [
                    {
                        "artifact_id": af3.PARAMETERS_ARTIFACT,
                        "mount_path": af3.PARAMETERS_MOUNT_PATH,
                        "content_digest": "sha256:" + af3.PARAMETERS_SHA256,
                        "file_manifest": [
                            {
                                "path": "af3.bin.zst",
                                "sha256": af3.PARAMETERS_SHA256,
                                "size_bytes": af3.PARAMETERS_SIZE_BYTES,
                            }
                        ],
                        "localization_receipt_digest": "sha256:" + "6" * 64,
                    },
                    {
                        "artifact_id": af3.REFERENCE_ARTIFACT,
                        "mount_path": af3.REFERENCE_MOUNT_PATH,
                        "content_digest": "sha256:" + tree,
                        "aggregate_tree": {
                            "storage_kind": "reference-data-plane",
                            "tree_sha256": tree,
                            "manifest_sha256": manifest,
                            "inventory_sha256": inventory,
                            "manifest_algorithm": (
                                "fs2-serve.nebius.ai/reference-data-manifest/v1"
                            ),
                            "file_count": 5001,
                            "directory_count": 0,
                            "expanded_bytes": expanded_bytes,
                            "canonical_path": terminal["storage"]["dataset_sub_path"],
                            "marker_relative_path": af3.REFERENCE_INVENTORY_MARKER,
                        },
                        "verification_receipt": terminal,
                        "localization_receipt_digest": (
                            "sha256:" + hashlib.sha256(terminal_json.encode()).hexdigest()
                        ),
                    },
                ],
                "stages": [
                    {
                        "stage_id": af3.DATA_STAGE_ID,
                        "image": "registry.test/alphafold3@" + af3.RUNTIME_IMAGE_DIGEST,
                        "collector_id": af3.DATA_COLLECTOR_ID,
                        "validator_id": af3.DATA_VALIDATOR_ID,
                        "mounts": [
                            workspace_mount,
                            {
                                "name": "alphafold3-databases",
                                "kind": "reference",
                                "claim_name": None,
                                "host_path": af3.REFERENCE_HOST_ROOT,
                                "mount_path": af3.REFERENCE_MOUNT_PATH,
                                "sub_path": None,
                                "read_only": True,
                            },
                        ],
                        "service_account_name": "fs2-academic-runner",
                        "resources": {
                            "requests": {
                                "cpu": "16",
                                "memory": "64Gi",
                                "ephemeral_storage": "32Gi",
                            },
                            "limits": {
                                "cpu": "16",
                                "memory": "64Gi",
                                "ephemeral_storage": "32Gi",
                            },
                        },
                        "active_deadline_seconds": 21600,
                        "termination_grace_seconds": 60,
                        "environment": {"FS2_NETWORK_MODE": "offline"},
                        "required_node_labels": {
                            "storage.fs2.nebius/reference-data": "true"
                        },
                    },
                    {
                        "stage_id": af3.INFERENCE_STAGE_ID,
                        "image": "registry.test/alphafold3@" + af3.RUNTIME_IMAGE_DIGEST,
                        "collector_id": af3.RESULT_COLLECTOR_ID,
                        "validator_id": af3.VALIDATOR_ID,
                        "mounts": [
                            workspace_mount,
                            {
                                "name": "alphafold3-parameters",
                                "kind": "private",
                                "claim_name": af3.PARAMETERS_CLAIM,
                                "host_path": None,
                                "mount_path": af3.PARAMETERS_MOUNT_PATH,
                                "sub_path": af3.PARAMETERS_SOURCE_SUB_PATH,
                                "read_only": True,
                            },
                        ],
                        "service_account_name": "fs2-academic-runner",
                        "resources": {
                            "requests": {
                                "cpu": "8",
                                "memory": "64Gi",
                                "ephemeral_storage": "64Gi",
                            },
                            "limits": {
                                "cpu": "32",
                                "memory": "192Gi",
                                "ephemeral_storage": "64Gi",
                            },
                        },
                        "active_deadline_seconds": 21600,
                        "termination_grace_seconds": 60,
                        "environment": {"FS2_NETWORK_MODE": "offline"},
                        "required_node_labels": {
                            "accelerator.fs2.nebius/class": "nvidia-h100-sxm5-80gb"
                        },
                    },
                ],
            }
        ],
    }
    path = tmp_path / "scientific-execution-map.json"
    path.write_text(json.dumps(execution_map), encoding="utf-8")
    renderer = FileScientificManifestRenderer(
        path=path,
        profiles=profiles,
        academic_tenant_id="academic-poc",
        academic_authorization_receipt_sha256="7" * 64,
    )
    access = renderer.access_context(scientific_profile, tenant_id="academic-poc")
    plan = renderer.plan(
        scientific_profile,
        request(),
        access_context=access,
        input_artifacts=(verified_input(),),
    )
    localizations = renderer.verify_runtime_artifacts(scientific_profile, plan, access)
    bound = renderer.bind_runtime_artifacts(
        scientific_profile, plan, access, localizations
    )
    bound.assert_controller_bound()
    data_mount = bound.invocation(af3.DATA_STAGE_ID, "main").runtime_mounts[0]
    parameter_mount = bound.invocation(af3.INFERENCE_STAGE_ID, "main").runtime_mounts[0]
    assert data_mount.mount_path == af3.REFERENCE_MOUNT_PATH
    assert data_mount.sub_path is None
    assert data_mount.readiness_receipt_sha256 == hashlib.sha256(terminal_json.encode()).hexdigest()
    assert parameter_mount.mount_path == af3.PARAMETERS_MOUNT_PATH
    assert parameter_mount.authorization_receipt_sha256 == "7" * 64
