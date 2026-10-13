import httpx
import pytest
from test_gateway import gateway_fixture  # noqa: F401

from scientific_ai.contracts import Owner
from scientific_ai.gateway import Gateway


def test_private_storage_never_falls_back_to_shared_or_sibling(request):
    original, *_ = request.getfixturevalue("gateway_fixture")
    shared = original.settings.tenants["system"]
    settings = original.settings.model_copy(
        update={
            "storage_modes": {"system": "user"},
            "user_storage": {
                "system": {
                    "alice": shared.model_copy(update={"bucket": "alice-private"}),
                    "bob": shared.model_copy(update={"bucket": "bob-private"}),
                }
            },
        }
    )
    gateway = Gateway(settings, storage_factory=lambda _: object())
    assert gateway.store(Owner(tenant_id="system", principal_id="alice")).bucket == "alice-private"
    assert gateway.store(Owner(tenant_id="system", principal_id="bob")).bucket == "bob-private"
    with pytest.raises(PermissionError):
        gateway.store(Owner(tenant_id="system", principal_id="missing"))


def test_retained_principal_is_stable_across_key_ids(request):
    original, *_ = request.getfixturevalue("gateway_fixture")

    def response(request):
        return httpx.Response(
            200,
            json={
                "id": request.headers["Authorization"],
                "principal_id": "qa",
                "tenant": "system",
                "models": [],
                "role": "user",
            },
        )

    gateway = Gateway(
        original.settings,
        storage_factory=lambda _: object(),
        http_factory=lambda **kw: httpx.Client(transport=httpx.MockTransport(response), **kw),
    )
    with gateway.client("Bearer first-key") as (_, first, _):
        with gateway.client("Bearer replacement-key") as (_, second, _):
            assert first.owner == second.owner == Owner(tenant_id="system", principal_id="qa")
            assert first.prefix == second.prefix


def test_shared_storage_stays_shared_but_operation_ownership_is_separate(request):
    gateway, *_ = request.getfixturevalue("gateway_fixture")
    first = gateway.store(Owner(tenant_id="system", principal_id="alice"))
    second = gateway.store(Owner(tenant_id="system", principal_id="bob"))
    assert first.bucket == second.bucket
    assert first.owner != second.owner
