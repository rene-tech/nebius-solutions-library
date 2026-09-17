from __future__ import annotations

import hashlib
import hmac
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from fs2_serve.api import RejectTraceMiddleware
from fs2_serve.auth import PAT_FINGERPRINT_CONTEXT, PepperRing, TokenService
from fs2_serve.memory_store import MemoryStore
from fs2_serve.models import Scope, TokenCreate


def token_request() -> TokenCreate:
    return TokenCreate(
        principal_id="sai34-user",
        tenant_id="tenant-a",
        scopes={Scope.CATALOG_READ},
        models={"qwen3-8b"},
        max_concurrency=1,
        name="SAI-34 regression key",
    )


@pytest.mark.asyncio
async def test_pat_fingerprint_is_keyed_and_rotation_uses_fresh_material(cipher, hasher) -> None:
    pepper = b"p" * 32
    store = MemoryStore(cipher, hasher)
    service = TokenService(store, PepperRing(active_key_id="pepper-v1", keys={"pepper-v1": pepper}))

    issued = await service.issue(token_request(), created_by="operator-a")
    expected = hmac.new(pepper, PAT_FINGERPRINT_CONTEXT + issued.token.encode(), hashlib.sha256).hexdigest()
    assert issued.fingerprint == expected
    assert issued.fingerprint != hashlib.sha256(issued.token.encode()).hexdigest()

    rotated = await service.rotate(issued.id, actor="operator-a")
    rotated_expected = hmac.new(
        pepper,
        PAT_FINGERPRINT_CONTEXT + rotated.token.encode(),
        hashlib.sha256,
    ).hexdigest()
    assert rotated.fingerprint == rotated_expected
    assert rotated.fingerprint != issued.fingerprint


@pytest.mark.asyncio
async def test_existing_unkeyed_bootstrap_fingerprint_remains_reconcilable(cipher, hasher) -> None:
    pepper = b"p" * 32
    store = MemoryStore(cipher, hasher)
    service = TokenService(store, PepperRing(active_key_id="pepper-v1", keys={"pepper-v1": pepper}))
    issued = await service.issue(token_request(), created_by="terraform-bootstrap")
    legacy = hashlib.sha256(issued.token.encode()).hexdigest()
    store.tokens[issued.id].view = store.tokens[issued.id].view.model_copy(update={"fingerprint": legacy})

    reconciled = await service.ensure_provisioned(
        issued.token,
        token_request(),
        created_by="terraform-bootstrap",
    )

    assert reconciled.id == issued.id
    assert reconciled.fingerprint == legacy


@pytest.mark.asyncio
async def test_pepper_rotation_atomically_rekeys_bootstrap_fingerprint(cipher, hasher) -> None:
    old_pepper = b"o" * 32
    new_pepper = b"n" * 32
    store = MemoryStore(cipher, hasher)
    original = TokenService(store, PepperRing(active_key_id="pepper-v1", keys={"pepper-v1": old_pepper}))
    issued = await original.issue(token_request(), created_by="terraform-bootstrap")
    rotating = TokenService(
        store,
        PepperRing(
            active_key_id="pepper-v2",
            keys={"pepper-v1": old_pepper, "pepper-v2": new_pepper},
        ),
    )

    reconciled = await rotating.ensure_provisioned(
        issued.token,
        token_request(),
        created_by="terraform-bootstrap",
    )
    expected = hmac.new(
        new_pepper,
        PAT_FINGERPRINT_CONTEXT + issued.token.encode(),
        hashlib.sha256,
    ).hexdigest()
    persisted = await store.token_for_verification(issued.id)

    assert reconciled.pepper_key_id == "pepper-v2"
    assert reconciled.fingerprint == expected
    assert persisted is not None
    assert persisted[0].pepper_key_id == "pepper-v2"
    assert persisted[0].fingerprint == expected


def test_trace_is_rejected_before_application_dispatch_without_header_echo() -> None:
    app = FastAPI()

    @app.api_route("/{path:path}", methods=["GET", "TRACE"])
    async def downstream(path: str) -> dict[str, str]:
        return {"path": path}

    app.add_middleware(RejectTraceMiddleware)
    marker = "sai34-trace-marker"
    with TestClient(app) as client:
        denied = client.request("TRACE", "/", headers={"x-sai34-marker": marker})
        allowed = client.get("/readyz")

    assert denied.status_code == 405
    assert marker not in denied.text
    assert denied.headers["cache-control"] == "no-store"
    assert denied.headers["x-content-type-options"] == "nosniff"
    assert allowed.status_code == 200
    assert allowed.json() == {"path": "readyz"}


def test_retired_user_storage_cloud_tenant_setting_is_not_reintroduced() -> None:
    source_root = Path(__file__).resolve().parents[1] / "src/fs2_serve"
    matches = [path for path in source_root.glob("*.py") if "user_storage_cloud_tenant_id" in path.read_text()]
    assert matches == []
