from __future__ import annotations

from types import SimpleNamespace

import pytest

from fs2_serve import cli


@pytest.mark.asyncio
async def test_maintenance_purges_artifacts_before_database_retention(monkeypatch: pytest.MonkeyPatch) -> None:
    events: list[object] = []
    database_backlog = iter([("request_debug", "request_telemetry"), ()])
    artifact_backlog = iter([("scientific_artifacts",), ()])

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
            return next(database_backlog)

        async def close(self) -> None:
            events.append("closed")

    class FakeArtifacts:
        async def purge_expired(self, *, limit: int) -> list[object]:
            events.append(("artifacts", limit))
            return [object()]

        async def retention_backlog(self) -> tuple[str, ...]:
            events.append("artifact-backlog")
            return next(artifact_backlog)

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
        ("artifacts", 1000),
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
        "artifact-backlog",
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
        ("artifacts", 1000),
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
        "artifact-backlog",
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


@pytest.mark.asyncio
async def test_maintenance_fails_when_bounded_artifact_drain_does_not_converge(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifact_batches = 0

    class FakeStore:
        pool = object()

        async def purge_expired_payloads(self, *, batch_size: int) -> int:
            return 0

        async def delete_expired_rows(self, **_kwargs: int) -> dict[str, int]:
            return {}

        async def expired_retention_backlog(self, **_kwargs: int) -> tuple[str, ...]:
            return ()

        async def close(self) -> None:
            return None

    class FakeArtifacts:
        async def purge_expired(self, *, limit: int) -> list[object]:
            nonlocal artifact_batches
            assert limit == 50
            artifact_batches += 1
            return [object()] * limit

        async def retention_backlog(self) -> tuple[str, ...]:
            return ("scientific_artifacts",)

    async def connect(_database_url: str) -> FakeStore:
        return FakeStore()

    monkeypatch.setattr(cli.PostgresMaintenanceStore, "connect", connect)
    monkeypatch.setattr(cli, "_artifact_service", lambda _settings, _repository: FakeArtifacts())
    settings = SimpleNamespace(
        database_url="postgresql://unit.invalid/database",
        operation_retention_seconds=1,
        pat_retention_seconds=2,
        audit_retention_seconds=3,
        usage_retention_seconds=4,
        request_debug_retention_seconds=5,
        request_telemetry_retention_seconds=7776000,
        retention_batch_size=50,
        retention_max_batches=4,
    )

    with pytest.raises(RuntimeError, match="scientific_artifacts"):
        await cli.maintain(settings)

    assert artifact_batches == 4
