import hashlib
import json

import httpx
import pytest

import reconcile_agent as module


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value))


def test_agent_definition_without_submission_is_not_an_executed_chat(tmp_path):
    folder = tmp_path / "agent-b/model/mpinat-example"
    write(folder / "agent.json", {"model": "moonshotai/Kimi-K3", "model_parameters": {"reasoning_effort": "high"}})
    row = module.chat_record(folder)
    assert row["chat_submission_recorded"] is False
    assert row["prompt"] is None
    assert row["saved_messages"] == 0


def test_only_real_user_prompt_and_study_receipt_are_correlated(tmp_path):
    folder = tmp_path / "agent-c/model/mpinat-example"
    write(folder / "submission.json", {"conversationId": "conversation"})
    write(folder / "messages.json", [
        {"isCreatedByUser": True, "text": "Actual scientific request"},
        {"isCreatedByUser": False, "content": [{"type": "tool_call", "tool_call": {
            "name": "run_scientific_workflow", "output": json.dumps({"durable_study": True, "id": "study", "state": "queued"})}}]},
    ])
    row = module.chat_record(folder)
    assert row["prompt"] == "Actual scientific request"
    assert row["study_submissions"][0]["id"] == "study"
    assert row["chat_submission_recorded"] is True


def test_native_timings_require_actual_hash_verified_result_not_envelope(tmp_path):
    folder = tmp_path / "replays/cohort/model/mpinat-example/receipt"
    request = {"parameters": {"jobs": [{"id": "benchmark", "steps": [
        {"id": "repeat-1", "command": "mdrun", "args": []}]}]}}
    write(folder / "request.json", request)
    native = {"schema": "fs2-serve.nebius.ai/gromacs-workflow-result/v1", "operation_id": "operation",
              "job_id": "benchmark", "status": "succeeded", "completed_steps": ["repeat-1"],
              "commands": [{"step_id": "repeat-1", "command": ["gmx", "mdrun"], "exit_code": 0,
                            "performance_ns_per_day": 42.5, "wall_seconds": 123.4}]}
    write(folder / "runtime.artifact", native)
    source = folder / "runtime.artifact"
    ref = {"semantic_type": module.NATIVE_TYPE, "path": "/workspace/" + str(source.relative_to(tmp_path)),
           "sha256": hashlib.sha256(source.read_bytes()).hexdigest(), "size_bytes": source.stat().st_size}
    write(folder / "receipt.json", {"operation_id": "operation", "state": "verified", "verified_artifacts": [ref]})
    row = module.saved_receipts(tmp_path)[0]
    assert row["native_timings"]["benchmark_complete"] is True
    assert row["native_timings"]["repeats"][0]["command_wall_seconds"] == 123.4
    write(source, {"output_manifest": {}, "terminal_status": "succeeded"})
    row = module.saved_receipts(tmp_path)[0]
    assert row["native_timings"] is None
    assert "differs" in row["errors"][0]


def test_empty_csv_is_not_scientific_zero_or_report_success(tmp_path):
    path = tmp_path / "replays/cohort/model/mpinat-example/metrics.csv"
    path.parent.mkdir(parents=True)
    path.write_text("repeat,ns_per_day,wall_seconds\n")
    row = module.report_records(tmp_path)[0]
    assert row["empty_timing_table"] is True
    assert row["csv_data_rows"] == 0


def test_unresolved_operation_is_never_scheduled_as_new_work():
    suite = {"cases": [{"id": "example"}]}
    receipts = [{"case_id": "mpinat-example", "operation_id": "op", "native_timings": None}]
    rows = module.coverage(suite, [], receipts, [], [], {})
    assert rows[0]["followup"] == "resolve-existing-operation"
    followup = module.followups(rows, receipts, {})
    assert not followup["remaining"]["cases"]
    assert followup["unresolved"][0]["operation_ids"] == ["op"]


def test_successful_existing_native_result_generates_zero_gpu_recovery():
    suite = {"cases": [{"id": "example"}]}
    receipts = [{"case_id": "mpinat-example", "operation_id": "op", "native_timings": None},
                {"case_id": "mpinat-example", "operation_id": "op", "native_timings": {
                    "benchmark_complete": True, "repeats": [{"step_id": "repeat-1"}]}}]
    rows = module.coverage(suite, [], receipts, [], [], {"op": {"status": "succeeded"}})
    followup = module.followups(rows, receipts, {})
    case = followup["recovery"]["cases"][0]
    assert case["operation_id"] == "op"
    assert case["new_scientific_operations_allowed"] == 0
    assert not followup["remaining"]["cases"]


def test_live_reconciliation_uses_only_get_and_existing_qa(tmp_path, monkeypatch):
    env = tmp_path / "qa.env"
    env.write_text("SCIENTIFIC_MODELS_API_KEY=" + module.QA_PREFIX + "_fixture\nSCIENTIFIC_MODELS_MCP_URL=https://offline.invalid/mcp\n")
    calls = []
    def handle(request):
        calls.append((request.method, request.url.path))
        return httpx.Response(200, json={"operation": {"status": "failed"},
                                        "batch": {"result_published": True, "stages": []}})
    original = httpx.Client
    monkeypatch.setattr(module.httpx, "Client", lambda **kwargs: original(**kwargs, transport=httpx.MockTransport(handle)))
    value = module.observe_operations({"op"}, env)
    assert value["op"]["status"] == "failed"
    assert calls == [("GET", "/v1/operations/op")]
    assert "fixture" not in json.dumps(value)
    env.write_text("SCIENTIFIC_MODELS_API_KEY=customer-key\n")
    with pytest.raises(ValueError, match="system/qa"):
        module.observe_operations({"op"}, env)
    assert len(calls) == 1


def test_evidence_paths_cannot_escape_workspace(tmp_path):
    with pytest.raises(ValueError):
        module.local_path("/workspace/../etc/passwd", tmp_path)


def test_historical_messages_cannot_be_read_from_customer_container():
    with pytest.raises(ValueError, match="recorded QA container"):
        module.missing_chat_messages([], "lynx-customer-instance")
