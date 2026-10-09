"""Long native continuations must not turn input manifests into HTTP headers."""

from dataclasses import replace
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException
from scientific_batch_fakes import FakeScientificBatchRepository
from test_gromacs_adapter import many_file_continuation_state

from fs2_serve.scientific_batch.capability import ScientificWorkloadCapabilityAuthority
from fs2_serve.scientific_batch.models import ScientificAttemptState, WorkloadKind, WorkloadRef
from fs2_serve.scientific_batch.workload_routes import authorize_workload_capability


def active_workload(count):
    state = many_file_continuation_state(count)
    invocation = state.execution_plan.invocations[0]
    attempt = ScientificAttemptState(
        attempt_id=uuid4(),
        stage_id=invocation.stage_id,
        shard_id=invocation.shard_id,
        attempt_number=1,
        workload=WorkloadRef(namespace="fs2-models", name="continuation", kind=WorkloadKind.JOB),
    )
    state = replace(state, stages=(replace(state.stages[0], attempts=(attempt,)),))
    # The issuer needs only these immutable resource identities. Actual Job
    # rendering and HTTP transport are also exercised by verify_resume.py.
    resource = SimpleNamespace(
        operation_id=state.operation_id,
        batch_id=state.batch_id,
        workload_id=state.workload_id,
        attempt_id=attempt.attempt_id,
        attempt_number=1,
        tenant_id=state.tenant_id,
        model_id=state.model_id,
        variant_id=state.variant_id,
        stage_id=invocation.stage_id,
        shard_id=invocation.shard_id,
        invocation=invocation,
        materializations=state.input_manifest.entries,
        access_context=state.access_context,
    )
    repository = FakeScientificBatchRepository()
    repository.records[state.operation_id] = state
    return state, resource, repository


@pytest.mark.asyncio
@pytest.mark.parametrize("count", [1, 65, 305, 1000])
async def test_capability_header_is_bounded_and_rehydrates_exact_native_inputs(hasher, count):
    state, resource, repository = active_workload(count)
    authority = ScientificWorkloadCapabilityAuthority(hasher)
    token = authority.issue(resource)
    assert len(token) < 2000
    decoded = authority.verify(token)
    assert (decoded.artifacts_digest is not None) == (count > 1)
    assert len(decoded.artifacts) == (1 if count == 1 else 0)
    capability, admitted, attempt = await authorize_workload_capability(authority, repository, "Bearer " + token)
    assert admitted == state
    assert attempt.attempt_id == resource.attempt_id
    assert len(capability.artifacts) == count
    assert [item.artifact_id for item in capability.artifacts] == [
        item.artifact_id for item in state.input_manifest.entries
    ]
    assert [item.digest for item in capability.artifacts] == [item.digest for item in state.input_manifest.entries]


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["digest", "artifact_id", "size", "order", "attempt", "tenant", "cancel"])
async def test_compact_capability_cannot_outlive_or_widen_its_original_binding(hasher, change):
    state, resource, repository = active_workload(305)
    authority = ScientificWorkloadCapabilityAuthority(hasher)
    token = authority.issue(resource)
    entries = state.input_manifest.entries
    if change in {"digest", "artifact_id", "size"}:
        update = {
            "digest": {"digest": "sha256:" + "c" * 64},
            "artifact_id": {"artifact_id": uuid4()},
            "size": {"size_bytes": 99},
        }[change]
        state = replace(
            state, input_manifest=replace(state.input_manifest, entries=(replace(entries[0], **update),) + entries[1:])
        )
    elif change == "order":
        invocation = state.execution_plan.invocations[0]
        state = replace(
            state,
            execution_plan=replace(
                state.execution_plan,
                invocations=(replace(invocation, materializations=tuple(reversed(invocation.materializations))),),
            ),
        )
    elif change == "attempt":
        stage = state.stages[0]
        state = replace(state, stages=(replace(stage, attempts=(replace(stage.attempts[0], attempt_id=uuid4()),)),))
    elif change == "tenant":
        repository.records.clear()
    else:
        state = replace(state, cancel_requested=True)
    if change != "tenant":
        repository.records[state.operation_id] = state
    with pytest.raises(HTTPException) as denied:
        await authorize_workload_capability(authority, repository, "Bearer " + token)
    assert denied.value.status_code in {401, 409}


def test_compact_capability_is_still_authenticated(hasher):
    _, resource, _ = active_workload(305)
    authority = ScientificWorkloadCapabilityAuthority(hasher)
    token = authority.issue(resource)
    tampered = token[:-1] + ("0" if token[-1] != "0" else "1")
    with pytest.raises(ValueError, match="invalid"):
        authority.verify(tampered)
