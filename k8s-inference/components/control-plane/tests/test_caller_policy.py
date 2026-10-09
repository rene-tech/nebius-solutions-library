"""Read-only caller policy comes from verified identity, never supplied headers."""
import pytest
from test_api_mcp import TestClient, build_runtime, issue

from fs2_serve.api import create_app


@pytest.mark.parametrize("scopes", [["catalog.read"], ["inference.invoke"], ["mcp.invoke"]])
def test_caller_policy_is_authenticated_effective_and_non_mutating(registry, cipher, hasher, scopes):
    runtime = build_runtime(registry, cipher, hasher)
    with TestClient(create_app(runtime)) as client:
        assert client.get("/v1/me").status_code == 401
        assert client.get("/v1/me", headers={"authorization": "Bearer invalid"}).status_code == 401
        token = issue(client, principal="scientist", tenant="research-lab", scopes=scopes)
        response = client.get("/v1/me", headers={
            "authorization": f"Bearer {token}",
            "x-fs2-principal": "somebody-else", "x-fs2-max-concurrency": "99",
        })
        assert response.status_code == 200
        value = response.json()
        assert value["principal_id"] == "scientist"
        assert value["tenant_id"] == "research-lab"
        assert value["scopes"] == scopes
        assert value["models"] == ["qwen3-8b"]
        assert value["max_concurrency"] == 4
        assert value["concurrency_scope"] == "api_key"
        assert value["concurrency_counted_states"] == ["queued", "activating", "running"]
        assert value["available_slots"] is None
        assert response.headers["cache-control"] == "private, no-store"
        assert token not in response.text
        assert "token_id" not in value and "token_prefix" not in value
        assert not runtime.store.operations
        token_id = next(iter(runtime.store.tokens))
        assert client.delete(f"/admin/v1/tokens/{token_id}",
                             headers={"authorization": f"Bearer {'a' * 32}"}).status_code == 200
        assert client.get("/v1/me", headers={"authorization": f"Bearer {token}"}).status_code == 401
