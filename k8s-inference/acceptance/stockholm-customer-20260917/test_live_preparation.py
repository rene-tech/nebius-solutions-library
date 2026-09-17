"""Offline collector/runner contract tests; never calls a cluster or model."""

import hashlib
import importlib.util
import sys
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, HERE / filename)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


collector = load("collect_live", "collect_live.py")
runner = load("stockholm_run_live", "run_live.py")
client_probe = load("stockholm_client_probe", "probe_librechat.py")
finalizer = load("stockholm_finish_live", "finish_live.py")


def team():
    return {"tenant_id": "stockholm", "name": "stockholm-team-01", "principal_id": "team-01",
            "models": ["*"], "scopes": sorted(collector.REQUIRED_SCOPES), "max_concurrency": 5,
            "request_budget": None, "gpu_seconds_budget": None, "rate_limit_requests": None,
            "rate_window_seconds": None, "revoked_at": None, "expires_at": None}


def canary():
    secret = "synthetic-not-a-real-key"
    row = dict(team(), name="stockholm-canary-test", principal_id="stockholm-canary-test",
               id="test-id", expires_at=(datetime.now(UTC) + timedelta(hours=1)).isoformat(),
               fingerprint=hashlib.sha256(secret.encode()).hexdigest())
    key = {"schema": "fs2-customer-key/v1", "disposable": True, "secret": secret,
           "token_id": row["id"], "principal_id": row["principal_id"]}
    snapshot = {"token_metadata": [row], "team_policy": collector.policy(team())}
    return key, snapshot


def test_live_wildcard_team_policy():
    result = collector.team_policy([team(), dict(team(), name="stockholm-team-02")])
    assert result["team_count"] == 2
    assert result["team_policy"]["models"] == ["*"]


def test_diverging_team_policy_refused():
    with pytest.raises(ValueError, match="policies_diverge"):
        collector.team_policy([team(), dict(team(), max_concurrency=1)])


def test_same_policy_canary():
    key, snapshot = canary()
    assert runner.validate_canary(key, snapshot) == key["secret"]


@pytest.mark.parametrize("field,value", [("principal_id", "team-01"), ("name", "stockholm-team-01"),
    ("expires_at", None), ("max_concurrency", 10), ("scopes", ["tenant.admin"]), ("revoked_at", "now")])
def test_wrong_canary_refused(field, value):
    key, snapshot = canary()
    snapshot["token_metadata"][0][field] = value
    with pytest.raises(ValueError):
        runner.validate_canary(key, snapshot)


@pytest.mark.parametrize("model", runner.MODELS)
@pytest.mark.parametrize("scenario", runner.SCENARIOS)
def test_real_fixture_argument_forms(model, scenario):
    fixture = runner.portable_payloads(model)[0]
    original = deepcopy(fixture)
    value = runner.arguments(model, scenario, fixture, "test-idempotency-key", "live-discovered-operation")
    assert fixture == original
    if scenario == "legacy-generic-sdk":
        assert value["payload"]["wait_seconds"] == 0 and "wait_seconds" not in value
    elif scenario == "public-api":
        assert value["operation"] == "live-discovered-operation" and value["payload"] == original
    else:
        assert value["wait_seconds"] == 0


def test_server_times_measure_overlap_not_tasks():
    rows = [{"terminal_operation": {"accepted_at": f"2026-09-17T12:00:0{i}Z",
                                   "completed_at": "2026-09-17T12:00:10Z"}} for i in range(5)]
    assert runner.concurrent_peak(rows) == 5
    rows[-1]["terminal_operation"]["accepted_at"] = "2026-09-17T12:00:10Z"
    assert runner.concurrent_peak(rows) == 4


def test_private_receipts_refuse_secret_and_existing_file(tmp_path):
    path = tmp_path / "receipt.json"
    with pytest.raises(ValueError, match="secret"):
        collector.write_private(path, {"leak": "secret"}, ("secret",))
    collector.write_private(path, {"fine": True})
    assert path.stat().st_mode & 0o777 == 0o600
    with pytest.raises(FileExistsError):
        collector.write_private(path, {})


def test_plan_never_claims_customer_readiness():
    plan = runner.offline_plan()
    assert plan["customer_ready"] is False
    assert "actual LibreChat/installed-skill execution" in plan["not_proven"]
    assert plan["mixed_submission_order"] == ["boltz2"] * 4 + ["openfold2"]
    assert plan["mixed_parallel_submissions"] == 5


def test_scientific_fragment_loads_without_network(tmp_path):
    module = runner.scientific_module()
    config = module.RunConfig(endpoint="https://example.invalid", repository_root=runner.ROOT,
        activation_fragment=runner.ROOT / "models/structure/batch-adapters/esmfold2/activation/public-acceptance.json",
        receipt_path=tmp_path / "receipt.json", run_id="offline-validation")
    model, request, declarations, _ = module._activation(config)
    assert model == "esmfold2" and request["parameters"]["seed"] == 101
    assert len(declarations) == 2


def test_actual_client_operation_correlation_ignores_conversation_ids():
    import json
    from uuid import uuid4

    op_id = str(uuid4())
    value = {"conversationId": str(uuid4()), "content": [{"text": json.dumps({
        "operation": {"id": op_id, "status": "succeeded", "model_id": "openfold2"}})}]}
    assert client_probe.operations(value) == {op_id}
    assert client_probe.operations({"id": str(uuid4()), "status": "succeeded", "model_id": "unrelated"}) == set()


def test_batch_refuses_unadvertised_byte_tool_before_admission():
    with pytest.raises(ValueError, match="batch_download_tool_not_advertised"):
        runner.require_batch_download_tool({"read_scientific_artifact_bytes": {}})
    runner.require_batch_download_tool({"download_scientific_artifact": {}})


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", [None, "digest", "oversized", "reference", "expired", "http"])
async def test_batch_reads_advertised_handles_and_verifies_exact_bytes(monkeypatch, fault):
    import json
    from contextlib import asynccontextmanager
    from uuid import uuid4

    raw = b"synthetic structure bytes"
    output = {"artifact_id": str(uuid4()), "size_bytes": len(raw),
              "sha256": hashlib.sha256(raw).hexdigest()}
    manifest_bytes = json.dumps({"entries": [{"artifact": output}]}).encode()
    manifest = {"artifact_id": str(uuid4()), "size_bytes": len(manifest_bytes),
                "sha256": hashlib.sha256(manifest_bytes).hexdigest()}
    bodies = {output["artifact_id"]: raw, manifest["artifact_id"]: manifest_bytes}
    references = {row["artifact_id"]: row for row in (output, manifest)}
    observed = []

    class MCP:
        async def call_tool(self, name, args):
            assert name == "download_scientific_artifact"
            identifier = args["artifact_id"]
            reference = dict(references[identifier])
            if fault == "reference":
                reference["sha256"] = "0" * 64
            return {"artifact": reference, "handle": {
                "method": "GET", "url": "https://objects.example/" + identifier + "?signature=transient",
                "headers": {"x-test-signature": "transient"}, "expires_at": (
                    datetime.now(UTC) + timedelta(minutes=-5 if fault == "expired" else 5)).isoformat()}}

    class Response:
        status_code = 403 if fault == "http" else 200

        def __init__(self, body):
            self.body = body

        async def aiter_raw(self):
            if fault == "digest":
                yield b"x" * len(self.body)
            else:
                yield self.body + (b"x" if fault == "oversized" else b"")

    class HTTP:
        @asynccontextmanager
        async def stream(self, method, url, headers):
            from urllib.parse import urlsplit

            assert method == "GET" and headers == {"x-test-signature": "transient"}
            identifier = urlsplit(url).path.removeprefix("/")
            observed.append(identifier)
            yield Response(bodies[identifier])

    monkeypatch.setattr(runner, "_mcp_result", lambda value: value)
    invocation = runner.batch_downloads(MCP(), {"artifact_digests": {"output_manifest": manifest}},
                                      {"download_scientific_artifact": {}}, HTTP())
    if fault:
        with pytest.raises(ValueError):
            await invocation
    else:
        measured = await invocation
        assert observed == [manifest["artifact_id"], output["artifact_id"]]
        assert measured == [manifest, output]
        assert "transient" not in json.dumps(measured)


@pytest.mark.parametrize("rows,expected", [([], set()),
    ([{"id": "known", "status": "running"}], {"known"}),
    ([{"id": "unrelated", "status": "succeeded"}], {"missing"})])
def test_teardown_refuses_active_empty_or_missing_operations(rows, expected):
    with pytest.raises(ValueError):
        finalizer.settled(rows, expected)


def test_teardown_retains_failed_attempts_without_requiring_deletion():
    finalizer.settled([{"id": "current", "status": "succeeded"},
                       {"id": "earlier", "status": "failed"}], {"current"})


def test_teardown_refuses_partial_cohort_receipt():
    with pytest.raises(ValueError, match="two_completed_bounded_cohorts_required"):
        finalizer.expected_operations({"outcome": "partial_scope_passed", "cohorts": [{}]})


def batch_transport(failures):
    from types import SimpleNamespace

    class FailedTransport(Exception):
        code = "http_transport_failed"

    class Transport:
        host, tls = "example.invalid", True

        def __init__(self):
            self.calls = []

        def request(self, method, path, **kwargs):
            self.calls.append((method, path, kwargs))
            if len(self.calls) <= failures:
                raise FailedTransport()
            return SimpleNamespace(status=200, body=b'{"operation_id":"e10a1fc9-316e-40cb-81f5-596022b383bf"}')

    return Transport(), FailedTransport


def test_batch_transport_retries_identical_get_and_keeps_failure(tmp_path):
    transport, _ = batch_transport(1)
    sleeps = []
    client = runner.RecordedBatchClient(transport, tmp_path / "journal.json", "synthetic-secret", sleep=sleeps.append)
    client.request("GET", "/v1/operations/existing")
    assert transport.calls == [("GET", "/v1/operations/existing", {})] * 2
    assert sleeps == [1]
    journal = collector.private_json(tmp_path / "journal.json")
    assert journal["transport_failure_count"] == 1 and journal["clean_transport"] is False
    assert journal["known_operations"] == ["e10a1fc9-316e-40cb-81f5-596022b383bf"]
    assert journal["failures"][0]["started_at"] <= journal["failures"][0]["failed_at"]


@pytest.mark.parametrize("method", ["POST", "PUT"])
def test_batch_transport_never_retries_mutation(tmp_path, method):
    transport, error = batch_transport(1)
    client = runner.RecordedBatchClient(transport, tmp_path / "journal.json", "synthetic-secret", sleep=lambda _: None)
    with pytest.raises(error):
        client.request(method, "/v1/scientific-artifacts/uploads")
    assert len(transport.calls) == 1
    assert client.journal["get_retry_count"] == 0


def test_batch_transport_three_failures_exhaust_entire_budget(tmp_path):
    transport, error = batch_transport(9)
    client = runner.RecordedBatchClient(transport, tmp_path / "journal.json", "synthetic-secret", sleep=lambda _: None)
    with pytest.raises(error):
        client.request("GET", "/v1/operations/existing")
    assert len(transport.calls) == 3 and client.journal["transport_failure_count"] == 3
