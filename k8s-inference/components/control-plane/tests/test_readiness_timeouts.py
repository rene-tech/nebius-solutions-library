"""Readiness remains bounded and dependency-specific under stalled I/O."""

import asyncio
import time
from unittest.mock import AsyncMock

import pytest
from test_api_mcp import TestClient, build_runtime, publish_controller

from fs2_serve.api import create_app


@pytest.mark.parametrize(
    "dependency,code",
    [
        ("database", "database_unavailable"),
        ("activation", "activation_controller_unavailable"),
        ("federation", "federation_unavailable"),
    ],
)
@pytest.mark.parametrize("failure", ["stall", "exception"])
def test_readyz_bounds_dependency_failure_and_recovers(
    registry, cipher, hasher, monkeypatch, dependency, code, failure
):
    runtime = build_runtime(registry, cipher, hasher)
    runtime.settings.readiness_dependency_timeout_seconds = 0.02
    publish_controller(runtime)
    cancelled = []

    async def broken(*args):
        if failure == "exception":
            raise RuntimeError("PRIVATE_DEPENDENCY_DETAIL_MUST_NOT_LEAK")
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.append(True)
            raise

    target, attribute, ready_value = {
        "database": (runtime.store, "ping", True),
        "activation": (runtime.store, "activation_controller_ready", True),
        "federation": (runtime.admission.runtime, "federation_health", {"ready": True, "routes": 0, "circuits": {}}),
    }[dependency]
    monkeypatch.setattr(target, attribute, broken)
    with TestClient(create_app(runtime)) as client:
        started = time.monotonic()
        failed = client.get("/readyz")
        assert time.monotonic() - started < 1
        assert failed.status_code == 503
        assert failed.json()["error"]["type"] == code
        assert "PRIVATE_DEPENDENCY_DETAIL" not in failed.text
        if failure == "stall":
            assert "timed out" in failed.json()["error"]["message"]
            assert cancelled == [True]
        assert client.get("/livez").status_code == 200
        monkeypatch.setattr(target, attribute, AsyncMock(return_value=ready_value))
        assert client.get("/readyz").status_code == 200
