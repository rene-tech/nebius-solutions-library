"""Mounted operator request logs preserve payloads without changing inference."""

import asyncio
import base64
from datetime import UTC, datetime, timedelta
import hashlib
import json
from uuid import uuid4

from test_admin_access_api import BOOTSTRAP_AUTH, _client, _create_principal, _principal_cookie, _runtime

from fs2_serve.access_models import OperatorRole
from fs2_serve.api import ADMIN_SESSION_COOKIE
from fs2_serve.models import Scope, TokenCreate
from fs2_serve.request_debug import DebugExchange, InMemoryDebugStore, body_capture
from fs2_serve.request_debug_authorization import TOKEN_SCHEMA, TRUST_SCHEMA
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


DEBUG_KEY = Ed25519PrivateKey.generate()
DEBUG_PUBLIC_KEY = DEBUG_KEY.public_key().public_bytes(
    serialization.Encoding.Raw, serialization.PublicFormat.Raw
)
DEBUG_KEY_ID = "sha256:" + hashlib.sha256(DEBUG_PUBLIC_KEY).hexdigest()
DEBUG_BROKER_ID = "test-debug-broker"
DEBUG_CLUSTER_ID = "mk8scluster-test"
DEBUG_SCOPE_POLICY_SHA256 = "7" * 64


def _canonical(value: object) -> bytes:
    return json.dumps(value, separators=(",", ":"), sort_keys=True).encode()


def _urlsafe(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode().rstrip("=")


def _enable_debug_trust(runtime) -> None:
    runtime.settings.request_debug_proxy_trust_json = _canonical(
        {
            "brokers": [
                {
                    "cluster_ids": [DEBUG_CLUSTER_ID],
                    "config_path": "/etc/fs2/internal-debug-credential-broker.json",
                    "config_sha256": "1" * 64,
                    "executable_path": "/usr/local/libexec/fs2-internal-debug-credential-broker",
                    "executable_sha256": "2" * 64,
                    "id": DEBUG_BROKER_ID,
                    "isolation_mode": "private-network-namespace+broker-owned-proxy",
                    "key_id": DEBUG_KEY_ID,
                    "listener_modes": ["debug-read-only", "ordinary-authenticated"],
                    "modes": ["debug", "ordinary"],
                    "namespace_policy_sha256": "3" * 64,
                    "network_namespace_owner_uid": 0,
                    "peer_credential_mode": "SO_PEERCRED+capsule-effective-gid",
                    "peer_gid": 1234,
                    "peer_uid": 0,
                    "public_key": _urlsafe(DEBUG_PUBLIC_KEY),
                    "role": "internal-proxy-session-broker",
                    "runtime_review_sha256": "4" * 64,
                    "scope_policy_sha256": DEBUG_SCOPE_POLICY_SHA256,
                    "socket_path": "/run/fs2/internal-debug-credential-broker.sock",
                }
            ],
            "issuers": [],
            "schema": TRUST_SCHEMA,
        }
    ).decode()


def _debug_headers(*, app_id: str, model_id: str, tenant_id: str) -> dict[str, str]:
    issued = datetime.now(UTC).replace(microsecond=0)
    expires = issued + timedelta(hours=1)
    payload = {
        "activation_expires_at": expires.isoformat().replace("+00:00", "Z"),
        "activation_payload_sha256": "5" * 64,
        "app_id": app_id,
        "audience": "fs2-request-debug-read",
        "broker_id": DEBUG_BROKER_ID,
        "cluster_id": DEBUG_CLUSTER_ID,
        "expires_at": expires.isoformat().replace("+00:00", "Z"),
        "issued_at": issued.isoformat().replace("+00:00", "Z"),
        "issuer": {"id": DEBUG_BROKER_ID, "key_id": DEBUG_KEY_ID},
        "methods": ["GET", "HEAD"],
        "model_id": model_id,
        "schema": TOKEN_SCHEMA,
        "scope_policy_sha256": DEBUG_SCOPE_POLICY_SHA256,
        "session_id": "6" * 32,
        "tenant_id": tenant_id,
    }
    payload_sha256 = hashlib.sha256(_canonical(payload)).hexdigest()
    signed = _canonical(
        {"payload": payload, "payload_sha256": payload_sha256, "schema": TOKEN_SCHEMA}
    )
    envelope = {
        "payload": payload,
        "payload_sha256": payload_sha256,
        "schema": TOKEN_SCHEMA,
        "signature": _urlsafe(DEBUG_KEY.sign(signed)),
    }
    return {"x-fs2-debug-authorization": _urlsafe(_canonical(envelope))}


def _qwen_app(client) -> dict[str, str]:
    return next(
        item
        for item in client.get("/admin/api/v1/apps").json()["data"]["items"]
        if item["public_model_id"] == "qwen3-8b"
    )


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
        assert client.get("/admin/api/v1/requests").status_code == 403
    assert not asyncio.run(store.list()).items


def test_malformed_authenticated_payload_is_captured_without_a_run_or_auth_secret(registry, cipher, hasher):
    runtime = _runtime(registry, cipher, hasher)
    _enable_debug_trust(runtime)
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
        app = _qwen_app(client)
        base = f"/admin/api/v1/apps/{app['app_id']}/requests"
        debug_headers = _debug_headers(
            app_id=app["app_id"],
            model_id=app["public_model_id"],
            tenant_id="debug-tenant",
        )
        assert client.get("/admin/api/v1/requests").status_code == 403
        listing = client.get(base, headers=debug_headers)
        assert listing.status_code == 200, listing.text
        (summary,) = listing.json()["data"]["items"]
        assert summary["operation_id"] is None
        assert summary["tenant_id"] == "debug-tenant"
        assert summary["principal_id"] == "debug-researcher"
        assert "request_body" not in summary and "synthetic" not in listing.text
        detail = client.get(f"{base}/{summary['id']}", headers=debug_headers)
        data = detail.json()["data"]
        assert data["request_body"]["data"] == raw
        assert data["response_body"]["data"] == response.text
        assert data["request_body"]["complete"] and data["response_body"]["complete"]
        assert token.token not in detail.text
        assert ["authorization", "[REDACTED]"] in data["request_headers"]


def test_app_operation_filters_and_full_error_detail(registry, cipher, hasher):
    runtime = _runtime(registry, cipher, hasher)
    _enable_debug_trust(runtime)
    store = InMemoryDebugStore()
    runtime.request_debug_store = store
    operation = uuid4()
    expected = _exchange(operation=operation)
    foreign_model = _exchange(model="other-model")
    for exchange in (expected, foreign_model, _exchange(tenant=None, model=None)):
        asyncio.run(store.record(exchange))
    with _client(runtime) as client:
        assert client.post("/admin/api/v1/session", headers=BOOTSTRAP_AUTH).status_code == 200
        app = _qwen_app(client)
        debug_headers = _debug_headers(
            app_id=app["app_id"],
            model_id=app["public_model_id"],
            tenant_id="tenant-a",
        )
        base = f"/admin/api/v1/apps/{app['app_id']}/requests"
        listing = client.get(
            base,
            params={"operation_id": str(operation)},
            headers=debug_headers,
        ).json()["data"]
        assert [item["id"] for item in listing["items"]] == [str(expected.id)]
        detail = client.get(f"{base}/{expected.id}", headers=debug_headers)
        assert detail.status_code == 200
        assert detail.json()["data"]["response_body"]["data"] == expected.response_body.data
        assert client.get(f"{base}/{foreign_model.id}", headers=debug_headers).status_code == 404
        assert client.get(
            base, params={"cursor": "invalid"}, headers=debug_headers
        ).status_code == 400
        assert client.get("/admin/api/v1/requests", headers=debug_headers).status_code == 403


def test_tenant_operator_can_only_read_own_captures(registry, cipher, hasher):
    runtime = _runtime(registry, cipher, hasher)
    _enable_debug_trust(runtime)
    store = InMemoryDebugStore()
    runtime.request_debug_store = store
    rows = [_exchange(tenant=value) for value in ("tenant-a", "tenant-b", None)]
    for row in rows:
        asyncio.run(store.record(row))
    identity = _create_principal(runtime, role=OperatorRole.VIEWER, tenant_id="tenant-a", subject="tenant-observer")
    with _client(runtime) as client:
        client.cookies.set(ADMIN_SESSION_COOKIE, _principal_cookie(runtime, identity))
        app = _qwen_app(client)
        base = f"/admin/api/v1/apps/{app['app_id']}/requests"
        debug_headers = _debug_headers(
            app_id=app["app_id"],
            model_id=app["public_model_id"],
            tenant_id="tenant-a",
        )
        listing = client.get(base, headers=debug_headers)
        assert listing.status_code == 200, listing.text
        assert [item["id"] for item in listing.json()["data"]["items"]] == [str(rows[0].id)]
        assert client.get(f"{base}/{rows[0].id}", headers=debug_headers).status_code == 200
        for row in rows[1:]:
            assert client.get(f"{base}/{row.id}", headers=debug_headers).status_code == 404


def test_signed_scope_is_required_and_cannot_cross_app_tenant_or_method(
    registry, cipher, hasher
):
    runtime = _runtime(registry, cipher, hasher)
    _enable_debug_trust(runtime)
    runtime.request_debug_store = InMemoryDebugStore()
    with _client(runtime) as client:
        assert client.post("/admin/api/v1/session", headers=BOOTSTRAP_AUTH).status_code == 200
        app = _qwen_app(client)
        base = f"/admin/api/v1/apps/{app['app_id']}/requests"
        valid = _debug_headers(
            app_id=app["app_id"],
            model_id=app["public_model_id"],
            tenant_id="tenant-a",
        )
        wrong_app = _debug_headers(
            app_id=str(uuid4()),
            model_id=app["public_model_id"],
            tenant_id="tenant-a",
        )
        assert client.get(base).status_code == 403
        assert client.get(base, headers=wrong_app).status_code == 403
        assert client.post(base, headers=valid).status_code in {403, 405}
