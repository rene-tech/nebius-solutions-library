"""Fail-closed contracts for the isolated academic AlphaFold 3 candidate."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from uuid import UUID

import pytest

from fs2_serve.scientific_batch import (
    ArtifactAccessContext,
    MaterializationMode,
    ResourceClass,
    ScientificAdapterError,
    ScientificInputArtifact,
    compile_adapter_run,
)
from fs2_serve.scientific_batch.adapters import CollectionPendingError, collect_stage_output
from fs2_serve.scientific_batch.adapters import alphafold3 as af3
from fs2_serve.scientific_batch.adapters.production_registry import install_production_adapters
from fs2_serve.scientific_batch.scheduling import SchedulingContractError, SchedulingContractResolver

SOLUTION_ROOT = Path(__file__).resolve().parents[3]
ADAPTER_ROOT = SOLUTION_ROOT / "models/structure/batch-adapters/alphafold3"
RUNTIME_FIXTURES = SOLUTION_ROOT / "models/cancer-immunotherapy/images/alphafold3/fixtures"

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
            "model_eligible_pool_ids": {af3.MODEL_ID: ["h100-1x", "h100-reserved-8x"]},
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
    assert candidate["state"] == "candidate-unqualified"
    assert candidate["route_exposed"] is False
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


def test_result_collector_is_registered_once_in_the_global_companion_registry(tmp_path: Path) -> None:
    invocation = compile_plan().invocation(af3.INFERENCE_STAGE_ID, "main")
    collected = collect_stage_output(invocation, _inference_workspace(tmp_path / "result"))
    assert [item.name for item in collected.artifacts] == ["structure", "summary-confidence"]
    assert collected.validation["status"] == "passed"
    assert collected.validation["validator_id"] == af3.VALIDATOR_ID
    with pytest.raises(CollectionPendingError):
        collect_stage_output(compile_plan().invocation(af3.DATA_STAGE_ID, "main"), tmp_path / "pending")


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
