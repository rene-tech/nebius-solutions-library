from __future__ import annotations

from types import SimpleNamespace

import pytest

from fs2_serve import cli


@pytest.mark.asyncio
async def test_maintenance_purges_artifacts_before_database_retention(monkeypatch: pytest.MonkeyPatch) -> None:
    events: list[object] = []

    class FakeStore:
        pool = object()

        async def purge_expired_payloads(self) -> int:
            events.append("payloads")
            return 1

        async def delete_expired_rows(self, **kwargs: int) -> dict[str, int]:
            events.append(("rows", kwargs))
            return {}

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
    )

    await cli.maintain(settings)

    assert events == [
        "artifacts",
        "payloads",
        (
            "rows",
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
