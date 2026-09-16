from __future__ import annotations

from types import SimpleNamespace

import pytest

from fs2_serve import cli


@pytest.mark.asyncio
async def test_maintenance_purges_artifacts_before_database_retention(monkeypatch: pytest.MonkeyPatch) -> None:
    events: list[object] = []
    backlog = iter([("request_debug", "request_telemetry"), ()])

    class FakeStore:
        pool = object()

        async def purge_expired_payloads(self, *, batch_size: int = 100) -> int:
            events.append(("payloads", batch_size))
            return 1

        async def delete_expired_rows(self, **kwargs: int) -> dict[str, int]:
            events.append(("rows", kwargs))
            return {}

        async def expired_retention_backlog(self, **kwargs: int) -> tuple[str, ...]:
            events.append(("backlog", kwargs))
            return next(backlog)

        async def close(self) -> None:
            events.append("closed")

    class FakeArtifacts:
        async def purge_expired(self) -> list[object]:
            events.append("artifacts")
            return []

    store = FakeStore()

    async def connect(_database_url: str) -> FakeStore:
        return store

    monkeypatch.setattr(cli.PostgresMaintenanceStore, "connect", connect)
    monkeypatch.setattr(cli, "_artifact_service", lambda _settings, _repository: FakeArtifacts())
    settings = SimpleNamespace(
        database_url="postgresql://unit.invalid/database",
        operation_retention_seconds=1,
        pat_retention_seconds=2,
        audit_retention_seconds=3,
        usage_retention_seconds=4,
        request_debug_retention_seconds=5,
        request_telemetry_retention_seconds=6,
        retention_batch_size=1000,
        retention_max_batches=10,
    )

    await cli.maintain(settings)

    assert events == [
        "artifacts",
        ("payloads", 1000),
        (
            "rows",
            {
                "operation_retention_seconds": 1,
                "token_retention_seconds": 2,
                "audit_retention_seconds": 3,
                "usage_retention_seconds": 4,
                "request_debug_retention_seconds": 5,
                "request_telemetry_retention_seconds": 6,
                "batch_size": 1000,
            },
        ),
        (
            "backlog",
            {
                "operation_retention_seconds": 1,
                "token_retention_seconds": 2,
                "audit_retention_seconds": 3,
                "usage_retention_seconds": 4,
                "request_debug_retention_seconds": 5,
                "request_telemetry_retention_seconds": 6,
            },
        ),
        ("payloads", 1000),
        (
            "rows",
            {
                "operation_retention_seconds": 1,
                "token_retention_seconds": 2,
                "audit_retention_seconds": 3,
                "usage_retention_seconds": 4,
                "request_debug_retention_seconds": 5,
                "request_telemetry_retention_seconds": 6,
                "batch_size": 1000,
            },
        ),
        (
            "backlog",
            {
                "operation_retention_seconds": 1,
                "token_retention_seconds": 2,
                "audit_retention_seconds": 3,
                "usage_retention_seconds": 4,
                "request_debug_retention_seconds": 5,
                "request_telemetry_retention_seconds": 6,
            },
        ),
        "closed",
    ]


@pytest.mark.asyncio
async def test_maintenance_fails_after_bounded_batches_when_retention_cannot_converge(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0
    closed = False

    class FakeStore:
        pool = object()

        async def purge_expired_payloads(self, *, batch_size: int = 100) -> int:
            assert batch_size == 250
            return batch_size

        async def delete_expired_rows(self, **kwargs: int) -> dict[str, int]:
            nonlocal calls
            calls += 1
            assert kwargs["batch_size"] == 250
            return {"request_debug": 250, "request_telemetry": 250}

        async def expired_retention_backlog(self, **_kwargs: int) -> tuple[str, ...]:
            return ("request_debug", "request_telemetry")

        async def close(self) -> None:
            nonlocal closed
            closed = True

    async def connect(_database_url: str) -> FakeStore:
        return FakeStore()

    monkeypatch.setattr(cli.PostgresMaintenanceStore, "connect", connect)
    monkeypatch.setattr(cli, "_artifact_service", lambda _settings, _repository: None)
    settings = SimpleNamespace(
        database_url="postgresql://unit.invalid/database",
        operation_retention_seconds=1,
        pat_retention_seconds=2,
        audit_retention_seconds=3,
        usage_retention_seconds=4,
        request_debug_retention_seconds=5,
        request_telemetry_retention_seconds=7776000,
        retention_batch_size=250,
        retention_max_batches=3,
    )

    with pytest.raises(RuntimeError, match="retention backlog remains after 3 bounded batches"):
        await cli.maintain(settings)

    assert calls == 3
    assert closed is True
