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


@pytest.mark.parametrize('structured', [True, False])
def test_mcp_explicit_non_admission_waits_without_changing_request(monkeypatch, structured):
    calls, waits = [], []
    body = {'parameters': {'method': 'scvi'}, 'idempotency_key': 'same-logical-job'}
    rejected = {'error': {'code': 'admission_limit_reached', 'durable_admission': False,
                          'retryable': True, 'retry_after_seconds': 2}}

    def submit():
        calls.append(json.dumps(body, sort_keys=True))
        if len(calls) == 1:
            envelope = {'isError': True, 'content': [{'type': 'text', 'text': json.dumps(rejected)}]}
            if structured:
                envelope['structuredContent'] = rejected
            return hosted.tool_result(envelope, admission=True)
        return {'operation': {'id': 'only-one-job'}}

    monkeypatch.setattr(hosted.time, 'sleep', waits.append)
    result = hosted.submit_with_capacity_wait(submit, 60)
    assert result['operation']['id'] == 'only-one-job'
    assert len(calls) == 2 and calls[0] == calls[1] and waits == [2]


@pytest.mark.parametrize('error', [
    {'code': 'model_input_invalid'},
    {'code': 'admission_limit_reached', 'durable_admission': True},
    {'code': 'admission_limit_reached', 'retryable': False},
    {'code': 'rate_limited'},
])
def test_only_explicit_unadmitted_capacity_can_wait(error):
    envelope = {'isError': True, 'structuredContent': {'error': error}}
    with pytest.raises(ValueError, match='MCP tool failed'):
        hosted.tool_result(envelope, admission=True)


def test_read_tool_error_is_not_treated_as_submission_capacity():
    with pytest.raises(ValueError, match='MCP tool failed'):
        hosted.tool_result({'isError': True, 'structuredContent': {
            'error': {'code': 'admission_limit_reached', 'durable_admission': False}}})


def test_capacity_wait_deadline_preserves_actionable_non_admission():
    calls = []

    def submit():
        calls.append('attempt')
        raise hosted.AdmissionCapacityError({'retry_after_seconds': 2})

    with pytest.raises(RuntimeError, match='Retain uploads'):
        hosted.submit_with_capacity_wait(submit, 0)
    assert calls == ['attempt']
