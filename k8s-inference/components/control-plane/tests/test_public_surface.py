from __future__ import annotations

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from fs2_serve.public_surface import PublicSurfaceBoundary

MCP_RESOURCE = "https://inference.test.invalid/mcp"


def bounded_app(*, ready_status: int = 200, metadata_status: int = 200) -> FastAPI:
    app = FastAPI()

    @app.get("/readyz")
    async def readyz() -> JSONResponse:
        return JSONResponse(
            {
                "status": "ready",
                "models": 14,
                "route_evidence": {"generation": 7},
                "admission": {"workers": ["worker-a"]},
                "federation": {"circuits": {"private-model": "open"}},
            },
            status_code=ready_status,
        )

    @app.get("/.well-known/oauth-protected-resource")
    @app.get("/.well-known/oauth-protected-resource/mcp")
    async def protected_resource() -> JSONResponse:
        return JSONResponse(
            {
                "resource": MCP_RESOURCE,
                "authorization_servers": ["https://identity.test.invalid"],
                "scopes_supported": ["mcp.invoke"],
            },
            status_code=metadata_status,
        )

    @app.get("/operator-detail")
    async def operator_detail() -> JSONResponse:
        return JSONResponse({"workers": ["worker-a"]})

    app.add_middleware(PublicSurfaceBoundary, mcp_resource=MCP_RESOURCE)
    return app


def test_public_readiness_preserves_status_with_minimal_body() -> None:
    with TestClient(bounded_app()) as client:
        ready = client.get("/readyz")
    assert ready.status_code == 200
    assert ready.json() == {"status": "ready"}
    assert ready.headers["cache-control"] == "no-store"
    assert "worker-a" not in ready.text
    assert "private-model" not in ready.text


def test_public_readiness_failure_does_not_disclose_dependency() -> None:
    with TestClient(bounded_app(ready_status=503)) as client:
        unavailable = client.get("/readyz")
    assert unavailable.status_code == 503
    assert unavailable.json() == {"status": "unavailable"}
    assert "worker-a" not in unavailable.text
    assert "private-model" not in unavailable.text


def test_protected_resource_metadata_documents_static_header_bearer_only() -> None:
    with TestClient(bounded_app()) as client:
        responses = [
            client.get("/.well-known/oauth-protected-resource"),
            client.get("/.well-known/oauth-protected-resource/mcp"),
        ]
    for response in responses:
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
        assert response.json() == {
            "resource": MCP_RESOURCE,
            "bearer_methods_supported": ["header"],
        }
        assert "authorization_servers" not in response.json()
        assert "scopes_supported" not in response.json()


def test_boundary_preserves_non_success_metadata_and_unrelated_operator_detail() -> None:
    with TestClient(bounded_app(metadata_status=421)) as client:
        rejected = client.get("/.well-known/oauth-protected-resource")
        operator = client.get("/operator-detail")
    assert rejected.status_code == 421
    assert rejected.json()["authorization_servers"] == ["https://identity.test.invalid"]
    assert operator.json() == {"workers": ["worker-a"]}
