"""Mounted operator request logs preserve payloads without changing inference."""

import asyncio
from datetime import UTC, datetime
from uuid import uuid4

from test_admin_access_api import BOOTSTRAP_AUTH, _client, _create_principal, _principal_cookie, _runtime

from fs2_serve.access_models import OperatorRole
from fs2_serve.api import ADMIN_SESSION_COOKIE
from fs2_serve.models import Scope, TokenCreate
from fs2_serve.request_debug import DebugExchange, InMemoryDebugStore, body_capture


def _exchange(*, tenant="tenant-a", model="qwen3-8b", operation=None):
    return DebugExchange(
        id=uuid4(),
        source="upstream",
        operation_id=operation,
        started_at=datetime.now(UTC),
        completed_at=datetime.now(UTC),
        tenant_id=tenant,
        principal_id="researcher" if tenant else None,
        model_id=model,
        endpoint="/predict",
        method="POST",
        http_status=422,
        request_body=body_capture(b'{"input":"synthetic-debug-input"}', "application/json", True),
        response_body=body_capture(b'{"detail":"synthetic upstream validation failure"}', "application/json", True),
        error_type="upstream_http_error",
    )


def test_capture_is_opt_in_and_admin_traffic_is_not_captured(registry, cipher, hasher):
    runtime = _runtime(registry, cipher, hasher)
    store = InMemoryDebugStore()
    runtime.request_debug_store = store
    with _client(runtime) as client:
        client.get("/v1/models")
        assert client.post("/admin/api/v1/session", headers=BOOTSTRAP_AUTH).status_code == 200
        assert client.get("/admin/api/v1/requests").json()["data"]["items"] == []
    assert not asyncio.run(store.list()).items


def test_malformed_authenticated_payload_is_captured_without_a_run_or_auth_secret(registry, cipher, hasher):
    runtime = _runtime(registry, cipher, hasher)
    runtime.settings.request_debug_enabled = True
    token = asyncio.run(
        runtime.tokens.issue(
            TokenCreate(
                principal_id="debug-researcher",
                tenant_id="debug-tenant",
                name="debug-test",
                scopes=[Scope.INFERENCE_INVOKE],
                models=["qwen3-8b"],
            ),
            created_by="test",
        )
    )
    raw = '{"model":"qwen3-8b","messages":'
    with _client(runtime) as client:
        response = client.post(
            "/v1/chat/completions",
            content=raw,
            headers={"authorization": f"Bearer {token.token}", "content-type": "application/json"},
        )
        assert response.status_code in {400, 422}
        assert client.get("/admin/api/v1/requests").status_code == 401
        assert client.post("/admin/api/v1/session", headers=BOOTSTRAP_AUTH).status_code == 200
        listing = client.get("/admin/api/v1/requests")
        assert listing.status_code == 200, listing.text
        (summary,) = listing.json()["data"]["items"]
        assert summary["operation_id"] is None
        assert summary["tenant_id"] == "debug-tenant"
        assert summary["principal_id"] == "debug-researcher"
        assert "request_body" not in summary and "synthetic" not in listing.text
        detail = client.get(f"/admin/api/v1/requests/{summary['id']}")
        data = detail.json()["data"]
        assert data["request_body"]["data"] == raw
        assert data["response_body"]["data"] == response.text
        assert data["request_body"]["complete"] and data["response_body"]["complete"]
        assert token.token not in detail.text
        assert ["authorization", "[REDACTED]"] in data["request_headers"]


def test_app_operation_filters_and_full_error_detail(registry, cipher, hasher):
    runtime = _runtime(registry, cipher, hasher)
    store = InMemoryDebugStore()
    runtime.request_debug_store = store
    operation = uuid4()
    expected = _exchange(operation=operation)
    foreign_model = _exchange(model="other-model")
    for exchange in (expected, foreign_model, _exchange(tenant=None, model=None)):
        asyncio.run(store.record(exchange))
    with _client(runtime) as client:
        assert client.post("/admin/api/v1/session", headers=BOOTSTRAP_AUTH).status_code == 200
        app = next(
            item
            for item in client.get("/admin/api/v1/apps").json()["data"]["items"]
            if item["public_model_id"] == "qwen3-8b"
        )
        base = f"/admin/api/v1/apps/{app['app_id']}/requests"
        listing = client.get(base, params={"operation_id": str(operation)}).json()["data"]
        assert [item["id"] for item in listing["items"]] == [str(expected.id)]
        detail = client.get(f"{base}/{expected.id}")
        assert detail.status_code == 200
        assert detail.json()["data"]["response_body"]["data"] == expected.response_body.data
        assert client.get(f"{base}/{foreign_model.id}").status_code == 404
        assert client.get(base, params={"cursor": "invalid"}).status_code == 400
        assert len(client.get("/admin/api/v1/requests").json()["data"]["items"]) == 3


def test_tenant_operator_can_only_read_own_captures(registry, cipher, hasher):
    runtime = _runtime(registry, cipher, hasher)
    store = InMemoryDebugStore()
    runtime.request_debug_store = store
    rows = [_exchange(tenant=value) for value in ("tenant-a", "tenant-b", None)]
    for row in rows:
        asyncio.run(store.record(row))
    identity = _create_principal(runtime, role=OperatorRole.VIEWER, tenant_id="tenant-a", subject="tenant-observer")
    with _client(runtime) as client:
        client.cookies.set(ADMIN_SESSION_COOKIE, _principal_cookie(runtime, identity))
        listing = client.get("/admin/api/v1/requests")
        assert listing.status_code == 200, listing.text
        assert [item["id"] for item in listing.json()["data"]["items"]] == [str(rows[0].id)]
        assert client.get(f"/admin/api/v1/requests/{rows[0].id}").status_code == 200
        for row in rows[1:]:
            assert client.get(f"/admin/api/v1/requests/{row.id}").status_code == 404
