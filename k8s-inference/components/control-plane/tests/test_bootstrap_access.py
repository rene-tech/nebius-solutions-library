from __future__ import annotations

import pytest

from fs2_serve.auth import AuthenticationError, PepperRing, TokenService
from fs2_serve.memory_store import MemoryStore
from fs2_serve.models import Scope, TokenCreate

BOOTSTRAP_PAT = "fs2_pat_1234567890abcdef1234567890abcdef_abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUV"


def request(*, models: set[str] | None = None, max_concurrency: int = 32) -> TokenCreate:
    return TokenCreate(
        principal_id="terraform-bootstrap-client",
        tenant_id="tenant-a",
        name="Terraform bootstrap MCP and inference",
        scopes={
            Scope.CATALOG_READ,
            Scope.INFERENCE_INVOKE,
            Scope.MCP_INVOKE,
            Scope.OPERATIONS_READ,
            Scope.OPERATIONS_RESULT,
            Scope.OPERATIONS_CANCEL,
            Scope.OPERATIONS_ACKNOWLEDGE,
            Scope.USE_NONCLINICAL,
            Scope.USE_NONCOMMERCIAL,
        },
        models=models or {"*"},
        max_concurrency=max_concurrency,
    )


@pytest.mark.asyncio
async def test_bootstrap_pat_is_idempotent_and_verifies_for_mcp_and_inference(cipher, hasher) -> None:
    store = MemoryStore(cipher, hasher)
    service = TokenService(store, PepperRing(active_key_id="pepper-v1", keys={"pepper-v1": b"p" * 32}))

    first = await service.ensure_provisioned(BOOTSTRAP_PAT, request(), created_by="terraform-bootstrap")
    second = await service.ensure_provisioned(BOOTSTRAP_PAT, request(), created_by="terraform-bootstrap")
    principal = await service.verify(BOOTSTRAP_PAT)

    assert first == second
    assert principal.token_id == first.id
    assert principal.principal_id == "terraform-bootstrap-client"
    assert principal.tenant_id == "tenant-a"
    assert Scope.MCP_INVOKE in principal.scopes
    assert Scope.INFERENCE_INVOKE in principal.scopes
    assert principal.permits_model("qwen3-8b")
    assert principal.permits_model("future-live-model")
    assert [event.action for event in store.audit].count("token.issue") == 1
    assert BOOTSTRAP_PAT not in repr(store.tokens)
    assert BOOTSTRAP_PAT not in repr(store.audit)


@pytest.mark.asyncio
async def test_bootstrap_pat_reconciles_mutable_policy_without_changing_material(cipher, hasher) -> None:
    store = MemoryStore(cipher, hasher)
    service = TokenService(store, PepperRing(active_key_id="pepper-v1", keys={"pepper-v1": b"p" * 32}))
    original = await service.ensure_provisioned(BOOTSTRAP_PAT, request(), created_by="terraform-bootstrap")

    reconciled = await service.ensure_provisioned(
        BOOTSTRAP_PAT,
        request(models={"qwen3-8b", "cosmos-nano"}, max_concurrency=16),
        created_by="terraform-bootstrap",
    )
    principal = await service.verify(BOOTSTRAP_PAT)

    assert reconciled.id == original.id
    assert reconciled.models == ["cosmos-nano", "qwen3-8b"]
    assert reconciled.max_concurrency == 16
    assert principal.permits_model("cosmos-nano")
    assert principal.max_concurrency == 16
    assert [event.action for event in store.audit].count("token.issue") == 1
    assert [event.action for event in store.audit].count("token.policy.update") == 1


@pytest.mark.asyncio
async def test_bootstrap_pat_refuses_reuse_of_token_id_with_other_secret(cipher, hasher) -> None:
    store = MemoryStore(cipher, hasher)
    service = TokenService(store, PepperRing(active_key_id="pepper-v1", keys={"pepper-v1": b"p" * 32}))
    await service.ensure_provisioned(BOOTSTRAP_PAT, request(), created_by="terraform-bootstrap")
    conflicting = BOOTSTRAP_PAT[:-1] + "W"

    with pytest.raises(AuthenticationError, match="identity conflicts"):
        await service.ensure_provisioned(conflicting, request(), created_by="terraform-bootstrap")
