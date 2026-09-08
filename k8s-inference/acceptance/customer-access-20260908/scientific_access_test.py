"""Offline regression tests; never open a live endpoint or retained credentials."""

import argparse
import json
from types import SimpleNamespace

import pytest
import scientific_access as acceptance


def response(status, value):
    return acceptance.public.HttpResponse(status, {"content-type": "application/json"}, json.dumps(value).encode())


def test_preparation_preserves_both_qualified_requests_and_reads_no_credentials(monkeypatch, capsys):
    def forbidden(*args, **kwargs):
        raise AssertionError("offline preparation attempted private access or traffic")

    monkeypatch.setattr(acceptance.customer, "read_key", forbidden)
    monkeypatch.setattr(acceptance.BASE_CLIENT, "request", forbidden)
    monkeypatch.setattr(acceptance.httpx.Client, "request", forbidden)
    assert acceptance.main(["--key-file", "/not-read", "--access-bundle", "/not-read"]) == 0
    plan = json.loads(capsys.readouterr().out)
    assert plan["operation_count"] == 2 and plan["parallel_clients"] == 1
    assert plan["fixture_changes"] is False
    assert {row["model_id"]: row["request_sha256"] for row in plan["models"]} == {
        "bindcraft": "f2ed7f1380f0ef8e60432afe13b33d871e7a30d93b81ffc5bcd9a59e1f1d8dda",
        "alphafold3": "60c8c9a81f1b99c32999335640d1b6df28a2d18dfa6b955db9d1dcb25bf04cdc",
    }
    assert all(row["service_class"] == "customer-batch" and len(row["inputs"]) == 2 for row in plan["models"])


def test_execute_requires_exact_release_before_reading_key(tmp_path, monkeypatch):
    monkeypatch.setattr(acceptance.customer, "read_key", lambda *_: pytest.fail("private file read"))
    args = argparse.Namespace(deployed_source="short", output=tmp_path / "new")
    with pytest.raises(acceptance.public.AcceptanceError, match="exact_deployed_source_required"):
        acceptance.execute(args, {})
    assert not args.output.exists()


@pytest.mark.parametrize("denial_status", [403, 404])
def test_isolation_requires_same_model_discovery_and_only_reads(denial_status):
    calls = []

    def request(method, path):
        calls.append((method, path))
        return (
            response(200, {"data": [{"model_id": item} for item in acceptance.MODELS]})
            if len(calls) == 1
            else response(denial_status, {})
        )

    row = {
        "operation_id": "owned-operation",
        "downloaded_artifacts": [
            {"artifact_id": "manifest"},
            {"artifact_id": "output"},
        ],
    }
    checks = acceptance.isolation(SimpleNamespace(request=request), row)
    assert len(checks) == 4 and {item["status"] for item in checks} == {denial_status}
    assert calls[0] == ("GET", "/v1/scientific-models")
    assert all(method == "GET" for method, _ in calls)


@pytest.mark.parametrize("status", [200, 401, 429, 500])
def test_isolation_does_not_count_authentication_or_availability_failure(status):
    def request(method, path):
        return (
            response(200, {"data": [{"model_id": item} for item in acceptance.MODELS]})
            if path == "/v1/scientific-models"
            else response(status, {})
        )

    with pytest.raises(acceptance.public.AcceptanceError, match="cross_customer_data_not_denied"):
        acceptance.isolation(
            SimpleNamespace(request=request),
            {"operation_id": "owned", "downloaded_artifacts": []},
        )


def test_isolation_does_not_probe_data_when_same_model_is_not_discoverable():
    calls = []

    def request(method, path):
        calls.append(path)
        return response(200, {"data": []})

    with pytest.raises(acceptance.public.AcceptanceError, match="academic_models_not_discoverable"):
        acceptance.isolation(
            SimpleNamespace(request=request),
            {"operation_id": "owned", "downloaded_artifacts": []},
        )
    assert calls == ["/v1/scientific-models"]


@pytest.mark.parametrize(
    "field,value",
    [
        ("tenant_id", "other"),
        ("principal_id", "other"),
        ("api_key_prefix", "other"),
        ("model_id", "other"),
        ("id", "other"),
        ("status", "failed"),
    ],
)
def test_owner_attribution_rejects_mismatches(field, value):
    operation = {
        "id": "owned",
        "model_id": "bindcraft",
        **acceptance.customer.OWNER,
        "api_key_prefix": "prefix",
        "status": "succeeded",
        field: value,
    }
    with pytest.raises(acceptance.public.AcceptanceError):
        acceptance.assert_owner(
            {"operation": operation},
            "bindcraft",
            "owned",
            {"id": "access", "prefix": "prefix"},
        )


@pytest.mark.parametrize("fails", [False, True])
def test_request_trace_records_failure_once_without_auth_or_payload(tmp_path, monkeypatch, fails):
    calls = []

    def request(self, method, path, **kwargs):
        calls.append(path)
        if fails:
            raise RuntimeError("sensitive exception detail must not escape")
        return response(500, {"unexpected": "response payload not retained"})

    monkeypatch.setattr(acceptance.BASE_CLIENT, "request", request)
    path = tmp_path / "trace.jsonl"
    client = acceptance.trace_client(path)("https://example.invalid", "synthetic-private-value")
    if fails:
        with pytest.raises(RuntimeError):
            client.request("GET", "/v1/operations/owned")
    else:
        assert client.request("GET", "/v1/operations/owned").status == 500
    text = path.read_text()
    assert len(calls) == 1 and len(text.splitlines()) == 1
    assert (
        "synthetic-private-value" not in text and "sensitive exception" not in text and "response payload" not in text
    )
    row = json.loads(text)
    assert row["duration_seconds"] >= 0
    assert row["error_type"] == "RuntimeError" if fails else row["status"] == 500


@pytest.mark.parametrize("case_failure", [False, True])
def test_full_or_failed_run_keeps_attribution_counts_and_closes_session(tmp_path, monkeypatch, capsys, case_failure):
    origin = "https://example.invalid"
    owner_secret, other_secret = "synthetic-owner-only", "synthetic-other-only"
    access = tmp_path / "access.json"
    access.write_text(
        json.dumps(
            {
                "endpoints": {"inference_base_url": origin + "/v1"},
                "credentials": {
                    "inference_access_token": other_secret,
                    "admin_bootstrap_token": "synthetic-admin-only",
                },
            }
        )
    )
    metadata = {
        "id": "access-id",
        "prefix": "prefix",
        "scopes": acceptance.customer.SCOPES,
    }
    monkeypatch.setattr(
        acceptance.customer,
        "read_key",
        lambda _: {"origin": origin, "secret": owner_secret, "key_id": metadata["id"]},
    )
    monkeypatch.setattr(acceptance.customer, "validate_retained_metadata", lambda row: None)
    exchanges, runs = [], []

    def exchange(admin, trace, method, path, **kwargs):
        exchanges.append((method, path))
        return {}, {}

    monkeypatch.setattr(acceptance.customer.shared, "exchange", exchange)

    def admin_call(admin, trace, method, path, **kwargs):
        assert method == "GET"
        if path.endswith("/keys"):
            return {"items": [metadata]}
        if path.startswith("/admin/api/v1/apps?"):
            return {"items": [{"public_model_id": model, "app_id": model} for model in acceptance.MODELS]}
        if "/runs/" in path:
            model = path.split("/")[-3]
            return {
                "operation": {
                    "id": model + "-operation",
                    "model_id": model,
                    **acceptance.customer.OWNER,
                    "api_key_prefix": "prefix",
                    "status": "succeeded",
                }
            }
        return {
            "user": {
                **acceptance.customer.OWNER,
                "usage": {
                    "requests": 2,
                    "scientific_requests": 2,
                    "succeeded": 2,
                    "failed": 0,
                    "cancelled": 0,
                    "pending": 0,
                    "running": 0,
                    "input_tokens": {"value": None},
                    "output_tokens": {"value": None},
                },
            }
        }

    monkeypatch.setattr(acceptance.customer.shared, "admin_call", admin_call)

    def request(self, method, path, **kwargs):
        assert method == "GET"
        if path == "/v1/scientific-models":
            return response(200, {"data": [{"model_id": model} for model in acceptance.MODELS]})
        if self._token == other_secret:
            return response(404, {})
        assert self._token == owner_secret
        return response(200, {"synthetic_result": True})

    monkeypatch.setattr(acceptance.BASE_CLIENT, "request", request)

    async def mcp_call(origin, secret, trace, name, arguments):
        assert secret == owner_secret and name == "get_scientific_result"
        return {"synthetic_result": True}

    monkeypatch.setattr(acceptance.customer.shared, "mcp_call", mcp_call)

    def scenario(config, definition, secret):
        assert secret == owner_secret and set(definition) == {"id", "model_id"}
        model = definition["model_id"]
        runs.append(model)
        if case_failure:
            return {
                "model_id": model,
                "operation_id": model + "-operation",
                "outcome": "failed",
                "error_code": "original_case_failure",
            }
        acceptance.write(
            config.receipt_path,
            {"queue": {"observed_stages": [{"attempts": [{"resource_released": True}]}]}},
        )
        return {
            "model_id": model,
            "operation_id": model + "-operation",
            "outcome": "passed",
            "downloaded_artifacts": [
                {"artifact_id": "manifest"},
                {"artifact_id": "output"},
            ],
        }

    monkeypatch.setattr(acceptance.scenarios, "run_scenario", scenario)
    args = argparse.Namespace(
        endpoint=origin,
        run_id="test-r01",
        deployed_source="a" * 40,
        key_file=tmp_path / "not-read",
        access_bundle=access,
        output=tmp_path / "fresh",
        timeout_seconds=1800,
    )
    plan = {"models": [{"model_id": model, "fixture": "unused"} for model in acceptance.MODELS]}
    assert acceptance.execute(args, plan) == int(case_failure)
    result = json.loads((args.output / "outcome.json").read_text())
    assert result["admin_logged_out"] and result["retained_access_unchanged"]
    assert exchanges == [
        ("POST", "/admin/api/v1/session"),
        ("DELETE", "/admin/api/v1/session"),
    ]
    assert runs == (["bindcraft"] if case_failure else list(acceptance.MODELS))
    assert result["outcome"] == ("failed" if case_failure else "passed")
    if not case_failure:
        assert result["owner_usage"]["scientific_requests"] == 2
        assert all(row["mcp_result_parity"] and len(row["other_customer_denials"]) == 4 for row in result["rows"])
    assert all(secret not in capsys.readouterr().out for secret in (owner_secret, other_secret, "synthetic-admin-only"))
    assert acceptance.public.PublicApiClient is acceptance.BASE_CLIENT
