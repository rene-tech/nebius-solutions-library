"""Offline checks for the four-operation public acceptance harness."""

import copy
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import httpx
import public_apps as public
import pytest


def settings():
    return {
        "app_revision": 1,
        "capabilities": {"live_settings": True},
        "serving": {
            "tenant_id": "tenant",
            "etag": "sha256:original",
            "spec": {
                "modelRef": "phenoage",
                "lifecycle": {"desiredState": "Enabled"},
                "availability": {
                    "minReplicas": 1,
                    "maxReplicas": 2,
                    "idleSeconds": 300,
                    "cooldownSeconds": 300,
                    "startupTimeoutSeconds": 900,
                    "pollingIntervalSeconds": 5,
                    "warmWindows": [],
                },
                "runtime": {"image": "exact-image"},
                "cache": {"tier": "shared"},
            },
        },
    }


def test_zero_worker_setting_preserves_every_other_policy_and_does_not_edit_input():
    before = settings()["serving"]["spec"]
    original = copy.deepcopy(before)
    after = public.zero_worker_spec(before)
    assert before == original
    assert after["availability"]["minReplicas"] == 0
    assert after["availability"]["maxReplicas"] == 1
    after["availability"]["minReplicas"] = 1
    after["availability"]["maxReplicas"] = 2
    assert after == original


@pytest.mark.parametrize("model", public.MODELS)
def test_public_semantics_accept_retained_exact_outputs_and_reject_changed_values(
    model,
):
    receipt = json.loads((Path(__file__).parent / f"{model}-r01.json").read_text())
    for index, response in enumerate(receipt["native_http_predictions"]):
        value = response["body"]
        request = {"samples": [{"sample_id": value["predictions"][0]["sample_id"]}]}
        public.validate_result(model, index, request, value)
        wrong = copy.deepcopy(value)
        field = "phenotypic_age_years" if model == "phenoage" else "predicted_chronological_age_years"
        wrong["predictions"][0][field] += 1
        with pytest.raises(AssertionError, match="retained_prediction_parity"):
            public.validate_result(model, index, request, wrong)
        wrong = {**value, "device": "cuda" if model == "phenoage" else "cpu"}
        with pytest.raises(AssertionError, match="response_device"):
            public.validate_result(model, index, request, wrong)


def test_failed_http_request_is_retained_without_any_retry(tmp_path):
    calls = []

    def handle(request):
        calls.append(request)
        return httpx.Response(409, json={"error": "original conflict"})

    with httpx.Client(base_url="https://example.invalid", transport=httpx.MockTransport(handle)) as client:
        with pytest.raises(AssertionError, match="http_409"):
            public.exchange(
                client,
                public.Trace(tmp_path),
                "POST",
                "/v1/models/phenoage:invoke",
                payload={"x": 1},
            )
    assert len(calls) == 1
    receipt = json.loads(next(tmp_path.glob("*.json")).read_text())
    assert receipt["status"] == 409 and receipt["response"] == {"error": "original conflict"}


def test_key_disclosure_never_persists_secret(tmp_path):
    secret = "task-secret-value"  # noqa: S105 - deliberately synthetic redaction fixture
    with httpx.Client(
        base_url="https://example.invalid",
        transport=httpx.MockTransport(
            lambda request: httpx.Response(201, json={"data": {"key": {"id": "key-id"}, "secret": secret}}),
        ),
    ) as client:
        value, _ = public.exchange(
            client,
            public.Trace(tmp_path),
            "POST",
            "/keys",
            expected=(201,),
            disclosure=True,
        )
    assert value["data"]["secret"] == secret
    assert secret not in next(tmp_path.glob("*.json")).read_text()


@pytest.mark.parametrize("disclosure", [False, True])
def test_non_json_failure_retains_http_evidence_before_parser_error(tmp_path, disclosure):
    calls = []

    def handle(request):
        calls.append(request)
        return httpx.Response(500, text="Internal Server Error")

    with httpx.Client(base_url="https://example.invalid", transport=httpx.MockTransport(handle)) as client:
        with pytest.raises(ValueError):
            public.exchange(client, public.Trace(tmp_path), "POST", "/invoke", disclosure=disclosure)
    row = json.loads(next(tmp_path.glob("*.json")).read_text())
    assert len(calls) == 1
    assert row["status"] == 500
    assert row["response_content_type"] == "text/plain; charset=utf-8"
    assert row["response_bytes"] == 21
    assert len(row["response_sha256"]) == 64
    assert row["error_type"] == "JSONDecodeError"
    assert row["non_json_body"] == (None if disclosure else "Internal Server Error")


def test_parallel_traces_cannot_overwrite_each_other(tmp_path):
    trace = public.Trace(tmp_path)
    with ThreadPoolExecutor(max_workers=2) as workers:
        list(workers.map(lambda index: trace.record("test", {"index": index}), range(50)))
    assert len(list(tmp_path.glob("*.json"))) == 50


def test_accepted_operation_is_preserved_before_later_replay_failure():
    evidence = {"model_id": "phenoage", "app_id": "app-id", "operations": []}
    public.accepted(evidence, {"samples": []}, "durable-op-id", 0)
    assert evidence["operations"][0]["operation_id"] == "durable-op-id"


def test_temporary_key_is_revoked_even_when_first_discovery_fails(tmp_path, monkeypatch):
    admin_trace = public.Trace(tmp_path)
    deleted = []

    def admin_call(admin, trace, method, path, **kwargs):
        if method == "GET" and path.endswith("/settings"):
            return settings()
        if method == "PATCH":
            return {"serving": {"spec": kwargs["payload"]["serving_spec"]}}
        if method == "POST" and path.endswith("/keys"):
            return {"key": {"id": "owned-key"}, "secret": "memory-only"}
        if method == "DELETE" and path.endswith("/owned-key"):
            deleted.append(path)
            return {"state": "revoked"}
        raise AssertionError("unexpected admin mutation")

    original_client = httpx.Client
    monkeypatch.setattr(public, "admin_call", admin_call)
    monkeypatch.setattr(public, "wait_zero", lambda *args: {"zero": True, "at": "2026-09-08T00:00:00Z"})
    monkeypatch.setattr(
        public.httpx,
        "Client",
        lambda **kwargs: original_client(
            **kwargs,
            transport=httpx.MockTransport(
                lambda request: httpx.Response(401 if deleted else 404, json={"error": "expected"})
            ),
        ),
    )
    result = public.run_model(
        SimpleNamespace(output=tmp_path, release="test"),
        "phenoage",
        [],
        "https://example.invalid",
        None,
        admin_trace,
        {"tenant_id": "tenant", "principal_id": "person", "scopes": ["catalog.read"]},
        {"app_id": "new-phenoage-app"},
    )
    assert result["outcome"] == "failed" and result["error_code"] == "http_404"
    assert result["operations"] == [] and result["test_key_revoked"]
    assert deleted == ["/admin/api/v1/keys/owned-key"]


def test_wrong_tenant_fails_before_settings_or_key_mutation(tmp_path, monkeypatch):
    calls = []

    def admin_call(admin, trace, method, path, **kwargs):
        calls.append((method, path))
        assert method == "GET" and path.endswith("/settings")
        return settings()

    monkeypatch.setattr(public, "admin_call", admin_call)
    result = public.run_model(
        SimpleNamespace(output=tmp_path, release="test"),
        "phenoage",
        [],
        "https://example.invalid",
        None,
        public.Trace(tmp_path),
        {"tenant_id": "tenant-academic", "principal_id": "academic", "scopes": ["catalog.read"]},
        {"app_id": "new-phenoage-app"},
    )
    assert result["error_code"] == "source_key_tenant_mismatch"
    assert result["operations"] == []
    assert calls == [("GET", "/admin/api/v1/apps/new-phenoage-app/settings")]


def test_zero_workers_requires_observed_cold_not_desired_or_stale_empty_pods(monkeypatch):
    observations = iter(
        [
            {"at": "1", "containers": {"total": 0}, "summary": {"status": "Desired", "app_id": "app"}},
            {"at": "2", "containers": {"total": 0}, "summary": {"status": "Cold", "app_id": "app"}},
            {"at": "3", "containers": {"total": 0}, "summary": {"status": "Desired", "app_id": "app"}},
            {"at": "4", "containers": {"total": 1}, "summary": {"status": "Cold", "app_id": "app"}},
            {"at": "5", "containers": {"total": 0}, "summary": {"status": "Cold", "app_id": "app"}},
            {"at": "6", "containers": {"total": 0}, "summary": {"status": "Cold", "app_id": "app"}},
        ]
    )
    monkeypatch.setattr(public, "app_observation", lambda *args: next(observations))
    monkeypatch.setattr(public.time, "sleep", lambda _: None)
    result = public.wait_zero(None, None, "/apps/app", public.time.monotonic() + 30, "before")
    assert result["at"] == "6"
