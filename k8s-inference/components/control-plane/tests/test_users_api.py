"""Users mount through the production admin dependencies and shared key service."""

from test_admin_access_api import BOOTSTRAP_AUTH, _client, _runtime


def test_user_crud_keys_and_disable_use_real_api_contract(registry, cipher, hasher):
    runtime = _runtime(registry, cipher, hasher)
    with _client(runtime) as client:
        assert client.get("/admin/api/v1/users").status_code == 401
        assert client.post("/admin/api/v1/session", headers=BOOTSTRAP_AUTH).status_code == 200
        response = client.post(
            "/admin/api/v1/users",
            json={
                "display_name": "Customer researcher",
                "kind": "human",
                "principal_id": "customer-researcher",
                "tenant_id": "customer-team",
                "academic_eligible": True,
            },
        )
        assert response.status_code == 201, response.text
        user = response.json()["data"]
        assert user["principal_id"] == "customer-researcher"
        key_response = client.post(
            f"/admin/api/v1/users/{user['id']}/keys",
            json={
                "name": "Trial key",
                "principal_id": "customer-researcher",
                "tenant_id": "customer-team",
                "models": ["qwen3-8b"],
                "scopes": ["inference.invoke", "mcp.invoke", "operations.read"],
                "max_concurrency": 1,
            },
        )
        assert key_response.status_code == 201, key_response.text
        issued = key_response.json()["data"]
        detail = client.get(f"/admin/api/v1/users/{user['id']}")
        assert detail.status_code == 200, detail.text
        assert detail.json()["data"]["user"]["key_count"] == 1
        assert detail.json()["data"]["user"]["usage"]["requests"] == 0
        assert "secret" not in detail.json()["data"]["keys"][0]
        assert len(detail.json()["data"]["user"]["usage"]["request_series"]) == 60
        disabled = client.patch(f"/admin/api/v1/users/{user['id']}", json={"enabled": False})
        assert disabled.status_code == 200, disabled.text
        rejected = client.post(
            "/v1/chat/completions",
            headers={"Authorization": f"Bearer {issued['secret']}"},
            json={"model": "qwen3-8b", "messages": [{"role": "user", "content": "test"}]},
        )
        assert rejected.status_code == 403, rejected.text
        revoked = client.delete(f"/admin/api/v1/keys/{issued['key']['id']}")
        assert revoked.status_code == 200, revoked.text
        assert revoked.json()["data"]["state"] == "revoked"


def test_capacity_summary_mount_preserves_unknown_when_no_live_adapter(registry, cipher, hasher):
    with _client(_runtime(registry, cipher, hasher)) as client:
        assert client.post("/admin/api/v1/session", headers=BOOTSTRAP_AUTH).status_code == 200
        response = client.get("/admin/api/v1/capacity/summary")
        assert response.status_code == 200, response.text
        data = response.json()["data"]
        assert data["pools"] == []
        assert data["gpu_utilization_percent"]["value"] is None
        assert data["loaded_idle_gpus"]["value"] is None
        assert data["pending_customer_runs"]["value"] == 0
