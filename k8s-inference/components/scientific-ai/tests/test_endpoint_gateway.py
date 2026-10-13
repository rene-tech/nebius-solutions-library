"""Offline REST/MCP integration and tenant boundaries, not GPU qualification."""

import base64
import hashlib
import json

import httpx
import pytest
from test_gateway import gateway_fixture  # noqa: F401

from scientific_ai.endpoint_catalog import catalog
from scientific_ai.endpoint_gateway import OPERATIONS


@pytest.fixture
def endpoint_gateway(request):
    gateway, client, storage, _, _, _ = request.getfixturevalue("gateway_fixture")
    original_factory = gateway.http_factory
    state = {
        "models": {
            mid: {
                "id": mid,
                "protocol": "native",
                "modes": ["sync"],
                "image": "registry.example/test@sha256:" + "a" * 64,
                "regions": [{"region": "test-a", "status": "scaled-to-zero", "replicas_ready": 0}],
            }
            for mid in catalog()
        },
        "scopes": ["read:catalog", "invoke:sync", "read:operations", "read:results", "artifacts:write"],
        "allowed": ["admet-ai"],
        "legacy": None,
        "calls": [],
        "response_status": 200,
        "result": {"molecules": [{"id": "m1", "prediction": 0.25}]},
        "fail": None,
    }

    def upstream(request):
        auth = request.headers.get("Authorization")
        path = request.url.path
        if path == "/v1/keys/me" and auth != "Bearer invalid":
            return httpx.Response(
                200,
                json={
                    "id": auth.removeprefix("Bearer "),
                    "tenant": "system",
                    "role": "user",
                    "models": [] if auth == "Bearer qa" else state["allowed"],
                    "scopes": [] if auth == "Bearer qa" else state["scopes"],
                    "legacy": state["legacy"],
                },
            )
        if path == "/v1/models":
            return httpx.Response(200, json=list(state["models"].values()))
        mid = path.removeprefix("/v1/models/").removesuffix(":invoke")
        if mid in state["models"]:
            if request.method == "GET":
                return httpx.Response(200, json=state["models"][mid])
            state["calls"].append((auth, mid, json.loads(request.content), request.extensions["timeout"]))
            if state["fail"] == "timeout":
                raise httpx.ReadTimeout("private-input-do-not-log", request=request)
            if state["fail"] == "admission":
                return httpx.Response(429, json={"detail": "private-upstream-error"})
            value = {
                "operation": {"id": "sync-native-1", "status": "SUCCEEDED", "input": "private"},
                "result": state["result"],
            }
            if state["response_status"] != 200:
                value["operation"]["status"] = "FAILED"
                value["operation"]["error"] = "private-upstream-error"
            return httpx.Response(state["response_status"], json=value)
        with original_factory(base_url="https://s2.example") as fallback:
            return fallback.send(request)

    gateway.http_factory = lambda **kw: httpx.Client(transport=httpx.MockTransport(upstream), **kw)
    client.headers["Authorization"] = "Bearer endpoint-key"
    yield gateway, client, storage, state


def rpc(client, name, arguments=None, *, method="tools/call", headers=None):
    return client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": method,
            "params": {"name": name, "arguments": arguments or {}} if method == "tools/call" else {},
        },
        headers={"Accept": "application/json, text/event-stream", **(headers or {})},
    )


def tool_value(response):
    result = response.json()["result"]
    assert not result.get("isError"), result
    return result.get("structuredContent") or json.loads(result["content"][0]["text"])


def invoke(client, mid="admet-ai", payload=None, **kwargs):
    return client.post(
        f"/v1/models/{mid}:invoke",
        json={
            "operation": OPERATIONS[mid],
            "payload": payload or {"smiles": "CCO"},
        },
        **kwargs,
    )


def reserve(client, content, *, mid="admet-ai", media_type="application/json"):
    response = client.post(
        "/v1/scientific-artifacts/uploads",
        json={
            "model_id": mid,
            "sha256": hashlib.sha256(content).hexdigest(),
            "size_bytes": len(content),
            "media_type": media_type,
        },
    )
    assert response.status_code == 200, response.text
    value = response.json()
    response = client.put(value["upload_url"], content=content)
    assert response.status_code == 200, response.text
    response = client.post(f"/v1/scientific-artifacts/uploads/{value['upload_id']}:finalize")
    assert response.status_code == 200, response.text
    return response.json()["artifact"]


def test_discovery_intersects_live_native_binding_and_key_model_grants(endpoint_gateway):
    _, client, _, state = endpoint_gateway
    result = client.get("/v1/models")
    assert result.status_code == 200
    assert result.json()["object"] == "list"
    assert [row["id"] for row in result.json()["data"]] == ["admet-ai"]
    model = result.json()["data"][0]
    assert model["runtime_identity"].endswith("a" * 64)
    assert model["regions"][0]["replicas_ready"] == 0
    assert model["execution"]["durable_async"] is False
    tools = rpc(client, "", method="tools/list").json()["result"]["tools"]
    names = {tool["name"] for tool in tools}
    assert "infer_admet_ai_native" in names
    assert "infer_ctoxpred2_native" not in names and "submit_gromacs_workflow" not in names
    schema = next(tool for tool in tools if tool["name"] == "infer_admet_ai_native")["inputSchema"]
    assert "smiles" in schema["properties"] and "payload" not in schema["properties"]
    state["models"].pop("admet-ai")
    assert client.get("/v1/models").json()["data"] == []
    assert client.get("/v1/models/admet-ai/schema").status_code == 404
    assert "infer_admet_ai_native" not in {
        tool["name"] for tool in rpc(client, "", method="tools/list").json()["result"]["tools"]
    }


def test_all_packaged_tools_keep_exact_public_name_and_typed_fields(endpoint_gateway):
    _, client, _, _ = endpoint_gateway
    tools = rpc(client, "", method="tools/list", headers={"Authorization": "Bearer qa"}).json()["result"][
        "tools"
    ]
    tools = {tool["name"]: tool for tool in tools}
    for contract in catalog().values():
        assert tools[contract.tool_name]["inputSchema"] == contract.schema("mcp")


def test_rest_and_mcp_use_original_key_and_actual_s2_id(endpoint_gateway):
    _, client, _, state = endpoint_gateway
    response = invoke(client)
    assert response.status_code == 200, response.text
    value = response.json()
    assert value["operation"] == {"id": "sync-native-1", "status": "SUCCEEDED"}
    assert response.headers["x-fs2-operation-id"] == "sync-native-1"
    assert "private" not in response.text
    mcp = tool_value(rpc(client, "infer_admet_ai_native", {"smiles": "CCO"}))
    assert mcp == value
    assert len(state["calls"]) == 2
    for auth, mid, body, timeout in state["calls"]:
        assert auth == "Bearer endpoint-key" and mid == "admet-ai"
        assert body == {"mode": "sync", "input": {"smiles": "CCO"}, "timeout_s": 900}
        assert timeout["read"] == 915
    assert client.get("/v1/operations/sync-native-1/result").json() == value
    assert tool_value(rpc(client, "get_scientific_result", {"operation_id": "sync-native-1"})) == value
    assert (
        client.get(
            "/v1/operations/sync-native-1/result", headers={"Authorization": "Bearer sibling"}
        ).status_code
        == 404
    )


def test_native_keys_cannot_use_gromacs_and_scope_limits_apply(endpoint_gateway):
    _, client, _, state = endpoint_gateway
    assert client.get("/v1/models/gromacs/schema").status_code == 403
    assert invoke(client, "ctoxpred2").status_code == 403
    state["scopes"] = ["read:catalog"]
    assert invoke(client).status_code == 403
    assert client.get("/v1/operations/sync-native-1/result").status_code == 403
    assert client.post("/v1/scientific-artifacts/uploads", json={"model_id": "admet-ai"}).status_code == 403
    tools = rpc(client, "", method="tools/list").json()["result"]["tools"]
    assert "infer_admet_ai_native" not in {tool["name"] for tool in tools}
    hidden = tool_value(rpc(client, "infer_admet_ai_native", {"smiles": "CCO"}))
    assert hidden["error"]["status_code"] == 403
    assert not state["calls"]


@pytest.mark.parametrize("nested", [False, True])
def test_imported_no_mcp_scope_blocks_mcp_without_blocking_rest(endpoint_gateway, nested):
    _, client, _, state = endpoint_gateway
    legacy = {"original_scopes": ["catalog.read", "inference.invoke"]}
    state["legacy"] = {"metadata": legacy} if nested else legacy
    assert invoke(client).status_code == 200
    assert rpc(client, "", method="tools/list").status_code == 403
    legacy["original_scopes"].append("mcp.invoke")
    assert rpc(client, "", method="tools/list").status_code == 200


@pytest.mark.parametrize("header", ["Idempotency-Key", "x-fs2-wait-seconds", "x-fs2-deadline-seconds"])
def test_unsupported_native_execution_options_are_rejected_before_admission(endpoint_gateway, header):
    _, client, _, state = endpoint_gateway
    response = invoke(client, headers={header: "anything"})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "NATIVE_EXECUTION_OPTION_UNSUPPORTED"
    assert not state["calls"]


def test_mcp_rejects_unsupported_idempotency_explicitly(endpoint_gateway):
    _, client, _, state = endpoint_gateway
    response = rpc(client, "infer_admet_ai_native", {"smiles": "CCO", "idempotency_key": "not-supported"})
    assert response.json()["error"]["data"]["code"] == "NATIVE_EXECUTION_OPTION_UNSUPPORTED"
    assert not state["calls"]


def test_private_input_artifact_materialization_and_cross_owner_failure(endpoint_gateway):
    _, client, _, state = endpoint_gateway
    data = b'[{"id":"m1","smiles":"CCO"}]'
    pointer = reserve(client, data)
    response = invoke(client, payload={"molecules": pointer})
    assert response.status_code == 200, response.text
    assert state["calls"][-1][2]["input"] == {"molecules": json.loads(data)}
    response = invoke(client, payload={"molecules": pointer}, headers={"Authorization": "Bearer sibling"})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "INVALID_OR_UNAUTHORIZED_ARTIFACT"
    assert len(state["calls"]) == 1


def test_binary_results_are_artifacts_not_model_context(endpoint_gateway):
    _, client, _, state = endpoint_gateway
    data = b"synthetic zipped structures"
    state["result"] = {
        "encoding": "base64",
        "media_type": "application/zip",
        "data": base64.b64encode(data).decode(),
        "size_bytes": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
    }
    value = tool_value(rpc(client, "infer_admet_ai_native", {"smiles": "CCO"}))
    pointer = value["result"]["artifact"]
    assert "data" not in value["result"]
    response = client.get(f"/v1/artifacts/{pointer['artifact_id']}/content")
    assert response.content == data and response.headers["content-type"] == "application/zip"
    assert (
        client.get(
            f"/v1/artifacts/{pointer['artifact_id']}/content", headers={"Authorization": "Bearer sibling"}
        ).status_code
        == 404
    )


@pytest.mark.parametrize("failure", ["publication", "receipt"])
def test_publication_failure_never_loses_accepted_id(endpoint_gateway, monkeypatch, failure):
    _, client, storage, state = endpoint_gateway
    state["result"] = {"large": "a" * 70000}
    method = "upload_fileobj" if failure == "publication" else "put_object"

    def fail(*args, **kwargs):
        raise OSError("private-storage-error")

    monkeypatch.setattr(storage, method, fail)
    response = invoke(client)
    assert response.status_code == 502
    assert response.headers["x-fs2-operation-id"] == "sync-native-1"
    assert response.json()["error"]["operation_id"] == "sync-native-1"
    assert response.json()["error"]["admission"] == "accepted"
    assert response.json()["error"]["retryable"] is False
    assert "private-storage-error" not in response.text
    assert len(state["calls"]) == 1


@pytest.mark.parametrize("failure", ["timeout", "admission", "runtime"])
def test_native_failure_is_safe_and_never_retried(endpoint_gateway, failure):
    _, client, _, state = endpoint_gateway
    state["fail"] = failure
    state["response_status"] = 422 if failure == "runtime" else 200
    response = invoke(client)
    assert response.status_code == {"timeout": 504, "admission": 429, "runtime": 422}[failure]
    error = response.json()["error"]
    assert error["retryable"] is False
    assert (
        error["admission"]
        == {"timeout": "unknown", "admission": "not-confirmed", "runtime": "accepted"}[failure]
    )
    assert "private" not in response.text and len(state["calls"]) == 1
    if failure == "runtime":
        assert error["operation_id"] == "sync-native-1"


def test_wrong_native_operation_and_fields_never_invoke(endpoint_gateway):
    _, client, _, state = endpoint_gateway
    response = client.post(
        "/v1/models/admet-ai:invoke", json={"operation": "generate-molecule", "payload": {"smiles": "CCO"}}
    )
    assert response.status_code == 422
    assert invoke(client, payload={"unknown": "private-value"}).status_code == 422
    assert not state["calls"]
