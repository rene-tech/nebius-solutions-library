"""App inventory must not compute discarded per-user/lifecycle usage charts."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from test_apps import _context, _record

from fs2_serve.admin import AdminReadService
from fs2_serve.apps import AppsService
from fs2_serve.apps_repository import MemoryAppsRepository
from fs2_serve.memory_store import MemoryStore


@pytest.mark.asyncio
async def test_35_app_inventory_uses_scoped_summary_and_preserves_bounded_fanout(registry, cipher, hasher):
    context = _context()
    last_used = datetime.now(UTC) - timedelta(days=5)
    calls = []
    active = peak = 0

    class Repository(MemoryAppsRepository):
        async def usage_summary(self, model_id, received_context, tenant_id):
            nonlocal active, peak
            calls.append((model_id, received_context, tenant_id))
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0)
            active -= 1
            index = int(model_id.removeprefix("independent-"))
            return {"logical_runs": index, "last_used_at": last_used if index else None}

        async def usage(self, *args):
            raise AssertionError("Inventory must not read full usage or historical rollups")

        async def last_used(self, *args):
            raise AssertionError("Summary already contains the lifetime clock")

    repository = Repository()
    for index in range(35):
        await repository.seed(_record(public_id=f"independent-{index}", name=None))
    service = AppsService(repository=repository, registry=registry,
                          admin=AdminReadService(store=MemoryStore(cipher, hasher), registry=registry))
    result = await service.list(context, "tenant-a")
    assert len(result.items) == len(calls) == 35
    assert 1 < peak <= 4
    assert {call[0] for call in calls} == {f"independent-{index}" for index in range(35)}
    assert all(call[1:] == (context, "tenant-a") for call in calls)
    for item in result.items:
        index = int(item.public_model_id.removeprefix("independent-"))
        assert item.logical_run_count == index
        assert item.last_used_at == (last_used if index else None)


@pytest.mark.asyncio
async def test_summary_failure_is_not_fabricated_zero_usage(registry, cipher, hasher):
    class Repository(MemoryAppsRepository):
        async def usage_summary(self, *args):
            raise TimeoutError("source unavailable")

    repository = Repository()
    await repository.seed(_record(public_id="qwen3-8b", name=None))
    service = AppsService(repository=repository, registry=registry,
                          admin=AdminReadService(store=MemoryStore(cipher, hasher), registry=registry))
    with pytest.raises(TimeoutError):
        await service.list(_context())
