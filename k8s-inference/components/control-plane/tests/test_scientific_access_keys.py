"""Scientific model allowlists share key lifecycle, not interactive routing."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient
from test_admin_access_api import BOOTSTRAP_AUTH
from test_scientific_batch_production import profile_value, scientific_runtime

from fs2_serve.api import create_app
from fs2_serve.scientific_batch.profile_catalog import ScientificProfileError


def key_request(models: list[str]) -> dict[str, Any]:
    return {
        "name": "scientific customer",
        "principal_id": "scientist-a",
        "tenant_id": "tenant-a",
        "scopes": ["catalog.read", "inference.invoke", "mcp.invoke"],
        "models": models,
        "max_concurrency": 1,
    }


def test_scientific_key_create_update_rotation_and_revocation(registry, cipher, hasher) -> None:
    runtime, _, _, cluster, _ = scientific_runtime(registry, cipher, hasher)
    with TestClient(create_app(runtime), base_url="https://inference.test.invalid") as client:
        assert client.post("/admin/api/v1/session", headers=BOOTSTRAP_AUTH).status_code == 200
        issued = client.post("/admin/api/v1/keys", json=key_request(["protein-design"]))
        assert issued.status_code == 201, issued.text
        key = issued.json()["data"]
        token_id = key["key"]["id"]
        headers = {"authorization": f"Bearer {key['secret']}"}
        discovered = client.get("/v1/scientific-models", headers=headers)
        assert discovered.status_code == 200
        assert [item["model_id"] for item in discovered.json()["data"]] == ["protein-design"]
        assert client.get("/v1/models", headers=headers).json()["data"] == []
        denied = client.post(
            "/v1/models/qwen3-8b:submit",
            headers={**headers, "Idempotency-Key": "scientific-access-denied-001"},
            json={},
        )
        assert denied.status_code == 403

        # Unknown IDs remain rejected and a rejected edit leaves the policy intact.
        assert client.post("/admin/api/v1/keys", json=key_request(["not-a-model"])).status_code == 404
        assert client.patch(
            f"/admin/api/v1/keys/{token_id}", json={"models": ["not-a-model"]}
        ).status_code == 404
        assert client.patch(
            f"/admin/api/v1/keys/{token_id}", json={"models": ["protein-design", "qwen3-8b"]}
        ).status_code == 200
        narrowed = client.patch(
            f"/admin/api/v1/keys/{token_id}", json={"models": ["protein-design"]}
        )
        assert narrowed.status_code == 200
        assert narrowed.json()["data"]["models"] == ["protein-design"]

        rotated = client.post(f"/admin/api/v1/keys/{token_id}:rotate", json={})
        assert rotated.status_code == 201
        successor = rotated.json()["data"]
        assert successor["key"]["models"] == ["protein-design"]
        assert client.get("/v1/scientific-models", headers=headers).status_code == 401
        successor_headers = {"authorization": f"Bearer {successor['secret']}"}
        assert client.get("/v1/scientific-models", headers=successor_headers).status_code == 200
        assert client.delete(f"/admin/api/v1/keys/{successor['key']['id']}").status_code == 200
        assert client.get("/v1/scientific-models", headers=successor_headers).status_code == 401
    assert not cluster.apply_history


def test_legacy_token_endpoint_accepts_only_configured_scientific_ids(registry, cipher, hasher) -> None:
    runtime, _, _, _, _ = scientific_runtime(registry, cipher, hasher)
    with TestClient(create_app(runtime), base_url="https://inference.test.invalid") as client:
        issued = client.post("/admin/v1/tokens", headers=BOOTSTRAP_AUTH, json=key_request(["protein-design"]))
        assert issued.status_code == 200, issued.text
        assert issued.json()["models"] == ["protein-design"]
        assert client.post(
            "/admin/v1/tokens", headers=BOOTSTRAP_AUTH, json=key_request(["not-a-model"])
        ).status_code == 404


@pytest.mark.parametrize("unavailable", ["candidate", "tenant-license"])
def test_scientific_allowlist_does_not_grant_availability_or_tenant_license(
    registry, cipher, hasher, monkeypatch: pytest.MonkeyPatch, unavailable: str
) -> None:
    document = profile_value()
    if unavailable == "candidate":
        document = {**document, "state": "candidate", "route_exposed": False}
    runtime, _, repository, cluster, pointer = scientific_runtime(registry, cipher, hasher, profile_document=document)
    assert runtime.scientific_batches is not None
    if unavailable == "tenant-license":
        def unavailable_license(*args: Any, **kwargs: Any) -> None:
            raise ScientificProfileError("tenant is not authorized for the licensed artifact")

        monkeypatch.setattr(runtime.scientific_batches.execution_binding, "access_context", unavailable_license)
    with TestClient(create_app(runtime), base_url="https://inference.test.invalid") as client:
        assert client.post("/admin/api/v1/session", headers=BOOTSTRAP_AUTH).status_code == 200
        issued = client.post("/admin/api/v1/keys", json=key_request(["protein-design"]))
        assert issued.status_code == 201, issued.text
        headers = {"authorization": f"Bearer {issued.json()['data']['secret']}"}
        discovered = client.get("/v1/scientific-models", headers=headers)
        assert discovered.status_code == 200
        assert discovered.json()["data"] == []
        submitted = client.post(
            "/v1/models/protein-design:submit",
            headers={**headers, "Idempotency-Key": "scientific-access-unavailable-001"},
            json={
                "schema": "fs2-serve.nebius.ai/scientific-run-request/v1",
                "operation": "design",
                "service_class": "customer-batch",
                "input_manifest": pointer,
                "parameters": {},
            },
        )
        assert submitted.status_code == 503
    assert not repository.records
    assert not cluster.apply_history
