"""Large native manifests survive actual outbox and immutable SQL transitions."""

import hashlib
import json
from dataclasses import replace
from uuid import uuid4

import pytest
from test_gromacs_adapter import source
from test_gromacs_execution_shapes import compile_and_freeze, mpi_request
from test_scientific_batch_postgres_state import TENANT, durable_input_artifact, principal_of
from test_scientific_batch_postgres_state import store as store  # noqa: F401

from fs2_serve.models import AdmissionRequest
from fs2_serve.scientific_batch.codec import COMPACT_METADATA_SCHEMA, state_from_value, state_to_value
from fs2_serve.scientific_batch.models import ArtifactAccessContext, ScientificBatchState, VerifiedInputManifest
from fs2_serve.scientific_batch.postgres_repository import PostgresScientificBatchRepository


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_twenty_thousand_unique_files_outbox_reopen_cancel_and_sql_immutability(store, tmp_path):  # noqa: F811
    runtime, profile, _, _, policy = compile_and_freeze(tmp_path, 1, 1)
    principal = await principal_of(store, model_id="gromacs-mpi")
    body = mpi_request(1, 1)
    body["parameters"].update(
        max_wall_seconds=1209600,
        continuation_files=[
            {"input_id": f"resume-{index:05d}", "path": f"md.part{index:05d}.xtc"}
            for index in range(20000)
        ],
    )
    entries = tuple(
        replace(
            source(), logical_artifact_id=f"resume-{index:05d}", artifact_id=uuid4(),
            semantic_type="gromacs-continuation-file/v1", media_type="application/octet-stream",
            compression=None, digest="sha256:" + hashlib.sha256(f"file-{index}".encode()).hexdigest(),
        )
        for index in range(20000)
    )
    artifact = uuid4()
    access = ArtifactAccessContext(profile="public", receipt_digest=None, tenant_id=TENANT)

    def freeze(operation):
        plan = runtime.plan(
            profile, body, operation_id=operation.id, access_context=access, input_artifacts=entries,
        )
        plan = replace(plan, stage_bindings=tuple(
            replace(binding, active_deadline_seconds=1211400) for binding in plan.stage_bindings
        ))
        snapshot = policy.freeze(
            service_class="customer-batch", model_id="gromacs-mpi", tenant_id=TENANT,
            profile=profile.value, plan=plan.controller_plan,
        )
        return state_to_value(ScientificBatchState.admit(
            operation_id=operation.id, tenant_id=TENANT, model_id="gromacs-mpi", variant_id=plan.variant_id,
            input_artifact_id=artifact, plan=plan.controller_plan, scheduling=snapshot, execution_plan=plan,
            access_context=access,
            input_manifest=VerifiedInputManifest("large-inputs", artifact, "sha256:" + "a" * 64, entries),
        ))

    operation = await store.append_operation(
        principal=principal,
        admission=AdmissionRequest(
            model_id="gromacs-mpi", operation="run-workflow", protocol="scientific-batch-v1",
            idempotency_key="large-longrun-postgres-0001", request_body=json.dumps(body).encode(),
        ),
        model_revision=profile.model_revision, reserved_gpu_seconds=0, max_attempts=1,
        scientific_admission_factory=freeze,
    )
    pending = await store.get_scientific_admission(operation.id)
    assert pending.payload["input_manifest"]["encoding"] == COMPACT_METADATA_SCHEMA
    frozen = state_from_value(pending.payload)
    await durable_input_artifact(store, operation.id, artifact_id=artifact)
    repository = PostgresScientificBatchRepository(store.pool)
    admitted = await repository.create(
        operation_id=operation.id, tenant_id=TENANT, model_id=frozen.model_id, variant_id=frozen.variant_id,
        input_artifact_id=artifact, plan=frozen.plan, scheduling=frozen.scheduling,
        execution_plan=frozen.execution_plan, access_context=frozen.access_context,
        input_manifest=frozen.input_manifest, runtime_artifacts=frozen.runtime_artifacts,
    )
    await store.complete_scientific_admission(operation.id)
    reopened = await PostgresScientificBatchRepository(store.pool).get(operation.id, tenant_id=TENANT)
    assert reopened == admitted == frozen
    cancelled = await repository.request_cancel(operation.id, tenant_id=TENANT, actor=principal.principal_id)
    assert cancelled.cancel_requested
    value = state_to_value(cancelled)
    assert value["input_manifest"] == pending.payload["input_manifest"]
    assert value["adapter_execution"] == pending.payload["adapter_execution"]
