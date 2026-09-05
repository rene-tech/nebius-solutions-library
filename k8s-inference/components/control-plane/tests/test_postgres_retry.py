from __future__ import annotations

import inspect

import asyncpg
import pytest

from fs2_serve.postgres_retry import retry_serialization
from fs2_serve.scientific_batch.postgres_repository import PostgresScientificBatchRepository


@pytest.mark.asyncio
async def test_retry_serialization_retries_deadlocks_and_serialization_aborts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr("fs2_serve.postgres_retry.asyncio.sleep", fake_sleep)

    class Subject:
        def __init__(self) -> None:
            self.attempts = 0

        @retry_serialization
        async def run(self) -> str:
            self.attempts += 1
            if self.attempts == 1:
                raise asyncpg.DeadlockDetectedError("deadlock")
            if self.attempts == 2:
                raise asyncpg.SerializationError("serialization")
            return "ok"

    subject = Subject()
    assert await subject.run() == "ok"
    assert subject.attempts == 3
    assert sleeps == [0.01, 0.02]


def test_scientific_admission_uses_retry_and_nonexclusive_operation_lock() -> None:
    source = inspect.getsource(PostgresScientificBatchRepository.create)
    assert "@retry_serialization" in source
    assert "FOR KEY SHARE" in source
    assert "fs2_operations WHERE id=$1 FOR UPDATE" not in source
