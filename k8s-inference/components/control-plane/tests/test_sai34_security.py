from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from fs2_serve.access import AdminAccessService
from fs2_serve.api import RejectTraceMiddleware
from fs2_serve.auth import PepperRing, TokenService
from fs2_serve.memory_store import MemoryStore
from fs2_serve.models import TOKEN_FINGERPRINT_PREFIX, Scope, TokenCreate


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
async def test_pat_fingerprint_is_versioned_truncated_and_rotation_uses_fresh_material(cipher, hasher) -> None:
    pepper = b"p" * 32
    store = MemoryStore(cipher, hasher)
    service = TokenService(store, PepperRing(active_key_id="pepper-v1", keys={"pepper-v1": pepper}))

    issued = await service.issue(token_request(), created_by="operator-a")
    expected = TOKEN_FINGERPRINT_PREFIX + hashlib.sha256(issued.token.encode()).hexdigest()[:32]
    assert issued.fingerprint == expected
    assert issued.fingerprint != hashlib.sha256(issued.token.encode()).hexdigest()

    rotated = await service.rotate(issued.id, actor="operator-a")
    rotated_expected = TOKEN_FINGERPRINT_PREFIX + hashlib.sha256(rotated.token.encode()).hexdigest()[:32]
    assert rotated.fingerprint == rotated_expected
    assert rotated.fingerprint != issued.fingerprint


@pytest.mark.asyncio
async def test_existing_unkeyed_bootstrap_fingerprint_migrates_on_proof_without_pepper_rotation(
    cipher, hasher
) -> None:
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
    persisted = await store.token_for_verification(issued.id)
    safe_fingerprint = TOKEN_FINGERPRINT_PREFIX + legacy[:32]

    assert reconciled.id == issued.id
    assert reconciled.fingerprint == safe_fingerprint
    assert persisted is not None
    assert persisted[0].fingerprint == safe_fingerprint


@pytest.mark.asyncio
async def test_operator_list_never_displays_legacy_full_fingerprint_before_proof(cipher, hasher) -> None:
    store = MemoryStore(cipher, hasher)
    service = TokenService(store, PepperRing(active_key_id="pepper-v1", keys={"pepper-v1": b"p" * 32}))
    issued = await service.issue(token_request(), created_by="operator-a")
    legacy = hashlib.sha256(issued.token.encode()).hexdigest()
    store.tokens[issued.id].view = store.tokens[issued.id].view.model_copy(update={"fingerprint": legacy})

    listed = await service.list(tenant_id="tenant-a")

    assert listed[0].fingerprint == TOKEN_FINGERPRINT_PREFIX + legacy[:32]
    assert legacy not in listed[0].model_dump_json()


@pytest.mark.asyncio
async def test_admin_projection_never_displays_legacy_full_fingerprint(cipher, hasher) -> None:
    store = MemoryStore(cipher, hasher)
    service = TokenService(
        store,
        PepperRing(active_key_id="pepper-v1", keys={"pepper-v1": b"p" * 32}),
    )
    issued = await service.issue(token_request(), created_by="operator-a")
    legacy = hashlib.sha256(issued.token.encode()).hexdigest()
    legacy_view = store.tokens[issued.id].view.model_copy(update={"fingerprint": legacy})

    projected = await AdminAccessService(store, service)._project_keys([legacy_view], tenant_id="tenant-a")

    assert projected[0].fingerprint == TOKEN_FINGERPRINT_PREFIX + legacy[:32]
    assert legacy not in projected[0].model_dump_json()


@pytest.mark.asyncio
async def test_pepper_rotation_preserves_stable_bootstrap_fingerprint(cipher, hasher) -> None:
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
    expected = issued.fingerprint
    persisted = await store.token_for_verification(issued.id)

    assert reconciled.pepper_key_id == "pepper-v2"
    assert reconciled.fingerprint == expected
    assert persisted is not None
    assert persisted[0].pepper_key_id == "pepper-v2"
    assert persisted[0].fingerprint == expected


@pytest.mark.asyncio
async def test_pepper_rotation_uses_only_the_atomic_verifier_update(cipher, hasher, monkeypatch) -> None:
    old_pepper = b"o" * 32
    new_pepper = b"n" * 32
    store = MemoryStore(cipher, hasher)
    original = TokenService(store, PepperRing(active_key_id="pepper-v1", keys={"pepper-v1": old_pepper}))
    issued = await original.issue(token_request(), created_by="operator-a")

    assert not hasattr(store, "rehash_token")
    atomic_updates = 0
    atomic_update = store.rehash_token_with_fingerprint

    async def record_atomic_update(*args, **kwargs) -> None:
        nonlocal atomic_updates
        atomic_updates += 1
        await atomic_update(*args, **kwargs)

    monkeypatch.setattr(store, "rehash_token_with_fingerprint", record_atomic_update)
    rotating = TokenService(
        store,
        PepperRing(
            active_key_id="pepper-v2",
            keys={"pepper-v1": old_pepper, "pepper-v2": new_pepper},
        ),
    )

    principal = await rotating.verify(issued.token)
    persisted = await store.token_for_verification(issued.id)

    assert principal.token_id == issued.id
    assert atomic_updates == 1
    assert persisted is not None
    assert persisted[0].pepper_key_id == "pepper-v2"
    assert persisted[0].fingerprint == issued.fingerprint


def test_fingerprint_migration_is_expand_only_for_mixed_version_rollout() -> None:
    control_root = Path(__file__).resolve().parents[1]
    migration = (control_root / "migrations/0030_versioned_pat_fingerprints.sql").read_text(encoding="utf-8")
    postgres = (control_root / "src/fs2_serve/postgres.py").read_text(encoding="utf-8")

    assert "ADD COLUMN operator_fingerprint" in migration
    assert "fingerprint = NULL" in migration
    assert "CREATE TRIGGER fs2_tokens_normalize_fingerprint" in migration
    assert "BEFORE INSERT OR UPDATE OF fingerprint ON fs2_tokens" in migration
    assert "NEW.operator_fingerprint := normalized" in migration
    assert "NEW.fingerprint := NULL" in migration
    assert "DROP COLUMN" not in migration
    assert "ALTER COLUMN fingerprint" not in migration
    assert 'fingerprint=row["operator_fingerprint"] or row["fingerprint"]' in postgres
    assert "operator_fingerprint=$4,fingerprint=NULL" in postgres


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
