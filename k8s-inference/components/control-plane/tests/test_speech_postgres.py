"""PostgreSQL live-connection ownership under concurrent gateway replicas."""

import asyncio

import pytest
from test_postgres_integration import add_token, postgres_store, request  # noqa: F401, F811 - pytest fixture

from fs2_serve.models import OperationStatus

pytestmark = pytest.mark.postgres


async def test_only_specific_stream_executor_can_claim_live_operation(postgres_store):  # noqa: F811
    store = postgres_store
    principal = await add_token(store)
    live = await store.append_operation(
        principal=principal,
        admission=request("live-postgres-ownership").model_copy(update={"protocol": "speech-stream-v1"}),
        model_revision="test", reserved_gpu_seconds=5, max_attempts=1,
    )
    ordinary = await store.append_operation(
        principal=principal, admission=request("file-postgres-ownership"),
        model_revision="test", reserved_gpu_seconds=5, max_attempts=2,
    )
    assert await store.claim_operation("wrong-stream", lease_seconds=5, stream_operation_id=ordinary.id) is None
    assert (await store.claim_operation("http-worker", lease_seconds=5)).id == ordinary.id
    assert await store.claim_operation("http-worker-2", lease_seconds=5) is None
    results = await asyncio.gather(*[
        store.claim_operation(f"socket-{i}", lease_seconds=5, stream_operation_id=live.id) for i in range(6)
    ])
    claims = [item for item in results if item is not None]
    assert len(claims) == 1 and claims[0].id == live.id and claims[0].attempt == 1
    async with store.pool.acquire() as connection:
        await connection.execute(
            "UPDATE fs2_operations SET lease_expires_at=clock_timestamp()-interval '1 second' WHERE id=$1", live.id,
        )
    await store.reap_stale_operations()
    ended = await store.get_operation(live.id, tenant_id=principal.tenant_id)
    assert ended.status.terminal and ended.status is not OperationStatus.SUCCEEDED
    assert await store.claim_operation("reconnect", lease_seconds=5, stream_operation_id=live.id) is None


async def test_revoked_key_cannot_start_queued_stream(postgres_store):  # noqa: F811
    store = postgres_store
    principal = await add_token(store)
    live = await store.append_operation(
        principal=principal,
        admission=request("revoked-live-ownership").model_copy(update={"protocol": "speech-stream-v1"}),
        model_revision="test", reserved_gpu_seconds=5, max_attempts=1,
    )
    await store.revoke_token(principal.token_id, actor="test-admin")
    assert await store.claim_operation("socket", lease_seconds=5, stream_operation_id=live.id) is None
