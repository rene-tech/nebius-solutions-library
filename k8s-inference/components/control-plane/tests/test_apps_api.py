"""Exercise the mounted API contracts, not just isolated new services."""

from fs2_serve.access_models import OperatorRole
from fs2_serve.api import ADMIN_SESSION_COOKIE

from test_admin_access_api import (
    BOOTSTRAP_AUTH,
    _client,
    _create_principal,
    _principal_cookie,
    _runtime,
)


def test_apps_all_tabs_are_mounted_and_missing_observations_are_not_zero(registry, cipher, hasher):
    runtime = _runtime(registry, cipher, hasher)
    with _client(runtime) as client:
        assert client.get("/admin/api/v1/apps").status_code == 401
        assert client.post("/admin/api/v1/session", headers=BOOTSTRAP_AUTH).status_code == 200
        listing = client.get("/admin/api/v1/apps")
        assert listing.status_code == 200, listing.text
        items = listing.json()["data"]["items"]
        assert len(items) == len(registry.list())
        chosen = next(item for item in items if item["public_model_id"] == "qwen3-8b")
        base = f"/admin/api/v1/apps/{chosen['app_id']}"
        for tab in ("", "/runs", "/usage", "/settings", "/metrics", "/logs", "/containers"):
            response = client.get(base + tab)
            assert response.status_code == 200, (tab, response.text)
            assert "data" in response.json() and "meta" in response.json()
        metrics = client.get(base + "/metrics").json()["data"]
        assert len(metrics["charts"]) == 7
        assert all(chart["summary"]["average"] is None for chart in metrics["charts"])
        assert client.get(base + "/logs?cursor=invalid").status_code == 422


def test_users_and_capacity_summary_are_mounted(registry, cipher, hasher):
    with _client(_runtime(registry, cipher, hasher)) as client:
        assert client.post("/admin/api/v1/session", headers=BOOTSTRAP_AUTH).status_code == 200
        for path in ("/users", "/capacity/summary"):
            response = client.get("/admin/api/v1" + path)
            assert response.status_code == 200, response.text
        created = client.post(
            "/admin/api/v1/users",
            json={
                "tenant_id": "trial",
                "principal_id": "researcher",
                "display_name": "Researcher",
                "kind": "human",
                "academic_eligible": True,
            },
        )
        assert created.status_code == 201, created.text
        user = created.json()["data"]
        detail = client.get(f"/admin/api/v1/users/{user['id']}")
        assert detail.status_code == 200, detail.text
        assert len(detail.json()["data"]["apps"]) == len(registry.list())
        changed = client.patch(f"/admin/api/v1/users/{user['id']}", json={"enabled": False})
        assert changed.status_code == 200, changed.text
        assert changed.json()["data"]["enabled"] is False


def test_viewer_cannot_read_app_logs_but_operator_and_read_only_tabs_remain_available(
    registry, cipher, hasher
):
    runtime = _runtime(registry, cipher, hasher)
    viewer = _create_principal(runtime, role=OperatorRole.VIEWER, tenant_id=None, subject="logs-viewer")
    operator = _create_principal(runtime, role=OperatorRole.OPERATOR, tenant_id=None, subject="logs-operator")

    with _client(runtime) as client:
        client.cookies.set(ADMIN_SESSION_COOKIE, _principal_cookie(runtime, viewer))
        app_id = client.get("/admin/api/v1/apps").json()["data"]["items"][0]["app_id"]
        base = f"/admin/api/v1/apps/{app_id}"
        assert client.get(base + "/metrics").status_code == 200
        assert client.get(base + "/containers").status_code == 200
        assert client.get(base + "/logs").status_code == 403

        client.cookies.set(ADMIN_SESSION_COOKIE, _principal_cookie(runtime, operator))
        assert client.get(base + "/logs").status_code == 200
