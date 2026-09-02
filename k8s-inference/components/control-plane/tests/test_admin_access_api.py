from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from fastapi.testclient import TestClient

from fs2_serve.access_models import (
    BOOTSTRAP_OPERATOR_PRINCIPAL_ID,
    OperatorPrincipalCreate,
    OperatorPrincipalPatch,
    OperatorRole,
    PrincipalKind,
)
from fs2_serve.admission import AdmissionService
from fs2_serve.api import ADMIN_SESSION_COOKIE, AppRuntime, create_app
from fs2_serve.auth import AuthenticationError, OperatorSessionService, PepperRing, TokenService
from fs2_serve.configuration import ConfigurationService, InMemoryConfigurationRepository, configuration_etag
from fs2_serve.configuration_models import ConfigurationProposal
from fs2_serve.memory_store import MemoryStore
from fs2_serve.models import AdmissionRequest, Scope, TokenCreate
from fs2_serve.runtime import StubRuntimeClient
from fs2_serve.settings import Settings
from fs2_serve.telemetry import Metrics

BOOTSTRAP_TOKEN = "a" * 32
BOOTSTRAP_AUTH = {"authorization": f"Bearer {BOOTSTRAP_TOKEN}"}
REJECTED_CREDENTIAL = "REJECTED_BOOTSTRAP_MUST_NEVER_APPEAR_9481"
REJECTED_POLICY_VALUE = "REJECTED_POLICY_VALUE_MUST_NOT_APPEAR_9481"


def _runtime(registry: Any, cipher: Any, hasher: Any) -> AppRuntime:
    store = MemoryStore(cipher, hasher)
    settings = Settings(
        run_workers=False,
        max_request_bytes=16_384,
        public_base_url="https://inference.test.invalid",
        authorization_server_url="https://identity.test.invalid",
        catalog_dir=Path("/unused"),
        bindings_file=Path("/unused"),
    )
    metrics = Metrics(registry.list(enabled_only=True))
    peppers = PepperRing(active_key_id="pepper-v1", keys={"pepper-v1": b"p" * 32})
    return AppRuntime(
        settings=settings,
        registry=registry,
        store=store,
        tokens=TokenService(store, peppers),
        admission=AdmissionService(
            registry=registry,
            store=store,
            runtime=StubRuntimeClient(),
            metrics=metrics,
            worker_concurrency=1,
            poll_seconds=0.01,
            lease_seconds=30,
            maintenance_interval_seconds=1,
            shutdown_grace_seconds=1,
        ),
        metrics=metrics,
        admin_token=BOOTSTRAP_TOKEN.encode(),
        operator_sessions=OperatorSessionService(store, peppers, ttl_seconds=300),
        owns_store=False,
    )


def _client(runtime: AppRuntime) -> TestClient:
    return TestClient(create_app(runtime), base_url="https://inference.test.invalid")


def _cookie_from(response: Any) -> str:
    value = response.cookies.get(ADMIN_SESSION_COOKIE)
    assert value is not None
    return value


def test_authenticated_configuration_routes_plan_and_stop_at_terraform(
    registry: Any,
    cipher: Any,
    hasher: Any,
) -> None:
    # Reuse the canonical-catalog configuration fixture rather than cloning its
    # immutable acquisition/provenance identities in this HTTP integration.
    from test_admin_configuration import qualified_configuration, with_cooldown

    initial, catalog = qualified_configuration()
    runtime = _runtime(registry, cipher, hasher)
    runtime.configuration = ConfigurationService(
        repository=InMemoryConfigurationRepository(initial),
        catalog=catalog,
    )
    with _client(runtime) as client:
        unauthorized = client.get("/admin/api/v1/configuration")
        session = client.post("/admin/api/v1/session", headers=BOOTSTRAP_AUTH)
        current = client.get("/admin/api/v1/configuration")
        desired = with_cooldown(initial, 301)
        proposal = ConfigurationProposal(
            base_etag=configuration_etag(initial),
            desired=desired,
        ).model_dump(mode="json")
        diff = client.post("/admin/api/v1/configuration:diff", json=proposal)
        validation = client.post("/admin/api/v1/configuration:validate", json=proposal)
        planned = client.post("/admin/api/v1/configuration:plan", json=proposal)
        plan_data = planned.json()["data"]
        reconciled = client.post(
            "/admin/api/v1/configuration:reconcile",
            json={"plan_id": plan_data["plan_id"], "base_etag": configuration_etag(initial)},
        )
        status = client.get(
            f"/admin/api/v1/configuration/reconciliations/{reconciled.json()['data']['reconciliation_id']}"
        )

    assert unauthorized.status_code == 401
    assert session.status_code == 200
    assert current.status_code == 200 and current.json()["data"]["revision"] == 1
    assert diff.status_code == 200 and diff.json()["data"]["runtime_change_count"] == 0
    assert diff.json()["data"]["terraform_change_count"] == 1
    assert validation.status_code == 200 and validation.json()["data"]["valid"]
    assert planned.status_code == 200 and plan_data["terraform"]["state"] == "review-required"
    assert reconciled.status_code == 200
    assert reconciled.json()["data"]["phase"] == "awaiting-terraform-plan-apply"
    assert status.status_code == 200 and status.json()["data"] == reconciled.json()["data"]


def _create_principal(
    runtime: AppRuntime,
    *,
    role: OperatorRole,
    tenant_id: str | None,
    subject: str,
) -> UUID:
    assert isinstance(runtime.store, MemoryStore)
    principal_id = uuid4()
    asyncio.run(
        runtime.store.create_operator_principal(
            principal_id=principal_id,
            request=OperatorPrincipalCreate(
                subject=subject,
                display_name=subject,
                kind=PrincipalKind.HUMAN,
                role=role,
                tenant_id=tenant_id,
            ),
            actor="test-bootstrap",
        )
    )
    return principal_id


def _principal_cookie(runtime: AppRuntime, principal_id: UUID) -> str:
    return asyncio.run(runtime.operator_sessions.issue(principal_id, actor="test-bootstrap")).cookie_value


def test_curl_session_handoff_is_no_store_strict_cookie_and_never_reflects_credentials(
    registry: Any, cipher: Any, hasher: Any, caplog: Any
) -> None:
    runtime = _runtime(registry, cipher, hasher)
    assert isinstance(runtime.store, MemoryStore)
    caplog.set_level(logging.INFO, logger="fs2_serve.access")
    with _client(runtime) as client:
        rejected = client.post(
            "/admin/api/v1/session",
            headers={"authorization": f"Bearer {REJECTED_CREDENTIAL}"},
        )
        accepted = client.post(
            "/admin/api/v1/session",
            headers=BOOTSTRAP_AUTH,
        )
        session = client.get("/admin/api/v1/session")

    assert rejected.status_code == 401
    assert REJECTED_CREDENTIAL not in rejected.text
    assert REJECTED_CREDENTIAL not in str(runtime.store.audit)
    assert rejected.headers["cache-control"] == "no-store"
    assert accepted.status_code == 200
    assert BOOTSTRAP_TOKEN not in accepted.text
    assert BOOTSTRAP_TOKEN not in str(dict(accepted.headers))
    set_cookie = accepted.headers["set-cookie"]
    assert set_cookie.startswith(f"{ADMIN_SESSION_COOKIE}=")
    assert "Secure" in set_cookie
    assert "HttpOnly" in set_cookie
    assert "SameSite=strict" in set_cookie
    assert "Path=/" in set_cookie
    assert "Max-Age=300" in set_cookie
    assert "Domain=" not in set_cookie
    assert session.status_code == 200
    assert session.headers["cache-control"] == "no-store"
    assert "cookie_value" not in session.text
    cookie_value = _cookie_from(accepted)
    assert cookie_value not in repr(runtime.store.operator_sessions)
    assert BOOTSTRAP_TOKEN not in caplog.text
    assert REJECTED_CREDENTIAL not in caplog.text
    assert cookie_value not in caplog.text


def test_legacy_bootstrap_routes_keep_cli_auth_status_contract(registry: Any, cipher: Any, hasher: Any) -> None:
    runtime = _runtime(registry, cipher, hasher)
    with _client(runtime) as client:
        missing = client.get("/admin/v1/tokens")
        rejected = client.get(
            "/admin/v1/tokens",
            headers={"authorization": f"Bearer {REJECTED_CREDENTIAL}"},
        )
        session_rejected = client.post(
            "/admin/api/v1/session",
            headers={"authorization": f"Bearer {REJECTED_CREDENTIAL}"},
        )

    assert missing.status_code == 401
    assert rejected.status_code == 403
    assert session_rejected.status_code == 401
    assert REJECTED_CREDENTIAL not in missing.text + rejected.text + session_rejected.text
    assert REJECTED_CREDENTIAL not in str(runtime.store.audit)


def test_session_rotation_fixation_logout_and_replay_are_denied(registry: Any, cipher: Any, hasher: Any) -> None:
    runtime = _runtime(registry, cipher, hasher)
    attacker_cookie = "fs2_admin_00000000000000000000000000000000_" + "z" * 43
    with _client(runtime) as client:
        client.cookies.set(ADMIN_SESSION_COOKIE, attacker_cookie, path="/")
        first = client.post("/admin/api/v1/session", headers=BOOTSTRAP_AUTH)
        first_cookie = _cookie_from(first)
        second = client.post("/admin/api/v1/session", headers=BOOTSTRAP_AUTH)
        second_cookie = _cookie_from(second)
        assert first_cookie != attacker_cookie
        assert second_cookie != first_cookie
        replay_first = client.get(
            "/admin/api/v1/session",
            headers={"cookie": f"{ADMIN_SESSION_COOKIE}={first_cookie}"},
        )
        current = client.get("/admin/api/v1/session")
        logout = client.delete("/admin/api/v1/session")
        repeated_logout = client.delete(
            "/admin/api/v1/session",
            headers={"cookie": f"{ADMIN_SESSION_COOKIE}={second_cookie}"},
        )
        replay_second = client.get(
            "/admin/api/v1/session",
            headers={"cookie": f"{ADMIN_SESSION_COOKIE}={second_cookie}"},
        )

    assert first.status_code == second.status_code == current.status_code == 200
    assert replay_first.status_code == 401
    assert logout.status_code == 204
    assert repeated_logout.status_code == 204
    assert "Max-Age=0" in logout.headers["set-cookie"]
    assert "expires=" in logout.headers["set-cookie"].lower()
    assert "Secure" in logout.headers["set-cookie"]
    assert "HttpOnly" in logout.headers["set-cookie"]
    assert "SameSite=strict" in logout.headers["set-cookie"]
    assert "Domain=" not in logout.headers["set-cookie"]
    assert replay_second.status_code == 401


def test_wrong_host_and_origin_precede_session_side_effects(registry: Any, cipher: Any, hasher: Any) -> None:
    runtime = _runtime(registry, cipher, hasher)
    assert isinstance(runtime.store, MemoryStore)
    sessions_before = len(runtime.store.operator_sessions)
    with _client(runtime) as client:
        wrong_origin = client.post(
            "/admin/api/v1/session",
            headers={**BOOTSTRAP_AUTH, "origin": "https://attacker.example.invalid"},
        )
        wrong_host = client.post(
            "/admin/api/v1/session",
            headers={**BOOTSTRAP_AUTH, "host": "attacker.example.invalid"},
        )
    assert wrong_origin.status_code == 403
    assert wrong_host.status_code == 421
    assert wrong_origin.headers["cache-control"] == wrong_host.headers["cache-control"] == "no-store"
    assert len(runtime.store.operator_sessions) == sessions_before


def test_expired_revoked_and_disabled_principal_sessions_fail_closed(registry: Any, cipher: Any, hasher: Any) -> None:
    runtime = _runtime(registry, cipher, hasher)
    assert isinstance(runtime.store, MemoryStore)
    principal_id = _create_principal(
        runtime,
        role=OperatorRole.VIEWER,
        tenant_id="tenant-a",
        subject="tenant-a-viewer",
    )
    expired_cookie = _principal_cookie(runtime, principal_id)
    expired_id = runtime.operator_sessions._parse(expired_cookie)
    record = runtime.store.operator_sessions[expired_id]
    runtime.store.operator_sessions[expired_id] = record.model_copy(
        update={"session": record.session.model_copy(update={"expires_at": datetime.now(UTC) - timedelta(seconds=1)})}
    )
    revoked_cookie = _principal_cookie(runtime, principal_id)
    asyncio.run(runtime.operator_sessions.revoke(revoked_cookie, actor="test-bootstrap"))
    disabled_cookie = _principal_cookie(runtime, principal_id)
    asyncio.run(
        runtime.store.update_operator_principal(
            principal_id,
            request=OperatorPrincipalPatch(enabled=False),
            actor="test-bootstrap",
        )
    )

    with _client(runtime) as client:
        expired = client.get(
            "/admin/api/v1/session",
            headers={"cookie": f"{ADMIN_SESSION_COOKIE}={expired_cookie}"},
        )
        revoked = client.get(
            "/admin/api/v1/session",
            headers={"cookie": f"{ADMIN_SESSION_COOKIE}={revoked_cookie}"},
        )
        disabled = client.get(
            "/admin/api/v1/session",
            headers={"cookie": f"{ADMIN_SESSION_COOKIE}={disabled_cookie}"},
        )

    assert expired.status_code == revoked.status_code == disabled.status_code == 401
    assert all(response.headers["cache-control"] == "no-store" for response in (expired, revoked, disabled))


def test_tenant_and_role_isolation_key_disclosure_rotation_and_cross_origin(
    registry: Any, cipher: Any, hasher: Any
) -> None:
    runtime = _runtime(registry, cipher, hasher)
    assert isinstance(runtime.store, MemoryStore)
    operator_id = _create_principal(
        runtime,
        role=OperatorRole.OPERATOR,
        tenant_id="tenant-a",
        subject="tenant-a-operator",
    )
    viewer_id = _create_principal(
        runtime,
        role=OperatorRole.VIEWER,
        tenant_id="tenant-a",
        subject="tenant-a-viewer",
    )
    viewer_cookie = _principal_cookie(runtime, viewer_id)
    key_request = {
        "name": "agent-runtime",
        "principal_id": "agent-a",
        "tenant_id": "tenant-a",
        "scopes": ["inference.invoke", "mcp.invoke"],
        "models": ["qwen3-8b"],
        "request_budget": 100,
        "gpu_seconds_budget": 500,
        "max_concurrency": 2,
        "rate_limit_requests": 10,
        "rate_window_seconds": 60,
    }

    with _client(runtime) as client:
        handoff = client.post(
            "/admin/api/v1/session",
            headers=BOOTSTRAP_AUTH,
            json={"principal_id": str(operator_id)},
        )
        operator_cookie = _cookie_from(handoff)
        issued = client.post(
            "/admin/api/v1/keys",
            headers={"cookie": f"{ADMIN_SESSION_COOKIE}={operator_cookie}"},
            json=key_request,
        )
        secret = issued.json()["data"]["secret"]
        token_id = issued.json()["data"]["key"]["id"]
        stored = runtime.store.tokens[UUID(token_id)]
        stored.view = stored.view.model_copy(update={"requests_used": 5})
        failed_policy = client.patch(
            f"/admin/api/v1/keys/{token_id}",
            headers={"cookie": f"{ADMIN_SESSION_COOKIE}={operator_cookie}"},
            json={"name": REJECTED_POLICY_VALUE, "request_budget": 1},
        )
        updated_policy = client.patch(
            f"/admin/api/v1/keys/{token_id}",
            headers={"cookie": f"{ADMIN_SESSION_COOKIE}={operator_cookie}"},
            json={
                "scopes": ["catalog.read"],
                "models": ["qwen3-8b"],
                "request_budget": 10,
                "gpu_seconds_budget": 600,
                "max_concurrency": 3,
                "rate_limit_requests": None,
                "rate_window_seconds": None,
            },
        )
        listed = client.get(
            "/admin/api/v1/keys",
            headers={"cookie": f"{ADMIN_SESSION_COOKIE}={operator_cookie}"},
        )
        tenant_mismatch = client.get(
            "/admin/api/v1/keys?tenant_id=tenant-b",
            headers={"cookie": f"{ADMIN_SESSION_COOKIE}={operator_cookie}"},
        )
        viewer_issue = client.post(
            "/admin/api/v1/keys",
            headers={"cookie": f"{ADMIN_SESSION_COOKIE}={viewer_cookie}"},
            json=key_request,
        )
        cross_origin = client.post(
            "/admin/api/v1/keys",
            headers={
                "cookie": f"{ADMIN_SESSION_COOKIE}={operator_cookie}",
                "origin": "https://attacker.example.invalid",
            },
            json=key_request,
        )
        rotated = client.post(
            f"/admin/api/v1/keys/{token_id}:rotate",
            headers={"cookie": f"{ADMIN_SESSION_COOKIE}={operator_cookie}"},
            json={"name": "agent-runtime-rotated"},
        )
        rotated_secret = rotated.json()["data"]["secret"]
        rotated_id = rotated.json()["data"]["key"]["id"]
        revoked = client.delete(
            f"/admin/api/v1/keys/{rotated_id}",
            headers={"cookie": f"{ADMIN_SESSION_COOKIE}={operator_cookie}"},
        )

    assert handoff.status_code == 200
    assert handoff.json()["data"]["principal"]["role"] == "operator"
    assert issued.status_code == rotated.status_code == 201
    assert failed_policy.status_code == 409
    assert updated_policy.status_code == 200
    assert updated_policy.json()["data"]["scopes"] == ["catalog.read"]
    assert updated_policy.json()["data"]["request_budget"] == 10
    assert updated_policy.json()["data"]["rate_limit_requests"] is None
    assert issued.headers["cache-control"] == rotated.headers["cache-control"] == "no-store"
    assert issued.text.count(secret) == 1
    assert secret not in listed.text
    assert "secret" not in listed.json()["data"]["items"][0]
    assert listed.json()["data"]["items"][0]["usage"]["estimated_gpu_seconds"]["state"] == "estimated"
    assert listed.json()["data"]["items"][0]["usage"]["input_tokens"]["state"] == "available"
    assert tenant_mismatch.status_code == viewer_issue.status_code == cross_origin.status_code == 403
    assert len(runtime.store.tokens) == 2
    assert revoked.status_code == 200
    assert revoked.json()["data"]["state"] == "revoked"

    async def verify_lifecycle() -> None:
        try:
            await runtime.tokens.verify(secret)
        except AuthenticationError:
            pass
        else:
            raise AssertionError("rotated predecessor remained valid")
        try:
            await runtime.tokens.verify(rotated_secret)
        except AuthenticationError:
            pass
        else:
            raise AssertionError("revoked successor remained valid")

    asyncio.run(verify_lifecycle())
    audit_text = str(runtime.store.audit)
    assert secret not in audit_text
    assert rotated_secret not in audit_text
    assert REJECTED_POLICY_VALUE not in audit_text
    assert any(event.action == "token.policy.update" and event.outcome == "failed" for event in runtime.store.audit)
    assert any(event.action == "token.policy.update" and event.outcome == "succeeded" for event in runtime.store.audit)


def test_tenant_viewer_bootstraps_context_and_reads_only_own_ledger(registry: Any, cipher: Any, hasher: Any) -> None:
    runtime = _runtime(registry, cipher, hasher)
    assert isinstance(runtime.store, MemoryStore)
    viewer_id = _create_principal(
        runtime,
        role=OperatorRole.VIEWER,
        tenant_id="tenant-a",
        subject="tenant-a-detail-viewer",
    )
    viewer_cookie = _principal_cookie(runtime, viewer_id)

    async def seed(tenant_id: str) -> UUID:
        issued = await runtime.tokens.issue(
            TokenCreate(
                principal_id=f"{tenant_id}-agent",
                tenant_id=tenant_id,
                scopes={Scope.INFERENCE_INVOKE},
                models={"qwen3-8b"},
            ),
            created_by="test-bootstrap",
        )
        principal = await runtime.tokens.verify(issued.token)
        operation = await runtime.store.append_operation(
            principal=principal,
            admission=AdmissionRequest(
                model_id="qwen3-8b",
                operation="chat",
                protocol="openai-chat",
                idempotency_key=f"tenant-bound-detail-{tenant_id}",
                request_body=b"private",
            ),
            model_revision="sha256:" + "b" * 64,
            reserved_gpu_seconds=1,
            max_attempts=1,
        )
        return operation.id

    own_operation_id = asyncio.run(seed("tenant-a"))
    other_operation_id = asyncio.run(seed("tenant-b"))
    with _client(runtime) as client:
        headers = {"cookie": f"{ADMIN_SESSION_COOKIE}={viewer_cookie}"}
        context = client.get("/admin/api/v1/context", headers=headers)
        keys = client.get("/admin/api/v1/keys", headers=headers)
        listing = client.get("/admin/api/v1/operations", headers=headers)
        own_detail = client.get(
            f"/admin/api/v1/operations/{own_operation_id}",
            headers=headers,
        )
        other_detail = client.get(
            f"/admin/api/v1/operations/{other_operation_id}",
            headers=headers,
        )
        overview = client.get("/admin/api/v1/overview", headers=headers)
        models = client.get("/admin/api/v1/models", headers=headers)
        capacity = client.get("/admin/api/v1/capacity", headers=headers)
        observability = client.get("/admin/api/v1/observability", headers=headers)

    assert context.status_code == 200
    assert keys.status_code == 200
    assert {item["tenant_id"] for item in keys.json()["data"]["items"]} == {"tenant-a"}
    assert listing.status_code == 200
    assert {item["id"] for item in listing.json()["data"]["items"]} == {str(own_operation_id)}
    assert own_detail.status_code == 200
    assert other_detail.status_code == 404
    assert overview.status_code == models.status_code == capacity.status_code == observability.status_code == 403


def test_tenant_admin_cannot_create_or_modify_global_principals(registry: Any, cipher: Any, hasher: Any) -> None:
    runtime = _runtime(registry, cipher, hasher)
    other_tenant_principal_id = _create_principal(
        runtime,
        role=OperatorRole.VIEWER,
        tenant_id="tenant-b",
        subject="tenant-b-viewer",
    )
    tenant_admin_id = _create_principal(
        runtime,
        role=OperatorRole.ADMIN,
        tenant_id="tenant-a",
        subject="tenant-a-admin",
    )
    tenant_admin_cookie = _principal_cookie(runtime, tenant_admin_id)
    headers = {"cookie": f"{ADMIN_SESSION_COOKIE}={tenant_admin_cookie}"}

    with _client(runtime) as client:
        create_global = client.post(
            "/admin/api/v1/principals",
            headers=headers,
            json={
                "subject": "global-intruder",
                "display_name": "Global intruder",
                "kind": "human",
                "role": "viewer",
                "tenant_id": None,
            },
        )
        update_global = client.patch(
            f"/admin/api/v1/principals/{BOOTSTRAP_OPERATOR_PRINCIPAL_ID}",
            headers=headers,
            json={"enabled": False},
        )
        update_other_tenant = client.patch(
            f"/admin/api/v1/principals/{other_tenant_principal_id}",
            headers=headers,
            json={"enabled": False},
        )
        update_missing = client.patch(
            f"/admin/api/v1/principals/{uuid4()}",
            headers=headers,
            json={"enabled": False},
        )

    assert create_global.status_code == update_global.status_code == 403
    assert update_other_tenant.status_code == update_missing.status_code == 404
    assert all(principal.subject != "global-intruder" for principal in runtime.store.operator_principals.values())
    assert runtime.store.operator_principals[BOOTSTRAP_OPERATOR_PRINCIPAL_ID].enabled is True
    assert runtime.store.operator_principals[other_tenant_principal_id].enabled is True


def test_tenant_key_identifiers_are_not_cross_tenant_enumerable(registry: Any, cipher: Any, hasher: Any) -> None:
    runtime = _runtime(registry, cipher, hasher)
    operator_id = _create_principal(
        runtime,
        role=OperatorRole.OPERATOR,
        tenant_id="tenant-a",
        subject="tenant-a-key-operator",
    )
    operator_cookie = _principal_cookie(runtime, operator_id)

    async def seed_other_tenant() -> UUID:
        issued = await runtime.tokens.issue(
            TokenCreate(
                principal_id="tenant-b-agent",
                tenant_id="tenant-b",
                scopes={Scope.INFERENCE_INVOKE},
                models={"qwen3-8b"},
            ),
            created_by="test-bootstrap",
        )
        return issued.id

    other_token_id = asyncio.run(seed_other_tenant())
    headers = {"cookie": f"{ADMIN_SESSION_COOKIE}={operator_cookie}"}
    with _client(runtime) as client:
        cross_tenant_patch = client.patch(
            f"/admin/api/v1/keys/{other_token_id}",
            headers=headers,
            json={"name": "must-not-change"},
        )
        missing_patch = client.patch(
            f"/admin/api/v1/keys/{uuid4()}",
            headers=headers,
            json={"name": "must-not-change"},
        )
        cross_tenant_rotate = client.post(
            f"/admin/api/v1/keys/{other_token_id}:rotate",
            headers=headers,
            json={},
        )
        cross_tenant_revoke = client.delete(
            f"/admin/api/v1/keys/{other_token_id}",
            headers=headers,
        )

    assert cross_tenant_patch.status_code == missing_patch.status_code == 404
    assert cross_tenant_rotate.status_code == cross_tenant_revoke.status_code == 404
    other_token = asyncio.run(runtime.store.get_token(other_token_id))
    assert other_token.name is None
    assert other_token.revoked_at is None
