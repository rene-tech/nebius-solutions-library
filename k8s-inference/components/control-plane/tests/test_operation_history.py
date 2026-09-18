"""Customer history must discover runs without broadening existing read access."""

from datetime import UTC, datetime
from uuid import UUID

from test_api_mcp import TestClient, build_runtime, issue

from fs2_serve.api import create_app


def submit(client, token, suffix):
    response = client.post(
        "/v1/chat/completions",
        headers={
            "authorization": f"Bearer {token}",
            "idempotency-key": "history-study-" + suffix,
            "x-fs2-wait-seconds": "0",
        },
        json={"model": "qwen3-8b", "messages": [{"role": "user", "content": "public test input"}]},
    )
    assert response.status_code == 202, response.text
    return response.json()["id"]


def test_customer_history_exact_owner_tenant_admin_and_stable_pagination(registry, cipher, hasher):
    runtime = build_runtime(registry, cipher, hasher)
    with TestClient(create_app(runtime)) as client:
        owner = issue(client, principal="scientist", scopes=["inference.invoke"])
        second_key = issue(client, principal="scientist", scopes=["inference.invoke"])
        colleague = issue(client, principal="colleague", scopes=["inference.invoke"])
        other_lab = issue(client, principal="scientist", tenant="other-lab", scopes=["inference.invoke"])
        administrator = issue(client, principal="lab-admin", scopes=["tenant.admin"])
        own_ids = [submit(client, owner, f"owner-{index}") for index in range(3)]
        same_person_other_key = submit(client, second_key, "second-key")
        colleague_id = submit(client, colleague, "colleague")
        foreign_id = submit(client, other_lab, "other-lab")
        # Tie timestamps to exercise deterministic UUID ordering. Scientific
        # operations share the same durable table and owner contract.
        for index, operation_id in enumerate(own_ids):
            row = runtime.store.operations[UUID(operation_id)]
            row.view = row.view.model_copy(
                update={
                    "accepted_at": datetime(2026, 9, 18, tzinfo=UTC),
                    "protocol": "scientific-batch-v1" if index == 0 else "openai-chat",
                }
            )
        headers = {"authorization": f"Bearer {owner}"}
        assert client.get("/v1/operations").status_code == 401
        first = client.get("/v1/operations?limit=2", headers=headers)
        assert first.status_code == 200, first.text
        assert first.headers["cache-control"] == "no-store"
        assert len(first.json()["data"]) == 2 and first.json()["next_cursor"]
        second = client.get(
            "/v1/operations", params={"limit": 2, "cursor": first.json()["next_cursor"]}, headers=headers
        )
        rows = first.json()["data"] + second.json()["data"]
        assert [row["id"] for row in rows] == sorted(own_ids, reverse=True)
        assert second.json()["next_cursor"] is None
        assert "scientific-batch-v1" in {row["protocol"] for row in rows}
        for operation_id in (same_person_other_key, colleague_id, foreign_id):
            assert client.get(f"/v1/operations/{operation_id}", headers=headers).status_code == 404
        admin_rows = client.get("/v1/operations", headers={"authorization": f"Bearer {administrator}"}).json()
        assert {row["id"] for row in admin_rows["data"]} == set(own_ids + [same_person_other_key, colleague_id])
        assert "public test input" not in first.text
        for query in ("limit=0", "limit=201"):
            assert client.get("/v1/operations?" + query, headers=headers).status_code == 422
        malformed = client.get("/v1/operations?cursor=not-a-cursor", headers=headers)
        assert malformed.status_code == 400
        assert malformed.json()["error"]["type"] == "invalid_cursor"


def test_history_cursor_does_not_skip_older_work_when_new_run_arrives(registry, cipher, hasher):
    runtime = build_runtime(registry, cipher, hasher)
    with TestClient(create_app(runtime)) as client:
        token = issue(client, principal="scientist", scopes=["inference.invoke"])
        old = submit(client, token, "old")
        recent = submit(client, token, "recent")
        headers = {"authorization": f"Bearer {token}"}
        first = client.get("/v1/operations?limit=1", headers=headers).json()
        assert first["data"][0]["id"] == recent
        new = submit(client, token, "new")
        second = client.get(
            "/v1/operations", params={"limit": 1, "cursor": first["next_cursor"]}, headers=headers
        ).json()
        assert [row["id"] for row in second["data"]] == [old]
        assert new not in {row["id"] for row in second["data"]}
