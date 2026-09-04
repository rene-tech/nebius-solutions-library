"""Fail-closed contracts for the isolated academic AlphaFold 3 candidate."""

from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path
from uuid import UUID

import pytest

from fs2_serve.scientific_batch import (
    ArtifactAccessContext,
    MaterializationMode,
    ResourceClass,
    ScientificAdapterError,
    ScientificInputArtifact,
    companion,
    compile_adapter_run,
)
from fs2_serve.scientific_batch.adapters import CollectionPendingError, collect_stage_output
from fs2_serve.scientific_batch.adapters import alphafold3 as af3
from fs2_serve.scientific_batch.adapters.production_registry import install_production_adapters
from fs2_serve.scientific_batch.scheduling import SchedulingContractError, SchedulingContractResolver

SOLUTION_ROOT = Path(__file__).resolve().parents[3]
ADAPTER_ROOT = SOLUTION_ROOT / "models/structure/batch-adapters/alphafold3"
RUNTIME_FIXTURES = SOLUTION_ROOT / "models/cancer-immunotherapy/images/alphafold3/fixtures"
RUNTIME_HANDOFF = (
    SOLUTION_ROOT
    / "models/cancer-immunotherapy/images/alphafold3/contracts/af3-runtime-handoff.json"
)
R7_IMAGE_DIGEST = "sha256:ecc3e7352da7984e854f67d8024ed28fa6dbbbf7cfae39aa5a50f8a29eda85e7"

install_production_adapters()


def load(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def profile() -> dict[str, object]:
    projection = load(ADAPTER_ROOT / "activation/workload-profile.json")
    value = projection["profile"]
    assert isinstance(value, dict)
    return value


def request() -> dict[str, object]:
    return load(ADAPTER_ROOT / "fixtures/positive-raw.json")


def verified_input(*, artifact_id: UUID | None = None) -> ScientificInputArtifact:
    return ScientificInputArtifact(
        logical_artifact_id=af3.FOLD_INPUT_ID,
        semantic_type=af3.FOLD_INPUT_SEMANTIC_TYPE,
        artifact_id=artifact_id or UUID("00000000-0000-4000-8000-000000000001"),
        digest="sha256:" + "a" * 64,
        size_bytes=867,
        media_type=af3.FOLD_INPUT_MEDIA_TYPE,
        compression="none",
    )


def compile_plan(*, inputs: tuple[ScientificInputArtifact, ...] | None = None):
    return compile_adapter_run(
        af3.MODEL_ID,
        profile(),
        request(),
        operation_id="op-af3-isolated-candidate",
        variant_id=af3.VARIANT_ID,
        access_context=ArtifactAccessContext(
            profile="academic",
            receipt_digest="sha256:" + "f" * 64,
            tenant_id="academic-poc",
        ),
        input_artifacts=(verified_input(),) if inputs is None else inputs,
    )


def academic_scheduling() -> SchedulingContractResolver:
    return SchedulingContractResolver(
        {
            "schema": "fs2-serve.nebius.ai/kueue-scheduling/v1",
            "pool_node_label_key": "accelerator.fs2.nebius/pool-id",
            "service_classes": {
                "customer-batch": {
                    "workload_priority_class": "customer-batch",
                    "priority": 0,
                    "default_local_queue": af3.INFERENCE_LOCAL_QUEUE,
                    "preemption_mode": "restartable",
                    "pool_preference": ["h100-reserved-8x", "h100-1x"],
                    "max_queue_seconds": 900,
                    "max_execution_seconds": 86400,
                    "caller_selectable": True,
                }
            },
            "local_queues": {
                af3.INFERENCE_LOCAL_QUEUE: {
                    "metadata": {"name": af3.INFERENCE_LOCAL_QUEUE, "namespace": af3.EXECUTION_NAMESPACE},
                    "spec": {"clusterQueue": "inference"},
                },
                af3.DATA_LOCAL_QUEUE: {
                    "metadata": {"name": af3.DATA_LOCAL_QUEUE, "namespace": af3.EXECUTION_NAMESPACE},
                    "spec": {"clusterQueue": af3.DATA_CLUSTER_QUEUE},
                },
            },
            "cluster_queues": {
                "inference": {"metadata": {"name": "inference"}, "spec": {}},
                af3.DATA_CLUSTER_QUEUE: {"metadata": {"name": af3.DATA_CLUSTER_QUEUE}, "spec": {}},
            },
            "workload_priority_classes": {"customer-batch": {"value": 0}},
            "local_queue_routes": {
                af3.INFERENCE_LOCAL_QUEUE: {
                    "namespace": af3.EXECUTION_NAMESPACE,
                    "cluster_queue": "inference",
                    "model_ids": [],
                    "tenant_ids": [],
                    "service_classes": [],
                },
                af3.DATA_LOCAL_QUEUE: {
                    "namespace": af3.EXECUTION_NAMESPACE,
                    "cluster_queue": af3.DATA_CLUSTER_QUEUE,
                    "model_ids": [],
                    "tenant_ids": [],
                    "service_classes": [],
                },
            },
            "model_eligible_pool_ids": {af3.MODEL_ID: ["h100-reserved-8x", "h100-1x"]},
            "cpu_classes_schema": "fs2-serve.nebius.ai/cpu-stage-classes/v1",
            "cpu_classes": {
                "reference-data": {
                    "local_queue": af3.DATA_LOCAL_QUEUE,
                    "cluster_queue": af3.DATA_CLUSTER_QUEUE,
                    "namespace": af3.EXECUTION_NAMESPACE,
                    "resource_flavor": "reference-data-cpu",
                    "pool_id": "reference-data-cpu",
                    "node_selector": {"storage.fs2.nebius/reference-data": "true"},
                    "tolerations": [],
                    "schedulable_capacity": {
                        "cpu_millicores": 32000,
                        "memory_mib": 131072,
                        "ephemeral_storage_mib": 131072,
                    },
                }
            },
            "cpu_stage_requests": {"reference-data": {"cpu_millicores": 16000, "memory_mib": 65536}},
            "namespace_bound_models": {af3.MODEL_ID: af3.EXECUTION_NAMESPACE},
            "pools": {
                "h100-1x": {
                    "resource_flavor": "h100-1x",
                    "accelerator_resource_name": "nvidia.com/gpu",
                    "capacity": 1,
                },
                "h100-reserved-8x": {
                    "resource_flavor": "h100-reserved-8x",
                    "accelerator_resource_name": "nvidia.com/gpu",
                    "capacity": 8,
                },
            },
        }
    )


def test_candidate_keeps_academic_planes_separate_and_is_not_route_exposed() -> None:
    candidate = profile()
    contract = load(ADAPTER_ROOT / "contract.json")
    handoff = load(RUNTIME_HANDOFF)
    assert contract["runtime"]["workspace_uid"] == 1001
    assert contract["runtime"]["workspace_gid"] == 1001
    assert contract["runtime"]["production_protocol_compatible"] is True
    assert handoff["image"]["tag"] == "3.0.4-85c4d205-r7"
    assert handoff["image"]["digest"] == R7_IMAGE_DIGEST
    assert contract["runtime"]["image_digest"] == handoff["image"]["digest"]
    assert af3.RUNTIME_IMAGE_DIGEST == handoff["image"]["digest"]
    assert candidate["execution_identity"]["runtime_image_digest"] == handoff["image"]["digest"]
    assert "h100" in contract["runtime"]["qualification_required"].lower()
    assert contract["route_gate"]["route_exposed"] is False
    assert candidate["state"] == "candidate-unqualified"
    assert candidate["route_exposed"] is False
    assert candidate["resources"]["compatible_pool_ids"] == ["h100-reserved-8x", "h100-1x"]
    assert candidate["resources"]["required_node_labels"] == {
        "accelerator.fs2.nebius/class": "nvidia-h100-sxm5-80gb"
    }
    assert candidate["access"] == {
        "profile": "academic",
        "state": "verified",
        "receipt_digest": None,
        "credentials_embedded": False,
    }
    plan = compile_plan()
    data, inference = plan.invocations
    assert [stage.resource_class for stage in plan.controller_plan.stages] == [
        ResourceClass.CPU,
        ResourceClass.GPU,
    ]
    assert all(
        stage.resources is not None and stage.placement_class is not None for stage in plan.controller_plan.stages
    )
    assert data.runtime_artifacts == (af3.REFERENCE_ARTIFACT,)
    assert inference.runtime_artifacts == (af3.PARAMETERS_ARTIFACT,)
    assert data.runtime_mounts == (af3.mount_contract(af3.DATA_STAGE_ID),)
    assert data.runtime_mounts[0].read_only is True
    assert data.runtime_mounts[0].mount_path == af3.REFERENCE_MOUNT_PATH
    assert data.runtime_mounts[0].sub_path is None
    assert data.runtime_mounts[0].supplemental_groups == (af3.PUBLIC_ARTIFACT_SUPPLEMENTAL_GROUP,)
    assert inference.runtime_mounts == (af3.mount_contract(af3.INFERENCE_STAGE_ID),)
    assert inference.runtime_mounts[0].read_only is True
    assert inference.runtime_mounts[0].mount_path == af3.PARAMETERS_MOUNT_PATH
    assert inference.runtime_mounts[0].sub_path == af3.PARAMETERS_SOURCE_SUB_PATH
    assert inference.runtime_mounts[0].supplemental_groups == (af3.PARAMETERS_SUPPLEMENTAL_GROUP,)
    assert data.materializations[0].artifact_id == af3.FOLD_INPUT_ID
    assert inference.materializations[0].artifact_id == data.produces
    assert inference.materializations[0].mode is MaterializationMode.EXTRACT_TAR
    assert af3.PARAMETERS_MOUNT_PATH not in data.argv
    assert af3.REFERENCE_MOUNT_PATH not in inference.argv


def test_candidate_freezes_in_the_academic_namespace_only() -> None:
    plan = compile_plan().controller_plan
    snapshot = academic_scheduling().freeze(
        service_class="customer-batch",
        model_id=af3.MODEL_ID,
        tenant_id="academic-poc",
        profile=profile(),
        plan=plan,
        workload_namespace=af3.EXECUTION_NAMESPACE,
    )
    data, inference = snapshot.stages
    assert data.resolved_local_queue == af3.DATA_LOCAL_QUEUE
    assert data.resolved_cluster_queue == af3.DATA_CLUSTER_QUEUE
    assert data.resolved_pool_preference == ("reference-data-cpu",)
    assert inference.resolved_local_queue == af3.INFERENCE_LOCAL_QUEUE
    assert inference.resolved_pool_preference == ("h100-reserved-8x", "h100-1x")
    with pytest.raises(SchedulingContractError, match="execution namespace differs"):
        academic_scheduling().freeze(
            service_class="customer-batch",
            model_id=af3.MODEL_ID,
            tenant_id="academic-poc",
            profile=profile(),
            plan=plan,
            workload_namespace="fs2-models",
        )


def test_only_verified_inner_fold_input_is_compiled() -> None:
    with pytest.raises(ScientificAdapterError, match="exactly one"):
        compile_plan(inputs=())
    outer = UUID(str(request()["input_manifest"]["artifact_id"]))  # type: ignore[index]
    with pytest.raises(ScientificAdapterError, match="distinct artifacts"):
        compile_plan(inputs=(verified_input(artifact_id=outer),))


def _inference_workspace(tmp_path: Path) -> Path:
    output = tmp_path / "outputs/pdl1-binder"
    output.mkdir(parents=True)
    (output / "pdl1-binder_model.cif").write_bytes(b"data_pdl1_binder\n#\nATOM 1 C CA A 1 0.0 0.0 0.0\n#\n")
    (output / "pdl1-binder_summary_confidences.json").write_text(json.dumps({"ranking_score": 0.87}), encoding="utf-8")
    receipt = load(RUNTIME_FIXTURES / "inference-stage-receipt.json")
    receipt.update(
        {
            "input_identity": {
                "artifact_id": "00000000-0000-4000-8000-000000000001",
                "sha256": "a" * 64,
            },
            "image": {
                "runtime_id": af3.MODEL_ID,
                "upstream_commit": af3.SOURCE_REVISION,
                "parameters_embedded": False,
                "reference_databases_embedded": False,
            },
            "status": "PASS",
            "execution": {
                "upstream": "/app/alphafold/run_alphafold.py",
                "exit_code": 0,
                "terminal_state": "succeeded",
            },
        }
    )
    (tmp_path / af3.INFERENCE_RECEIPT_FILENAME).write_text(json.dumps(receipt, separators=(",", ":")), encoding="utf-8")
    return tmp_path


def _data_workspace(tmp_path: Path, *, publish_receipt: bool = True) -> tuple[Path, bytes]:
    handoff = tmp_path / af3.DATA_OUTPUT_DIR / af3.HANDOFF_DIR_NAME
    payload = b'{"name":"pdl1-binder","sequences":[{"proteinChain":{"sequence":"ACDE"}}]}\n'
    relative = "pdl1-binder/pdl1-binder_data.json"
    payload_path = handoff / relative
    payload_path.parent.mkdir(parents=True)
    payload_path.write_bytes(payload)
    entry = {
        "fold_job": "pdl1-binder",
        "relative_path": relative,
        "bytes": len(payload),
        "sha256": hashlib.sha256(payload).hexdigest(),
    }
    index = {
        "schema": af3.HANDOFF_SCHEMA,
        "input_identity": {
            "artifact_id": "00000000-0000-4000-8000-000000000001",
            "sha256": "a" * 64,
        },
        "count": 1,
        "fold_jobs": ["pdl1-binder"],
        "entries": [entry],
        "paths_are_relative_to": "the directory containing this index",
    }
    index_bytes = (json.dumps(index, indent=2, sort_keys=True) + "\n").encode()
    (handoff / af3.HANDOFF_INDEX).write_bytes(index_bytes)
    receipt = load(RUNTIME_FIXTURES / "data-stage-receipt.json")
    receipt.update(
        {
            "input_identity": {
                "artifact_id": "00000000-0000-4000-8000-000000000001",
                "sha256": "a" * 64,
            },
            "image": {
                "runtime_id": af3.MODEL_ID,
                "upstream_commit": af3.SOURCE_REVISION,
                "parameters_embedded": False,
                "reference_databases_embedded": False,
            },
            "status": "PASS",
            "execution": {
                "upstream": "/app/alphafold/run_alphafold.py",
                "exit_code": 0,
                "terminal_state": "succeeded",
            },
            "handoff": {
                **index,
                "handoff_dirname": af3.HANDOFF_DIR_NAME,
                "index_sha256": hashlib.sha256(index_bytes).hexdigest(),
                "note": "portable fixture",
            },
        }
    )
    receipt_bytes = (json.dumps(receipt, sort_keys=True, separators=(",", ":")) + "\n").encode()
    if publish_receipt:
        (tmp_path / af3.DATA_RECEIPT_FILENAME).write_bytes(receipt_bytes)
    return tmp_path, receipt_bytes


def test_result_collector_is_registered_once_in_the_global_companion_registry(tmp_path: Path) -> None:
    invocation = compile_plan().invocation(af3.INFERENCE_STAGE_ID, "main")
    collected = collect_stage_output(invocation, _inference_workspace(tmp_path / "result"))
    assert [item.name for item in collected.artifacts] == ["structure", "summary-confidence"]
    assert collected.validation["status"] == "passed"
    assert collected.validation["validator_id"] == af3.VALIDATOR_ID
    with pytest.raises(CollectionPendingError):
        collect_stage_output(compile_plan().invocation(af3.DATA_STAGE_ID, "main"), tmp_path / "pending")


def test_data_collector_waits_for_atomic_terminal_receipt_publication(tmp_path: Path) -> None:
    invocation = compile_plan().invocation(af3.DATA_STAGE_ID, "main")
    workspace, receipt = _data_workspace(tmp_path / "data", publish_receipt=False)
    partial = workspace / f".{af3.DATA_RECEIPT_FILENAME}.writer.partial"
    partial.write_bytes(receipt[: len(receipt) // 2])
    with pytest.raises(CollectionPendingError):
        collect_stage_output(invocation, workspace)

    partial.write_bytes(receipt)
    os.replace(partial, workspace / af3.DATA_RECEIPT_FILENAME)
    collected = collect_stage_output(invocation, workspace)
    assert collected.validation["status"] == "passed"
    assert collected.artifacts[0].path.stat().st_size <= companion._MAX_ARCHIVE_BYTES
    (workspace / af3.DATA_RECEIPT_FILENAME).write_bytes(b'{"terminal":"invalid"}\n')
    with pytest.raises(ScientificAdapterError, match="terminal PASS"):
        collect_stage_output(invocation, workspace)


def test_af3_handoff_package_limit_matches_the_materializer_and_has_safe_headroom() -> None:
    assert af3.MAX_HANDOFF_BYTES == companion._MAX_ARCHIVE_BYTES == 256 * 1024 * 1024
    assert af3.MAX_HANDOFF_CONTENT_BYTES == 255 * 1024 * 1024
    assert af3.MAX_HANDOFF_BYTES - af3.MAX_HANDOFF_CONTENT_BYTES >= ((af3.MAX_HANDOFF_FILES + 1) * 1024 + 10 * 1024)


def test_profile_does_not_accept_caller_supplied_license_receipts() -> None:
    invalid = copy.deepcopy(request())
    parameters = invalid["parameters"]
    assert isinstance(parameters, dict)
    parameters["license_receipt"] = "caller-controlled"
    with pytest.raises(ScientificAdapterError):
        compile_adapter_run(
            af3.MODEL_ID,
            profile(),
            invalid,
            operation_id="op-af3-caller-license",
            variant_id=af3.VARIANT_ID,
            access_context=ArtifactAccessContext(
                profile="academic",
                receipt_digest="sha256:" + "f" * 64,
                tenant_id="academic-poc",
            ),
            input_artifacts=(verified_input(),),
        )
