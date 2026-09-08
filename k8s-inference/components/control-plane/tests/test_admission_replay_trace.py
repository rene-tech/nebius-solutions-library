"""A new transport trace must not rewrite an idempotently accepted workload."""

from __future__ import annotations

from contextlib import nullcontext

import pytest
from opentelemetry.trace import NonRecordingSpan, SpanContext, TraceFlags, use_span
from test_admission_workers import request, service, setup_principal
from test_postgres_integration import postgres_store  # noqa: F401 - shared isolated database fixture.

from fs2_serve.lifecycle import (
    LifecyclePhase,
    MemoryLifecycleRepository,
    PostgresLifecycleRepository,
    trace_identity,
)
from fs2_serve.memory_store import MemoryStore
from fs2_serve.runtime import StubRuntimeClient

FIRST_TRACE = "00-" + "1" * 32 + "-" + "2" * 16 + "-01"
REPLAY_TRACE = "00-" + "3" * 32 + "-" + "4" * 16 + "-01"
SAME_TRACE_NEW_PARENT = "00-" + "1" * 32 + "-" + "5" * 16 + "-01"
EXPLICIT_FIRST = "00-" + "6" * 32 + "-" + "7" * 16 + "-01"
EXPLICIT_REPLAY = "00-" + "8" * 32 + "-" + "9" * 16 + "-01"


def active_trace(traceparent):
    if traceparent is None:
        return nullcontext()
    trace_id, span_id = trace_identity(traceparent)
    assert trace_id is not None and span_id is not None
    return use_span(
        NonRecordingSpan(
            SpanContext(
                trace_id=int(trace_id, 16),
                span_id=int(span_id, 16),
                is_remote=False,
                trace_flags=TraceFlags(TraceFlags.SAMPLED),
            )
        ),
        end_on_exit=False,
    )


async def check_replay(
    registry,
    store,
    lifecycle,
    *,
    first_active=None,
    replay_active=None,
    first_explicit=FIRST_TRACE,
    replay_explicit=REPLAY_TRACE,
):
    principal = await setup_principal(store)
    admission = service(registry, store, StubRuntimeClient())
    admission.lifecycle = lifecycle
    original_request = request("trace-replay-durable-operation-0001")
    with active_trace(first_active):
        original = await admission.admit(
            principal,
            original_request.model_copy(update={"traceparent": first_explicit}),
        )
    before = await lifecycle.get_workload(original.id, tenant_id=principal.tenant_id)
    assert before is not None
    assert not original.reused
    expected_trace = first_active or first_explicit
    assert original.traceparent == expected_trace
    assert (before.subject.trace_id, before.subject.parent_span_id) == trace_identity(expected_trace)

    with active_trace(replay_active):
        replay = await admission.admit(
            principal,
            original_request.model_copy(update={"traceparent": replay_explicit}),
        )

    assert replay.reused and replay.id == original.id
    assert replay.traceparent == original.traceparent
    assert replay.accepted_at == original.accepted_at
    assert replay.model_id == original.model_id
    assert replay.model_revision == original.model_revision
    assert replay.idempotency_key == original.idempotency_key
    after = await lifecycle.get_workload(original.id, tenant_id=principal.tenant_id)
    assert after == before
    subjects = await lifecycle.list_workloads(
        tenant_id=principal.tenant_id, model_id="qwen3-8b", operation_id=None, limit=10
    )
    assert subjects.total == 1
    assert subjects.items[0].subject.subject_id == original.id
    assert len(after.signals) == 2
    assert {signal.phase for signal in after.signals} == {LifecyclePhase.RECEIVE, LifecyclePhase.ENQUEUE}
    assert {signal.event_key for signal in after.signals} == {
        f"online:{original.id}:receive",
        f"online:{original.id}:enqueue",
    }
    # A real identity change must still fail; the fix is not permission to
    # overwrite lifecycle facts or suppress subject validation errors.
    with pytest.raises(ValueError, match="identity.*different facts"):
        await lifecycle.register_subject(before.subject.model_copy(update={"trace_id": "f" * 32}))
    assert await lifecycle.get_workload(original.id, tenant_id=principal.tenant_id) == before
    return original


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "changes",
    [
        {},
        {"replay_explicit": SAME_TRACE_NEW_PARENT},
        {
            "first_active": FIRST_TRACE,
            "replay_active": REPLAY_TRACE,
            "first_explicit": EXPLICIT_FIRST,
            "replay_explicit": EXPLICIT_REPLAY,
        },
        {"replay_active": REPLAY_TRACE},
        {"first_active": FIRST_TRACE, "first_explicit": EXPLICIT_FIRST},
        {"first_explicit": None, "replay_active": REPLAY_TRACE},
    ],
    ids=(
        "explicit-traces",
        "same-trace-new-parent",
        "active-overrides-explicit",
        "explicit-to-active",
        "active-to-explicit",
        "untraced-original-stays-untraced",
    ),
)
async def test_replay_preserves_original_trace_and_single_lifecycle_subject(registry, cipher, hasher, changes):
    await check_replay(registry, MemoryStore(cipher, hasher), MemoryLifecycleRepository(), **changes)


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_postgres_replay_with_new_active_trace_keeps_one_durable_operation_and_ledger(
    registry,
    postgres_store,  # noqa: F811 - pytest fixture injection.
):
    operation = await check_replay(
        registry,
        postgres_store,
        PostgresLifecycleRepository(postgres_store.pool),
        first_active=FIRST_TRACE,
        replay_active=REPLAY_TRACE,
        first_explicit=EXPLICIT_FIRST,
        replay_explicit=EXPLICIT_REPLAY,
    )
    assert await postgres_store.pool.fetchval("SELECT count(*) FROM fs2_operations") == 1
    assert await postgres_store.pool.fetchval("SELECT count(*) FROM fs2_telemetry_subjects") == 1
    assert await postgres_store.pool.fetchval("SELECT count(*) FROM fs2_lifecycle_signals") == 2
    stored_trace = await postgres_store.pool.fetchval(
        "SELECT traceparent FROM fs2_operations WHERE id=$1", operation.id
    )
    assert stored_trace == FIRST_TRACE
