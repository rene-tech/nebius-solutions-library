from __future__ import annotations

import httpx
from fastapi.testclient import TestClient

from fs2_serve.metrics_proxy import create_metrics_proxy_app


def test_metrics_proxy_has_one_fixed_loopback_upstream_and_no_discovery_surface() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url == "http://127.0.0.1:8080/metrics"
        assert "authorization" not in request.headers
        return httpx.Response(200, content=b"fs2_serve_test_metric 1\n")

    app = create_metrics_proxy_app(
        application_port=8080,
        transport=httpx.MockTransport(handler),
    )
    with TestClient(app) as client:
        response = client.get("/metrics")
        assert response.status_code == 200
        assert response.text == "fs2_serve_test_metric 1\n"
        assert response.headers["cache-control"] == "no-store"
        assert client.get("/openapi.json").status_code == 404


def test_metrics_proxy_fails_closed_on_upstream_error_or_oversize() -> None:
    for upstream in (
        httpx.Response(500, content=b"internal"),
        httpx.Response(200, content=b"x" * (16 * 1024 * 1024 + 1)),
    ):
        app = create_metrics_proxy_app(
            application_port=8080,
            transport=httpx.MockTransport(lambda _: upstream),
        )
        with TestClient(app) as client:
            response = client.get("/metrics")
            assert response.status_code == 503
            assert response.content == b""

    def unavailable(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("collector unavailable", request=request)

    app = create_metrics_proxy_app(
        application_port=8080,
        transport=httpx.MockTransport(unavailable),
    )
    with TestClient(app) as client:
        assert client.get("/metrics").status_code == 503
