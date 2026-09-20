from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
from fastapi import FastAPI

from fs2_serve.access_models import OperatorRole
from fs2_serve.performance_routes import performance_router


async def test_performance_routes_use_existing_access_check_before_repository():
    access = SimpleNamespace(authorize_global=AsyncMock())

    async def operator():
        return SimpleNamespace(subject="test")

    app = FastAPI()
    app.include_router(performance_router(None, operator, access, lambda data: {"data": data}))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://test") as client:
        response = await client.get("/admin/api/v1/performance/campaigns")
    assert response.status_code == 503
    assert access.authorize_global.call_args.args[1] == OperatorRole.VIEWER


async def test_unbounded_campaigns_rejected_before_database():
    async def operator():
        return SimpleNamespace(subject="test")

    app = FastAPI()
    app.include_router(performance_router(None, operator, None, lambda data: data))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://test") as client:
        response = await client.get("/admin/api/v1/performance/campaigns?limit=10000")
    assert response.status_code == 422
