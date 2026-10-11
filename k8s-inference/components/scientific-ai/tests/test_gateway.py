import hashlib
import io
import json

import httpx
import jwt
import pytest
from botocore.exceptions import ClientError
from fastapi.testclient import TestClient

from scientific_ai.contracts import FileRecord, Owner, Snapshot
from scientific_ai.gateway import Gateway, Settings, create_app
from scientific_ai.gateway_store import canonical


class S3:
    def __init__(self):
        self.data = {}

    def put_object(self, *, Bucket, Key, Body, IfNoneMatch=None, **kwargs):
        if IfNoneMatch == "*" and Key in self.data:
            raise ClientError({"Error": {"Code": "PreconditionFailed"}}, "PutObject")
        self.data[Key] = Body

    def get_object(self, *, Bucket, Key):
        if Key not in self.data:
            raise ClientError({"Error": {"Code": "NoSuchKey"}}, "GetObject")
        return {"Body": io.BytesIO(self.data[Key])}

    def upload_fileobj(self, source, bucket, key, **kwargs):
        self.data[key] = source.read()

    def generate_presigned_url(self, method, *, Params, ExpiresIn):
        assert ExpiresIn == 60
        return "https://storage.example/" + Params["Key"]

    def copy(self, source, bucket, target):
        self.data[target] = self.data[source["Key"]]


@pytest.fixture
def gateway_fixture(setup):
    runtime = setup[0]["profile"]
    storage = S3()
    calls, operations, bindings = [], {}, {}

    def upstream(request):
        path = request.url.path
        calls.append((request.method, path))
        if path == "/v1/keys/me":
            if request.headers["Authorization"] == "Bearer invalid":
                return httpx.Response(401, json={"error": "bad token"})
            return httpx.Response(
                200,
                json={
                    "id": request.headers["Authorization"].split()[1],
                    "tenant": "system",
                    "role": "user",
                    "models": [],
                },
            )
        if path == "/v1/fleet":
            return httpx.Response(
                200,
                json={
                    "execution": {
                        "io_modes": ["application"],
                        "execution_profiles": ["confidential-job-v1"],
                        "identities": [],
                        "private_input_dir": "/var/run/serverless2/private",
                    }
                },
            )
        if path.startswith("/v1/bindings/"):
            bindings[path] = json.loads(request.content)["data"]
            return httpx.Response(200, json={"state": "bound"})
        if path.endswith(":invoke"):
            op = "op-scientific-gromacs-1"
            operations.setdefault(
                op,
                {
                    "id": op,
                    "root_operation_id": op,
                    "status": "QUEUED",
                    "execution": {"io_mode": "application"},
                    "attempts": [],
                },
            )
            return httpx.Response(202, json=operations[op])
        if path.endswith(":resume"):
            op = "op-scientific-gromacs-1-r1"
            operations.setdefault(
                op, {"id": op, "root_operation_id": "op-scientific-gromacs-1", "status": "QUEUED"}
            )
            assert request.headers["Idempotency-Key"].startswith("sai-")
            return httpx.Response(202, json=operations[op])
        op = path.rsplit("/", 1)[-1]
        if op in operations:
            return httpx.Response(200, json=operations[op])
        return httpx.Response(404, json={})

    settings = Settings(
        serverless_url="https://s2.example",
        public_url="https://testserver",
        runtime=runtime,
        signing_key="private-test-only-key-" + "a" * 32,
        tenants={
            "system": {
                "endpoint": "https://storage.example",
                "region": "test-a",
                "bucket": "test-only",
                "prefix": "runs/s2-pilot",
                "access_key": "test",
                "secret_key": "test",
                "execution_key": "qa",
            }
        },
    )
    gateway = Gateway(
        settings,
        storage_factory=lambda tenant: storage,
        http_factory=lambda **kw: httpx.Client(transport=httpx.MockTransport(upstream), **kw),
    )
    app = create_app(gateway=gateway)
    with TestClient(app, base_url="https://testserver", headers={"Authorization": "Bearer qa"}) as client:
        yield gateway, client, storage, calls, operations, bindings


def upload(client, data, media_type="application/octet-stream", compression="none"):
    response = client.post(
        "/v1/scientific-artifacts/uploads",
        json={
            "model_id": "gromacs",
            "sha256": hashlib.sha256(data).hexdigest(),
            "size_bytes": len(data),
            "media_type": media_type,
            "compression": compression,
        },
    )
    assert response.status_code == 200, response.text
    reservation = response.json()
    aid = reservation["upload_id"]
    assert client.put(f"/v1/scientific-artifacts/uploads/{aid}/content", content=data).status_code == 200
    result = client.post(f"/v1/scientific-artifacts/uploads/{aid}:finalize")
    assert result.status_code == 200, result.text
    return result.json()["artifact"]


def submit_request(client, setup):
    data = setup[1].data["blobs/" + setup[0]["run"].input_bundle.sha256]
    bundle = upload(client, data, "application/gzip", "gzip")
    manifest = upload(
        client,
        canonical(
            {
                "schema": "fs2-serve.nebius.ai/scientific-artifact-manifest/v1",
                "manifest_id": "fixture",
                "entries": [
                    {"name": "gromacs-inputs", "semantic_type": "gromacs-input-bundle/v1", "artifact": bundle}
                ],
            }
        ),
    )
    return {
        "schema": "fs2-serve.nebius.ai/scientific-run-request/v1",
        "operation": "run-workflow",
        "input_manifest": manifest,
        "parameters": setup[0]["run"].parameters,
    }


def test_api_identity_upload_submit_replay_and_cross_user_isolation(gateway_fixture, setup):
    _, client, _, calls, operations, bindings = gateway_fixture
    assert client.get("/v1/me").json()["principal_id"] == "qa"
    body = submit_request(client, setup)
    first = client.post("/v1/models/gromacs:submit", json=body, headers={"Idempotency-Key": "cohort-001"})
    assert first.status_code == 202, first.text
    op = first.json()["operation_id"]
    assert client.get("/v1/operations/" + op).json()["status"] == "QUEUED"
    replay = client.post("/v1/models/gromacs:submit", json=body, headers={"Idempotency-Key": "cohort-001"})
    assert replay.status_code == 202 and replay.json()["operation_id"] == op
    assert len([path for _, path in calls if path.endswith(":invoke")]) == 1
    conflict = client.post(
        "/v1/models/gromacs:submit",
        json={**body, "client_context": {"changed": True}},
        headers={"Idempotency-Key": "cohort-001"},
    )
    assert conflict.status_code == 409
    assert client.get("/v1/operations/" + op, headers={"Authorization": "Bearer other"}).status_code == 404
    secret = json.loads(next(iter(bindings.values()))["run.json"])
    assert "secret_key" not in secret and "access_key" not in secret
    assert "storage_token" in secret


def test_job_grant_enforces_active_attempt_and_object_namespace(gateway_fixture, setup):
    gateway, client, _, _, operations, bindings = gateway_fixture
    body = submit_request(client, setup)
    response = client.post("/v1/models/gromacs:submit", json=body, headers={"Idempotency-Key": "cohort-002"})
    assert response.status_code == 202, response.text
    binding = json.loads(next(iter(bindings.values()))["run.json"])
    grant = {"Authorization": "Bearer " + binding["storage_token"]}
    operation = next(iter(operations.values()))
    operation.update(status="RUNNING", attempt_id="pod-1")
    request = {"action": "get", "key": "blobs/" + "a" * 64, "attempt_id": "pod-1"}
    assert client.post("/internal/storage", json=request, headers=grant).status_code == 200
    assert (
        client.post(
            "/internal/storage", json={**request, "key": "../another-user"}, headers=grant
        ).status_code
        == 403
    )
    assert (
        client.post("/internal/storage", json={**request, "attempt_id": "old-pod"}, headers=grant).status_code
        == 403
    )
    operation.update(status="CANCELLED")
    assert client.post("/internal/storage", json=request, headers=grant).status_code == 403
    claims = jwt.decode(
        binding["storage_token"],
        gateway.settings.signing_key.get_secret_value(),
        algorithms=["HS256"],
        audience="job-storage",
    )
    assert claims["owner"]["principal_id"] == "qa"


def test_upload_corruption_and_foreign_artifact_are_rejected(gateway_fixture):
    _, client, _, _, _, _ = gateway_fixture
    reservation = client.post(
        "/v1/scientific-artifacts/uploads",
        json={
            "sha256": hashlib.sha256(b"abc").hexdigest(),
            "size_bytes": 3,
        },
    ).json()
    url = f"/v1/scientific-artifacts/uploads/{reservation['upload_id']}/content"
    assert client.put(url, content=b"abcd").status_code == 413
    assert client.put(url, content=b"bad").status_code == 422
    assert client.put(url, content=b"abc", headers={"Authorization": "Bearer other"}).status_code == 404


def test_mcp_is_authenticated_and_exposes_native_schema(gateway_fixture):
    _, client, _, _, _, _ = gateway_fixture
    headers = {"Accept": "application/json, text/event-stream"}
    request = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
    assert (
        client.post("/mcp", json=request, headers={**headers, "Authorization": "Bearer invalid"}).status_code
        == 401
    )
    result = client.post("/mcp", json=request, headers=headers)
    assert result.status_code == 200, result.text
    tools = result.json()["result"]["tools"]
    tool = next(tool for tool in tools if tool["name"] == "submit_gromacs_workflow")
    schema = json.dumps(tool["inputSchema"])
    assert "mdrun" in schema and "checkpoint_minutes" in schema


def test_mcp_and_rest_share_submission_and_uuid_handle(gateway_fixture, setup):
    _, client, _, calls, _, _ = gateway_fixture
    body = submit_request(client, setup)
    headers = {"Accept": "application/json, text/event-stream"}
    request = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {
            "name": "submit_gromacs_workflow",
            "arguments": {"request": body, "idempotency_key": "mcp-cohort-01"},
        },
    }
    result = client.post("/mcp", json=request, headers=headers)
    assert result.status_code == 200, result.text
    message = result.json()["result"]
    assert not message.get("isError"), message
    value = message.get("structuredContent") or json.loads(message["content"][0]["text"])
    op = value["operation_id"]
    assert client.get("/v1/operations/" + op).json()["status"] == "QUEUED"
    replay = client.post("/v1/models/gromacs:submit", json=body, headers={"Idempotency-Key": "mcp-cohort-01"})
    assert replay.json()["operation_id"] == op
    assert len([path for _, path in calls if path.endswith(":invoke")]) == 1


def test_terminal_result_keeps_downloadable_legacy_artifact_manifest(gateway_fixture, setup):
    gateway, client, _, _, operations, _ = gateway_fixture
    body = submit_request(client, setup)
    submitted = client.post(
        "/v1/models/gromacs:submit", json=body, headers={"Idempotency-Key": "result-cohort-1"}
    ).json()
    op = submitted["operation_id"]
    owner = Owner(tenant_id="system", principal_id="qa")
    store = gateway.store(owner)
    data = b"synthetic-native-result"
    file = FileRecord(path="md.gro", sha256=hashlib.sha256(data).hexdigest(), size_bytes=len(data))
    store.objects(op).put("blobs/" + file.sha256, io.BytesIO(data))
    current = next(iter(operations.values()))
    profile = setup[0]["profile"]
    reference = store.artifacts(op).commit(
        Snapshot(
            kind="result",
            owner=owner,
            root_operation_id=current["id"],
            attempt_id="pod-1",
            epoch=1,
            runtime_revision=profile.revision,
            compatibility_id=profile.checkpoint_compatibility_id,
            image=profile.images["rtx-pro-6000"],
            job_id="replica",
            input_sha256="a" * 64,
            state={},
            files=(file,),
            result={"status": "succeeded", "files": [file.model_dump()]},
        )
    )
    current.update(status="SUCCEEDED", result_ref=reference)
    result = client.get(f"/v1/operations/{op}/result")
    assert result.status_code == 200, result.text
    assert result.json()["terminal_status"] == "succeeded"
    pointer = result.json()["output_manifest"]
    response = client.get(f"/v1/artifacts/{pointer['artifact_id']}/content")
    assert response.status_code == 200
    assert hashlib.sha256(response.content).hexdigest() == pointer["sha256"]
    entry = response.json()["entries"][0]
    downloaded = client.get(f"/v1/artifacts/{entry['artifact']['artifact_id']}/content")
    assert downloaded.content == data
    assert downloaded.headers["x-fs2-artifact-sha256"] == file.sha256
    from pathlib import Path

    from jsonschema import validate

    schema = Path(__file__).parents[3] / "catalog/runtime/schema/scientific-artifact-manifest.schema.json"
    validate(response.json(), json.loads(schema.read_text()))
