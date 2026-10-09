"""The customer CLI reuses the real, streamed qualification transport."""

import importlib.util
import json
import sys
from pathlib import Path

import httpx
import pytest

PATH = Path(__file__).resolve().parents[3] / "models/visual-science/scvi-scanvi/qualify_api.py"
SPEC = importlib.util.spec_from_file_location("scvi_customer_client", PATH)
client_module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(client_module)


def test_customer_training_and_query_inputs_are_typed_and_key_stays_at_api(tmp_path, monkeypatch):
    data, reference = tmp_path / "counts.h5ad", tmp_path / "reference.tar.gz"
    data.write_bytes(b"synthetic H5AD transport fixture")
    reference.write_bytes(b"synthetic reference transport fixture")
    parameters = tmp_path / "parameters.json"
    parameters.write_text(json.dumps({"schema": "fs2-serve.nebius.ai/scvi-workflow-request/v1", "mode": "map-query"}))
    output = tmp_path / "receipt"
    reservations, transferred, requests = {}, {}, []

    def handle(request):
        if request.url.host == "objects.example":
            assert "authorization" not in request.headers
            transferred[request.url.path[1:]] = request.content
            return httpx.Response(200)
        assert request.headers["authorization"] == "Bearer synthetic-customer-key"
        if request.url.path == "/v1/me":
            return httpx.Response(200, json={"tenant_id": "synthetic-poc", "principal_id": "researcher"})
        if request.url.path.startswith("/v1/operations/"):
            return httpx.Response(200, json={"status": "queued"})
        if request.url.path == "/v1/scientific-artifacts/uploads":
            value = json.loads(request.content)
            identity = str(len(reservations) + 1)
            reservations[identity] = value
            return httpx.Response(
                200,
                json={
                    "upload_id": identity,
                    "operation_id": identity,
                    "handle": {"headers": {}, "url": "https://objects.example/" + identity},
                },
            )
        if request.url.path.endswith(":finalize"):
            identity = request.url.path.rsplit("/", 1)[1].removesuffix(":finalize")
            value = reservations[identity]
            return httpx.Response(
                200,
                json={
                    "artifact_id": identity,
                    **{k: value[k] for k in ("sha256", "size_bytes", "media_type", "compression")},
                },
            )
        if request.url.path == "/v1/models/scvi-scanvi:submit":
            requests.append(json.loads(request.content))
            assert request.headers["idempotency-key"] == "customer-chosen-run"
            return httpx.Response(202, json={"operation": {"id": "synthetic-operation"}})
        raise AssertionError(str(request.url))

    real_client = httpx.Client
    monkeypatch.setattr(
        client_module.httpx, "Client", lambda **kwargs: real_client(**kwargs, transport=httpx.MockTransport(handle))
    )
    monkeypatch.setenv("SCIENTIFIC_MODELS_API_KEY", "synthetic-customer-key")
    monkeypatch.setattr(
        sys,
        "argv",
        [
            str(PATH),
            "--input",
            str(data),
            "--reference",
            str(reference),
            "--parameters",
            str(parameters),
            "--output",
            str(output),
            "--idempotency-key",
            "customer-chosen-run",
        ],
    )
    client_module.main(require_qa=False)
    assert reservations["2"]["media_type"] == "application/x-tar"
    assert reservations["2"]["compression"] == "gzip"
    manifest = json.loads(transferred["3"])
    assert [entry["name"] for entry in manifest["entries"]] == ["anndata", "reference"]
    assert requests[0]["input_manifest"]["artifact_id"] == "3"
    assert json.loads((output / "admission.json").read_text())["operation"]["id"] == "synthetic-operation"
    assert not any("synthetic-customer-key" in path.read_text() for path in output.glob("*.json"))


def test_qualification_entry_point_still_refuses_customer_identity(tmp_path, monkeypatch):
    private = tmp_path / "qa.env"
    private.write_text("SCIENTIFIC_MODELS_API_KEY=synthetic-key\n")
    real_client = httpx.Client
    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, json={"tenant_id": "customer", "principal_id": "user"})
    )
    monkeypatch.setattr(client_module.httpx, "Client", lambda **kwargs: real_client(**kwargs, transport=transport))
    monkeypatch.setattr(sys, "argv", [str(PATH), "--qa-env", str(private), "--output", str(tmp_path / "out")])
    with pytest.raises(ValueError, match="never a customer key"):
        client_module.main()


def test_explicit_non_admission_retries_identical_request(monkeypatch):
    calls, sleeps = [], []

    def handle(request):
        calls.append((request.content, request.headers["idempotency-key"]))
        if len(calls) == 1:
            return httpx.Response(429, json={"error": {"type": "concurrency_exceeded"}})
        return httpx.Response(202, json={"operation": {"id": "synthetic"}})

    monkeypatch.setattr(client_module.time, "sleep", sleeps.append)
    with httpx.Client(base_url="https://api.example", transport=httpx.MockTransport(handle)) as client:
        result = client_module.post_with_admission_wait(
            client, "/submit", wait_seconds=60, json={"same": "request"}, headers={"Idempotency-Key": "same-key"}
        )
    assert calls[0] == calls[1]
    assert len(calls) == 2 and sleeps == [5]
    assert result["operation"]["id"] == "synthetic"


@pytest.mark.parametrize("status", [400, 401, 403, 500])
def test_admission_wait_does_not_hide_other_errors(monkeypatch, status):
    calls = []

    def handle(request):
        calls.append(request)
        return httpx.Response(status, json={"error": {"type": "some-other-error"}})

    with httpx.Client(base_url="https://api.example", transport=httpx.MockTransport(handle)) as client:
        with pytest.raises(RuntimeError, match="HTTP"):
            client_module.post_with_admission_wait(client, "/submit", wait_seconds=60)
    assert len(calls) == 1


def test_admission_wait_has_an_actionable_deadline():
    transport = httpx.MockTransport(
        lambda request: httpx.Response(429, json={"error": {"type": "concurrency_exceeded"}})
    )
    with httpx.Client(base_url="https://api.example", transport=transport) as client:
        with pytest.raises(RuntimeError, match="Uploaded files are retained"):
            client_module.post_with_admission_wait(client, "/submit", wait_seconds=0)


@pytest.mark.parametrize("completed_marker,previous_receipt", [(True, True), (False, True), (False, False)])
def test_lost_finalization_response_does_not_retransmit_input(
    tmp_path, monkeypatch, completed_marker, previous_receipt
):
    import hashlib

    data = tmp_path / "data.h5ad"
    data.write_bytes(b"retained immutable upload")
    digest = hashlib.sha256(data.read_bytes()).hexdigest()
    output = tmp_path / "receipts"
    output.mkdir()
    if previous_receipt:
        (output / "anndata-upload.json").write_text(json.dumps({"operation_id": "upload-op", "upload_id": "upload"}))
    if completed_marker:
        (output / "anndata-transfer-completed.json").write_text("{}")
    calls = []

    def handle(request):
        calls.append(request.url.path)
        if request.url.path == "/v1/me":
            return httpx.Response(200, json={"tenant_id": "synthetic", "principal_id": "researcher"})
        if request.url.path == "/v1/scientific-artifacts/uploads":
            return httpx.Response(200, json={"operation_id": "upload-op", "upload_id": "upload", "handle": {}})
        if request.url.path == "/v1/operations/upload-op":
            return httpx.Response(200, json={"status": "queued" if completed_marker else "succeeded"})
        if request.url.path == "/v1/scientific-artifacts/uploads/upload:finalize":
            return httpx.Response(
                200, json={"artifact_id": "artifact", "sha256": digest, "size_bytes": data.stat().st_size}
            )
        raise AssertionError("Completed bytes must not be retransmitted")

    real_client = httpx.Client
    monkeypatch.setattr(
        client_module.httpx, "Client", lambda **kw: real_client(**kw, transport=httpx.MockTransport(handle))
    )
    monkeypatch.setenv("SCIENTIFIC_MODELS_API_KEY", "synthetic-key")
    monkeypatch.setattr(sys, "argv", [str(PATH), "--input", str(data), "--output", str(output), "--stage-only"])
    client_module.main(require_qa=False)
    assert calls == (
        ["/v1/me"]
        + ([] if previous_receipt else ["/v1/scientific-artifacts/uploads"])
        + ["/v1/operations/upload-op", "/v1/scientific-artifacts/uploads/upload:finalize"]
    )
    assert json.loads((output / "anndata-artifact.json").read_text())["sha256"] == digest
