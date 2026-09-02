from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "stages/workloads/scripts/public_edge_admin_smoke.py"


def _module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("public_edge_admin_smoke", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _response(
    module: ModuleType, body: object, *, status: int = 200, **headers: str
) -> object:
    payload = body if isinstance(body, bytes) else json.dumps(body).encode()
    return module.HttpResponse(
        status, headers, payload, "https://inference.example.test"
    )


def _envelope(data: dict[str, object], source: str = "postgresql") -> dict[str, object]:
    return {
        "meta": {
            "schema_version": "fs2.admin-api/v1",
            "sources": [{"id": source, "state": "available"}],
        },
        "data": data,
    }


class FakeTransport:
    origin = "https://inference.example.test"

    def __init__(self, responses: dict[tuple[str, str], object]) -> None:
        self.responses = responses
        self.calls: list[tuple[str, str, dict[str, str], bytes | None]] = []

    def request(
        self,
        path: str,
        *,
        method: str = "GET",
        headers: dict[str, str] | None = None,
        body: bytes | None = None,
    ) -> object:
        self.calls.append((method, path, dict(headers or {}), body))
        return self.responses[(method, path)]


def _working_transport(
    module: ModuleType, *, grafana_health: bool = True
) -> FakeTransport:
    responses: dict[tuple[str, str], object] = {
        ("GET", "/admin/"): _response(
            module,
            b"<!doctype html><html><body>Admin</body></html>",
            **{"content-type": "text/html; charset=utf-8"},
        ),
        ("POST", "/admin/api/v1/session"): _response(
            module,
            _envelope({"principal": {"id": "operator"}}),
            **{
                "set-cookie": "__Host-fs2_admin_session=session-secret; Secure; HttpOnly"
            },
        ),
        ("GET", "/admin/api/v1/session"): _response(
            module, _envelope({"principal": {"id": "operator"}})
        ),
        ("GET", "/admin/api/v1/models?limit=256"): _response(
            module, _envelope({"items": [{"identity": {"id": "qwen3-8b"}}], "total": 1})
        ),
        ("GET", "/admin/api/v1/capacity"): _response(
            module,
            _envelope(
                {
                    "node_pools": {"state": "available", "items": [{"id": "pool"}]},
                    "kueue": {
                        "state": "available",
                        "cluster_queues": [{"name": "models"}],
                        "local_queues": [{"name": "models"}],
                    },
                    "autoscaling": {
                        "hpa": {"state": "available"},
                        "keda": {"state": "available"},
                    },
                    "node_scaler": {
                        "state": "available",
                        "configured": True,
                        "healthy": True,
                    },
                },
                "kubernetes_capacity",
            ),
        ),
        ("GET", "/admin/api/v1/observability"): _response(
            module,
            _envelope(
                {
                    "components": [
                        {"id": "prometheus", "health": "healthy"},
                        {"id": "grafana", "health": "healthy"},
                        {"id": "loki", "health": "healthy"},
                        {"id": "otel", "health": "healthy"},
                        {"id": "dcgm", "health": "healthy"},
                        {"id": "kueue", "health": "healthy"},
                        {"id": "keda", "health": "healthy"},
                        {
                            "id": "tempo",
                            "health": "unknown",
                            "reason": "component is configured as not installed",
                        },
                    ]
                },
                "observability",
            ),
        ),
        ("GET", "/admin/api/v1/principals?limit=200"): _response(
            module, _envelope({"items": [{"id": "operator"}]})
        ),
        ("GET", "/admin/api/v1/keys?limit=200"): _response(
            module, _envelope({"items": []})
        ),
        ("DELETE", "/admin/api/v1/session"): _response(module, b"", status=204),
    }
    if grafana_health:
        responses[("GET", "/admin/observability/grafana/api/health")] = _response(
            module, {"database": "ok", "version": "13.2.0"}
        )
    else:
        responses[("GET", "/admin/observability/grafana/api/health")] = _response(
            module, {"message": "Unauthorized"}, status=401
        )
        responses[("GET", "/admin/observability/grafana/login")] = _response(
            module,
            b"<!doctype html><html><title>Grafana</title></html>",
            **{"content-type": "text/html"},
        )
    return FakeTransport(responses)


def test_smoke_checks_public_admin_views_queues_and_grafana_without_forwarding_tokens() -> (
    None
):
    module = _module()
    transport = _working_transport(module)
    token = "admin-token-must-not-leak"

    result = module.run_smoke(transport, token)

    assert result["status"] == "PASS"
    assert result["tls"] == {"verified": True}
    assert result["admin_api"]["models"]["items"] == 1
    assert result["admin_api"]["capacity_and_queues"] == {
        "node_pools": 1,
        "node_pool_state": "available",
        "cluster_queues": 1,
        "local_queues": 1,
        "queue_state": "available",
        "node_scaler_state": "available",
        "sources": {"available": 1},
    }
    assert result["admin_api"]["users"]["items"] == 1
    assert result["admin_api"]["api_keys"]["items"] == 0
    assert result["grafana"] == {
        "mode": "health",
        "database": "ok",
        "version": "13.2.0",
    }
    assert result["session_closed"] is True

    authorization_calls = [
        (method, path, headers)
        for method, path, headers, _ in transport.calls
        if "Authorization" in headers
    ]
    assert authorization_calls == [
        (
            "POST",
            "/admin/api/v1/session",
            {
                "Accept": "application/json",
                "Authorization": f"Bearer {token}",
                "Origin": transport.origin,
            },
        )
    ]
    for _, path, headers, _ in transport.calls:
        if path.startswith("/admin/observability/grafana"):
            assert "Authorization" not in headers
            assert "Cookie" not in headers
    assert token not in json.dumps(result)


def test_grafana_native_login_page_is_a_valid_reachability_result() -> None:
    module = _module()
    result = module.run_smoke(_working_transport(module, grafana_health=False), "token")
    assert result["grafana"] == {"mode": "login", "database": None, "version": None}


def test_partial_adapter_states_fail_readiness_and_close_the_session() -> None:
    module = _module()
    transport = _working_transport(module)
    response = transport.responses[("GET", "/admin/api/v1/observability")]
    payload = json.loads(response.body)
    payload["meta"]["sources"] = [
        {"id": "observability", "state": "available"},
        {"id": "node_scaler", "state": "unavailable"},
    ]
    transport.responses[("GET", "/admin/api/v1/observability")] = _response(
        module, payload
    )

    with pytest.raises(module.SmokeError, match="unavailable data source"):
        module.run_smoke(transport, "token")

    assert ("DELETE", "/admin/api/v1/session") in [
        (method, path) for method, path, _, _ in transport.calls
    ]


def test_unavailable_node_scaler_fails_readiness() -> None:
    module = _module()
    transport = _working_transport(module)
    response = transport.responses[("GET", "/admin/api/v1/capacity")]
    payload = json.loads(response.body)
    payload["data"]["node_scaler"] = {
        "state": "unavailable",
        "configured": False,
        "healthy": False,
    }
    transport.responses[("GET", "/admin/api/v1/capacity")] = _response(module, payload)

    with pytest.raises(module.SmokeError, match="unavailable scaling or queue"):
        module.run_smoke(transport, "token")


def test_missing_queue_shape_fails_and_still_closes_the_session() -> None:
    module = _module()
    transport = _working_transport(module)
    transport.responses[("GET", "/admin/api/v1/capacity")] = _response(
        module,
        _envelope(
            {
                "node_pools": {"state": "available", "items": []},
                "kueue": {"state": "available"},
                "autoscaling": {},
                "node_scaler": {},
            }
        ),
    )

    with pytest.raises(module.SmokeError, match="cluster and local queues"):
        module.run_smoke(transport, "token")

    assert ("DELETE", "/admin/api/v1/session") in [
        (method, path) for method, path, _, _ in transport.calls
    ]


def test_cli_redacts_token_from_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    module = _module()
    token = "admin-token-must-never-print"
    token_file = tmp_path / "admin-token"
    token_file.write_text(token, encoding="utf-8")
    monkeypatch.setattr(module, "HttpsTransport", lambda *args, **kwargs: object())

    def fail(*args: object, **kwargs: object) -> None:
        raise module.SmokeError(f"request rejected for {token}")

    monkeypatch.setattr(module, "run_smoke", fail)

    exit_code = module.main(
        [
            "--endpoint",
            "https://inference.example.test/admin/",
            "--admin-token-file",
            str(token_file),
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 1
    assert token not in captured.out
    assert token not in captured.err
    assert "<redacted>" in captured.err


@pytest.mark.parametrize(
    "endpoint",
    (
        "http://inference.example.test",
        "https://inference.example.test/mcp",
        "https://user:password@inference.example.test",
    ),
)
def test_endpoint_must_be_the_verified_public_origin_or_admin_url(
    endpoint: str,
) -> None:
    module = _module()
    with pytest.raises(module.SmokeError):
        module.normalize_origin(endpoint)


def test_admin_output_url_normalizes_to_origin() -> None:
    module = _module()
    assert (
        module.normalize_origin("https://inference.example.test:8443/admin/")
        == "https://inference.example.test:8443"
    )
