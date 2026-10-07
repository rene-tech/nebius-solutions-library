"""A rolling API release must not turn a retained GPU job into a duplicate."""
import importlib.util
import json
from pathlib import Path

import httpx
import pytest

PATH = Path(__file__).resolve().parents[3] / "models/visual-science/scvi-scanvi/qualify_hosted.py"
SPEC = importlib.util.spec_from_file_location("scvi_hosted_retry", PATH)
hosted = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(hosted)


@pytest.mark.parametrize("failure", [502, 503, 504, "disconnect"])
def test_retries_identical_read_or_idempotent_mcp_body(monkeypatch, failure):
    calls, waits = [], []

    def handle(request):
        calls.append((request.method, str(request.url), request.content, request.headers["authorization"]))
        if len(calls) == 1:
            if failure == "disconnect":
                raise httpx.ReadError("connection reset", request=request)
            return httpx.Response(failure)
        return httpx.Response(200, json={"operation_id": "same-running-job"})

    monkeypatch.setattr(hosted.time, "sleep", waits.append)
    with httpx.Client(base_url="https://api.example", transport=httpx.MockTransport(handle)) as client:
        response = hosted.request_with_retry(client, "POST", "/mcp", retryable=True,
            headers={"Authorization": "Bearer synthetic"},
            json={"jsonrpc": "2.0", "id": 7, "method": "tools/call", "params": {
                "name": "get_scientific_status", "arguments": {"operation_id": "same-running-job"}}})
    assert response.json()["operation_id"] == "same-running-job"
    assert calls[0] == calls[1] and waits == [1]
    assert json.loads(calls[1][2])["params"]["name"] == "get_scientific_status"


@pytest.mark.parametrize("method,status,expected", [("GET", 503, 6), ("POST", 503, 1),
                                                   ("GET", 400, 1), ("GET", 401, 1), ("GET", 403, 1)])
def test_retry_is_bounded_and_does_not_hide_invalid_or_unsafe_requests(monkeypatch, method, status, expected):
    calls, waits = [], []

    def handle(request):
        calls.append(request)
        return httpx.Response(status)

    monkeypatch.setattr(hosted.time, "sleep", waits.append)
    with httpx.Client(base_url="https://api.example", transport=httpx.MockTransport(handle)) as client:
        response = hosted.request_with_retry(client, method, "/operation")
    assert response.status_code == status and len(calls) == expected
    assert len(waits) == expected - 1


def test_persistent_connection_failure_is_not_reported_as_success(monkeypatch):
    waits = []

    def handle(request):
        raise httpx.ConnectError("no healthy upstream", request=request)

    monkeypatch.setattr(hosted.time, "sleep", waits.append)
    with httpx.Client(base_url="https://api.example", transport=httpx.MockTransport(handle)) as client:
        with pytest.raises(httpx.ConnectError):
            hosted.request_with_retry(client, "GET", "/operation")
    assert waits == [1, 2, 4, 8, 10]
