"""Persistence through the actual PostgreSQL operation/batch repository boundary."""

from __future__ import annotations

import json
from uuid import uuid4

import pytest
from test_gromacs_adapter import source
from test_gromacs_execution_shapes import compile_and_freeze, mpi_request
from test_scientific_batch_postgres_state import TENANT, durable_input_artifact, principal_of
from test_scientific_batch_postgres_state import store as store  # noqa: F401 - imported pytest fixture

from fs2_serve.models import AdmissionRequest
from fs2_serve.scientific_batch.models import ArtifactAccessContext, VerifiedInputManifest
from fs2_serve.scientific_batch.postgres_repository import PostgresScientificBatchRepository


@pytest.mark.postgres
@pytest.mark.asyncio
@pytest.mark.parametrize("nodes", [1, 2])
async def test_real_postgres_freezes_shape_and_replay_after_catalog_refresh(store, tmp_path, nodes):  # noqa: F811
    runtime, profile, _, _, policy = compile_and_freeze(tmp_path, nodes, 8)
    principal = await principal_of(store, model_id="gromacs-mpi")
    body = mpi_request(nodes, 8)
    operation = await store.append_operation(
        principal=principal,
        admission=AdmissionRequest(
            model_id="gromacs-mpi",
            operation="run-workflow",
            protocol="scientific-batch-v1",
            idempotency_key=f"mpi-shape-{nodes}x8",
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
    # Reopening uses persisted data even when the process has lost the mutable map.
    runtime.executions.clear()
    runtime.execution_shapes.clear()
    fresh_repository = PostgresScientificBatchRepository(store.pool)
    reopened = await fresh_repository.get(operation.id, tenant_id=TENANT)
    assert reopened == admitted
    assert reopened.plan.stages[0].execution_shape.accelerator_count == 8
    assert reopened.scheduling.stages[0].accelerator_count == 8
    assert reopened.plan.stages[0].gang_size == (None if nodes == 1 else 2)
    assert reopened.execution_plan.stage_bindings[0].request_cpu == "64000m"
    assert reopened.execution_plan.stage_bindings[0].request_memory == "128Gi"
    assert await fresh_repository.create(**arguments) == admitted
