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
