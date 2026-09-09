"""Offline verifier tests: no credentials, model calls or live endpoints."""

import asyncio
import copy
import json
from datetime import UTC, datetime
from uuid import UUID, uuid4

import httpx
import pytest
import verify

from fs2_serve.request_debug import DebugExchange, _summary, body_capture

KEY_ID = str(uuid4())
OWNER = {"tenant_id": "tenant-synthetic", "principal_id": "internal-test"}
SAVED = {"owner": OWNER, "key_id": KEY_ID}


def captured(tmp_path, *, source="public", operation=True):
    (tmp_path / "raw").mkdir(exist_ok=True)
    request = b"{}"
    response = b'{"detail":[{"loc":["body","sequences"],"msg":"Field required"}]}'
    case = {
        "name": "synthetic-validation",
        "model_id": "boltz2",
        "probe": "synthetic-probe",
        "endpoint": "/v1/models/boltz2:invoke",
        "started_at": datetime.now(UTC).isoformat(),
        "request": request,
        "response": response,
        "status": 422,
        "request_content_type": "application/json",
        "response_content_type": "application/json",
    }
    if operation:
        case["operation_id"] = str(uuid4())
    exchange = DebugExchange(
        id=uuid4(),
        source=source,
        request_id=uuid4() if source == "public" else None,
        operation_id=case.get("operation_id"),
        operation_attempt=1 if source == "upstream" else None,
        upstream_attempt=1 if source == "upstream" else None,
        started_at=datetime.now(UTC),
        completed_at=datetime.now(UTC),
        **OWNER,
        token_id=KEY_ID,
        model_id="boltz2",
        endpoint=case["endpoint"],
        method="POST",
        http_status=422,
        error_type="upstream_http_error" if source == "upstream" else None,
        request_headers=[
            ("authorization", "[REDACTED]"),
            ("x-api-key", "[REDACTED]"),
            (verify.PROBE_HEADER, case["probe"]),
        ],
        response_headers=[("content-type", "application/json")],
        request_body=body_capture(request, "application/json", True),
        response_body=body_capture(response, "application/json", True),
    )
    detail = exchange.model_dump(mode="json")
    summary = _summary(exchange).model_dump(mode="json")
    return case, detail, summary


def admin_mock(detail, summary):
    paths = []

    def handler(request):
        paths.append(str(request.url))
        if request.url.path.endswith("/" + detail["id"]):
            return httpx.Response(200, json={"data": detail})
        assert request.method == "GET"
        assert request.url.params["from"] and request.url.params["to"]
        return httpx.Response(200, json={"data": {"items": [summary], "next_cursor": None}})

    return httpx.Client(base_url="https://example.invalid", transport=httpx.MockTransport(handler)), paths


def test_default_is_offline_without_private_reads_or_client_creation(monkeypatch, capsys):
    def forbidden(*args, **kwargs):
        raise AssertionError("offline mode must not construct a client or read a key")

    monkeypatch.setattr(verify, "read_key", forbidden)
    monkeypatch.setattr(verify.httpx, "Client", forbidden)
    assert verify.main([]) == 0
    plan = json.loads(capsys.readouterr().out)
    assert plan["mode"] == "offline-preparation"
    assert plan["logical_native_operations"] == 3 and plan["client_replays"] == 0
    assert plan["phenoage_fixture_sha256"] == "b15ebdc61742dff7d3293e35a09b2a3207fd39d6e05ef7f881d900b4172b7842"


def test_generic_retained_key_is_not_coupled_to_old_customer(tmp_path):
    path = tmp_path / "key.json"
    value = {
        "schema": "fs2-customer-key/v1",
        **SAVED,
        "origin": "https://example.invalid",
        "secret": "fs2_pat_" + UUID(KEY_ID).hex + "_" + "synthetic-not-a-real-key" * 2,
    }
    # Credential files are inputs, never produced by the live verifier.
    path.write_text(json.dumps(value))
    path.chmod(0o600)
    assert verify.read_key(path) == value
    path.chmod(0o644)
    with pytest.raises(verify.CheckError, match="0600"):
        verify.read_key(path)


def test_selected_runtime_operation_comes_from_current_discovery_not_archive():
    discovery = {"data": [{"id": "openfold2", "operations": ["predict-structure"]}]}
    assert verify.advertised_operation(discovery, "openfold2") == "predict-structure"
    discovery["data"][0]["operations"] = ["predict", "predict-structure"]
    with pytest.raises(verify.CheckError, match="native_operation_ambiguous"):
        verify.advertised_operation(discovery, "openfold2")


def test_explicit_remaining_cases_exclude_previously_accepted_models(capsys):
    assert verify.main(["--cases", "openfold2", "malformed-http", "mcp"]) == 0
    plan = json.loads(capsys.readouterr().out)
    assert plan["selected_cases"] == ["openfold2", "malformed-http", "mcp"]
    assert plan["logical_native_operations"] == 1


@pytest.mark.parametrize("raw", [b'{"a": 1}\n', b"\xff\x00\x01"])
def test_exact_body_preserves_utf8_or_base64(raw):
    body = body_capture(raw, None, True).model_dump()
    verify.exact_body(body, raw)
    assert verify.body_bytes(body) == raw


@pytest.mark.parametrize("field,value", [("complete", False), ("redacted", True), ("observed_bytes", 100)])
def test_partial_redacted_or_incorrect_count_is_not_exact(field, value):
    body = body_capture(b"{}", "application/json", True).model_dump()
    body[field] = value
    with pytest.raises(verify.CheckError):
        verify.exact_body(body, b"{}")


@pytest.mark.parametrize("operation", [False, True])
def test_public_failed_and_operationless_capture_is_exact_lazy_and_correlated(tmp_path, operation):
    case, detail, summary = captured(tmp_path, operation=operation)
    client, paths = admin_mock(detail, summary)
    recorder = verify.Recorder(tmp_path, ())
    with client:
        result = verify.verify_public_capture(client, recorder, case, SAVED, "app-synthetic", 1)
    assert result["http_status"] == 422 and result["complete_exact_body_capture"]
    assert result["response_sha256"] == verify.sha(case["response"])
    assert "request_body" not in result
    assert len(paths) == 3 and "/admin/api/v1/requests/" in paths[-1]
    assert ("operation_id=" in paths[0]) is operation
    assert json.loads((tmp_path / "raw/synthetic-validation-public.json").read_text()) == detail


def test_mismatched_original_capture_is_preserved_without_becoming_a_pass(tmp_path):
    case, detail, summary = captured(tmp_path)
    detail["response_body"]["data"] = "not the same validation response"
    client, _ = admin_mock(detail, summary)
    with client, pytest.raises(verify.CheckError, match="captured_body_bytes_differ"):
        verify.verify_public_capture(client, verify.Recorder(tmp_path, ()), case, SAVED, "app", 1)
    assert (tmp_path / "raw/synthetic-validation-public.json").exists()


def test_leaked_credential_is_not_persisted_even_privately(tmp_path):
    case, detail, summary = captured(tmp_path)
    detail["request_headers"][0][1] = "synthetic-private-secret"
    client, _ = admin_mock(detail, summary)
    with client, pytest.raises(verify.CheckError, match="capture_contains_credential"):
        verify.verify_public_capture(
            client, verify.Recorder(tmp_path, ("synthetic-private-secret",)), case, SAVED, "app", 1
        )
    assert not list((tmp_path / "raw").iterdir())


def test_payload_in_summary_is_a_failure(tmp_path):
    _, detail, summary = captured(tmp_path)
    summary["request_body"] = detail["request_body"]
    client, _ = admin_mock(detail, summary)
    with client, pytest.raises(verify.CheckError, match="summary_contains_payload"):
        verify.capture_list(client, verify.Recorder(tmp_path, ()), "/admin/api/v1/requests", verify.shared.now())


def test_actual_upstream_422_body_and_attempt_identity(tmp_path):
    case, detail, summary = captured(tmp_path, source="upstream")
    client, _ = admin_mock(detail, summary)
    with client:
        result = verify.verify_upstream(client, verify.Recorder(tmp_path, ()), case, SAVED, "app", {}, 1, {422})
    assert result[0]["http_status"] == 422
    assert result[0]["operation_attempt"] == result[0]["upstream_attempt"] == 1
    assert result[0]["response_sha256"] == verify.sha(case["response"])


@pytest.mark.parametrize("kind", ["wrong-owner", "wrong-model", "unavailable", "missing-attempt"])
def test_upstream_unknown_or_misattributed_data_never_passes(tmp_path, kind):
    case, detail, summary = captured(tmp_path, source="upstream")
    if kind == "wrong-owner":
        detail["tenant_id"] = "different-customer"
    elif kind == "wrong-model":
        detail["model_id"] = "other-model"
    elif kind == "unavailable":
        detail["http_status"] = 503
    else:
        detail["upstream_attempt"] = None
    client, _ = admin_mock(detail, summary)
    with client, pytest.raises(verify.CheckError):
        verify.verify_upstream(client, verify.Recorder(tmp_path, ()), case, SAVED, "app", {}, 1, {400, 422})


def test_mcp_json_and_sse_error_is_not_http_success():
    assert verify.rpc_failed(b'{"jsonrpc":"2.0","id":1,"error":{"code":-32602}}')
    assert verify.rpc_failed(b'data: {"jsonrpc":"2.0","id":1,"result":{"isError":true}}\n\n')
    assert not verify.rpc_failed(b'{"jsonrpc":"2.0","id":1,"result":{"content":[]}}')


def test_mcp_transport_retains_only_consumed_tool_exchange_bytes():
    async def run():
        original = b'{"jsonrpc":"2.0","id":3,"result":{"isError":true}}'

        class ResponseStream(verify.shared.httpx2.AsyncByteStream):
            async def __aiter__(self):
                yield original[:10]
                yield original[10:]

        async def handler(request):
            assert request.headers[verify.PROBE_HEADER]
            return verify.shared.httpx2.Response(
                200, stream=ResponseStream(), headers={"content-type": "application/json"}
            )

        transport = verify.MCPTransport()
        await transport.inner.aclose()
        transport.inner = verify.shared.httpx2.MockTransport(handler)
        async with verify.shared.httpx2.AsyncClient(transport=transport) as client:
            response = await client.post(
                "https://example.invalid/mcp",
                json={
                    "jsonrpc": "2.0",
                    "id": 3,
                    "method": "tools/call",
                    "params": {"name": "invoke_model", "arguments": {"model_id": "phenoage", "payload": "bad"}},
                },
            )
            assert response.content == original
        assert len(transport.cases) == 1
        case = transport.cases[0]
        assert case["response"] == original and verify.rpc_failed(case["response"])
        assert case["model_id"] == "phenoage" and "operation_id" not in case
        assert case["request_content_type"] == case["response_content_type"] == "application/json"

    asyncio.run(run())


def test_output_is_exclusive_and_contains_no_payload_or_secret_on_stdout(tmp_path, capsys):
    path = tmp_path / "summary.json"
    value = {"outcome": "failed", "error_code": "capture_persistence_timeout"}
    verify.write(path, value)
    with pytest.raises(FileExistsError):
        verify.write(path, value)
    with pytest.raises(verify.CheckError):
        verify.write(tmp_path / "unsafe.json", {"secret": "synthetic-private-secret"}, ("synthetic-private-secret",))
    assert not (tmp_path / "unsafe.json").exists()
    assert not capsys.readouterr().out


def test_bounded_empty_capture_is_missing_not_zero_success(tmp_path, monkeypatch):
    times = iter([0, 0, 2])
    monkeypatch.setattr(verify.time, "monotonic", lambda: next(times))
    monkeypatch.setattr(verify.time, "sleep", lambda _: None)
    monkeypatch.setattr(verify, "capture_list", lambda *_: [])
    with pytest.raises(verify.CheckError, match="public_capture_persistence_timeout"):
        verify.verify_public_capture(None, verify.Recorder(tmp_path, ()), {"started_at": "unused"}, SAVED, "app", 1)


def test_owner_match_requires_token_and_both_identity_fields():
    identity = {**OWNER, "token_id": KEY_ID}
    assert verify.owner_matches(identity, SAVED)
    for key in identity:
        wrong = copy.copy(identity)
        wrong[key] = None
        assert not verify.owner_matches(wrong, SAVED)
