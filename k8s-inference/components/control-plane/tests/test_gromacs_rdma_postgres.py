"""The optional RDMA contract survives the actual PostgreSQL state boundary."""

import json
from uuid import uuid4

import pytest
from test_gromacs_adapter import source
from test_gromacs_execution_shapes import mpi_request
from test_gromacs_rdma import RDMA, rdma_runtime
from test_scientific_batch_postgres_state import TENANT, durable_input_artifact, principal_of
from test_scientific_batch_postgres_state import store as store  # noqa: F401

from fs2_serve.models import AdmissionRequest
from fs2_serve.scientific_batch.models import ArtifactAccessContext, VerifiedInputManifest
from fs2_serve.scientific_batch.placement import execution_resource_envelope
from fs2_serve.scientific_batch.postgres_repository import PostgresScientificBatchRepository
from fs2_serve.scientific_batch.scheduling import SchedulingContractResolver


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_postgres_reopens_exact_rdma_attempt_without_mutable_catalog(store, tmp_path, monkeypatch):  # noqa: F811
    runtime, profile, _, contract = rdma_runtime(tmp_path, monkeypatch)
    principal = await principal_of(store, model_id="gromacs-mpi")
    body = mpi_request(2, 8)
    operation = await store.append_operation(
        principal=principal,
        admission=AdmissionRequest(
            model_id="gromacs-mpi",
            operation="run-workflow",
            protocol="scientific-batch-v1",
            idempotency_key="mpi-rdma-shape",
            request_body=json.dumps(body).encode(),
        ),
        model_revision=profile.model_revision,
        reserved_gpu_seconds=0,
        max_attempts=1,
    )
    artifact = uuid4()
    await durable_input_artifact(store, operation.id, artifact_id=artifact)
    access = ArtifactAccessContext(profile="public", receipt_digest=None, tenant_id=TENANT)
    plan = runtime.plan(profile, body, operation_id=operation.id, access_context=access, input_artifacts=(source(),))
    policy = SchedulingContractResolver(
        contract,
        stage_shape_resources={
            key: execution_resource_envelope(value) for key, value in runtime.execution_shapes.items()
        },
    )
    snapshot = policy.freeze(
        service_class="customer-batch",
        model_id="gromacs-mpi",
        tenant_id=TENANT,
        profile=profile.value,
        plan=plan.controller_plan,
    )
    arguments = dict(
        operation_id=operation.id,
        tenant_id=TENANT,
        model_id="gromacs-mpi",
        variant_id=plan.variant_id,
        input_artifact_id=artifact,
        plan=plan.controller_plan,
        scheduling=snapshot,
        execution_plan=plan,
        access_context=access,
        input_manifest=VerifiedInputManifest("inputs", artifact, "sha256:" + "a" * 64, (source(),)),
    )
    repository = PostgresScientificBatchRepository(store.pool)
    admitted = await repository.create(**arguments)
    runtime.executions.clear()
    runtime.execution_shapes.clear()
    fresh = PostgresScientificBatchRepository(store.pool)
    reopened = await fresh.get(operation.id, tenant_id=TENANT)
    assert reopened == admitted
    assert reopened.plan.stages[0].execution_shape.rdma.to_value() == RDMA
    assert reopened.execution_plan.stage_bindings[0].rdma.to_value() == RDMA
    assert reopened.scheduling.stages[0].accelerator_count == 8
    assert (
        dict(reopened.scheduling.stages[0].node_selector)["topology.nebius.com/gpu-cluster-id"]
        == RDMA["gpu_cluster_id"]
    )
    assert await fresh.create(**arguments) == admitted
