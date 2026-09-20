import hashlib
import importlib.util
import json
import sys
from uuid import uuid4

import httpx
import pytest
from conftest import SOLUTION_ROOT

HERE = SOLUTION_ROOT / "acceptance/performance-placement-20260920"
sys.path.insert(0, str(HERE))
spec = importlib.util.spec_from_file_location("benchmark_runner_test", HERE / "runner.py")
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)


@pytest.mark.parametrize("corrupt", [False, True])
def test_native_result_materializes_and_hash_checks_large_artifacts(tmp_path, corrupt):
    operation_id, artifact_id = str(uuid4()), str(uuid4())
    raw = b'{"native_result":true}'

    def transport(request):
        if request.method == "POST":
            return httpx.Response(202, json={"id": operation_id})
        if request.url.path.endswith("/result"):
            return httpx.Response(
                200,
                json={
                    "schema": "fs2-serve.nebius.ai/operation-artifact-result/v1",
                    "artifact": {
                        "artifact_id": artifact_id,
                        "size_bytes": len(raw),
                        "sha256": "0" * 64 if corrupt else hashlib.sha256(raw).hexdigest(),
                    },
                },
            )
        if request.url.path.endswith("/content"):
            return httpx.Response(200, content=raw)
        return httpx.Response(200, json={"id": operation_id, "status": "succeeded"})

    with httpx.Client(base_url="https://test", transport=httpx.MockTransport(transport)) as client:
        if corrupt:
            with pytest.raises(RuntimeError, match="result_artifact_identity_mismatch"):
                runner.invoke(client, "diffdock", "native", "dock", {}, "stable-key", tmp_path, 60)
        else:
            path, status, elapsed = runner.invoke(client, "diffdock", "native", "dock", {}, "stable-key", tmp_path, 60)
            assert path.read_bytes() == raw and status["id"] == operation_id and elapsed >= 0


def test_finalized_input_is_reused_without_rewriting_bytes():
    data = b"public-fixture"
    artifact = {
        "artifact_id": str(uuid4()),
        "sha256": hashlib.sha256(data).hexdigest(),
        "size_bytes": len(data),
        "media_type": "application/json",
        "compression": "none",
    }
    operation_id, upload_id = str(uuid4()), str(uuid4())
    calls = []

    class Client:
        def request(self, method, path, **kwargs):
            calls.append((method, path))
            if path == "/v1/scientific-artifacts/uploads":
                value, status = {"operation_id": operation_id, "upload_id": upload_id}, 201
            elif method == "GET":
                value, status = {"status": "succeeded"}, 200
            else:
                assert path.endswith(":finalize")
                value, status = artifact, 200
            return runner.PUBLIC.HttpResponse(status, {}, json.dumps(value).encode())

    returned = runner.PUBLIC._upload(
        Client(),
        model_id="alphafold3",
        data=data,
        media_type="application/json",
        compression="none",
        idempotency_key="existing-key",
    )
    assert returned == artifact
    assert not any(method == "PUT" for method, _ in calls)


def test_scientific_queue_wait_retries_only_explicit_rejection(monkeypatch):
    calls = []
    responses = [runner.PUBLIC.HttpResponse(code, {}, b"{}") for code in (429, 503)]

    def request(self, *args, **kwargs):
        calls.append((args, kwargs))
        return responses.pop(0)

    monkeypatch.setattr(runner.PUBLIC.PublicApiClient, "request", request)
    monkeypatch.setattr(runner.time, "sleep", lambda _: None)
    result = runner.QueuedPublicClient("https://test", "private", 60).request("POST", "/submit", headers={"Idempotency-Key": "same"})
    assert result.status == 503 and len(calls) == 2 and calls[0] == calls[1]


@pytest.mark.parametrize("code,blocked", [("http_403", True), ("http_401", True), ("operation_failed", False)])
def test_access_denial_blocks_only_the_same_campaign_case(code, blocked):
    trial = {"id": "new", "case_id": "private-model"}
    previous = [{"id": "first", "case_id": "private-model", "result": {"error_code": code}}]
    def transport(request):
        assert request.method == "GET" and request.url.path.endswith("/campaigns/campaign")
        return httpx.Response(200, json={"data": {"trials": previous}})
    with httpx.Client(base_url="https://test", transport=httpx.MockTransport(transport)) as client:
        assert runner.prior_access_denial(client, "campaign", trial) == ("first" if blocked else None)
        assert runner.prior_access_denial(client, "campaign", {**trial, "case_id": "other-model"}) is None
