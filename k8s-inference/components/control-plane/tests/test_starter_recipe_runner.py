"""File bindings and retries must not misrepresent or duplicate a model run."""

import importlib.util
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

SOURCE = Path(__file__).resolve().parents[3] / "starter-data/run_example.py"
SPEC = importlib.util.spec_from_file_location("starter_runner_test", SOURCE)
runner = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(runner)


def response(error=None):
    payload = {"error": error} if error else {"id": "a-run"}
    return SimpleNamespace(model_dump=lambda **kwargs: {"structuredContent": payload, "isError": bool(error)})


async def test_explicit_nonadmission_retries_same_arguments(monkeypatch):
    rejected = response({"code": "admission_limit_reached", "durable_admission": False})
    accepted = response()
    client = SimpleNamespace(call_tool=AsyncMock(side_effect=[rejected, accepted]))
    monkeypatch.setattr(runner.asyncio, "sleep", AsyncMock())
    arguments = {"idempotency_key": "one-intended-run"}
    assert await runner.call_with_capacity_wait(client, "typed", arguments, deadline=float("inf")) is accepted
    assert client.call_tool.call_count == 2
    assert all(call.args == ("typed", arguments) for call in client.call_tool.call_args_list)


async def test_durable_or_unknown_admission_never_retried():
    client = SimpleNamespace(call_tool=AsyncMock(return_value=response({"code": "admission_limit_reached"})))
    with pytest.raises(runner.ToolRejected):
        await runner.call_with_capacity_wait(client, "typed", {}, deadline=float("inf"))
    assert client.call_tool.call_count == 1


async def test_structured_protocol_rejection_preserves_nonadmission(monkeypatch):
    from mcp import MCPError

    client = SimpleNamespace(
        call_tool=AsyncMock(
            side_effect=MCPError(-32602, "invalid", {"type": "input_invalid", "durable_admission": False})
        )
    )
    with pytest.raises(runner.ToolRejected, match="input_invalid"):
        await runner.call_with_capacity_wait(client, "typed", {}, deadline=float("inf"))
    assert client.call_tool.call_count == 1


async def test_capacity_wait_stops_at_deadline():
    rejected = SimpleNamespace(status_code=429, json=lambda: {"error": {"type": "concurrency_exceeded"}})
    http = SimpleNamespace(request=AsyncMock(return_value=rejected))
    assert await runner.request_with_capacity_wait(http, "POST", "https://example.invalid", deadline=0) is rejected
    assert http.request.call_count == 1


async def test_file_binding_rejects_changed_asset(tmp_path):
    (tmp_path / "input.txt").write_bytes(b"expected")
    manifest = {"objects": [{"path": "input.txt", "size_bytes": 8, "sha256": runner.sha(b"expected")}]}
    inputs = runner.Inputs(tmp_path, manifest, runner.offline_upload)
    assert await inputs.materialize({"$file": "input.txt", "encoding": "text"}) == "expected"
    (tmp_path / "input.txt").write_bytes(b"modified")
    with pytest.raises(ValueError, match="input_changed"):
        await inputs.materialize({"$file": "input.txt", "encoding": "text"})


async def test_json_artifact_fingerprint_includes_nested_file_bytes(tmp_path):
    template = runner.encoded({"asset": {"$file": "input.txt", "encoding": "artifact", "media_type": "text/plain"}})
    (tmp_path / "template.json").write_bytes(template)
    (tmp_path / "input.txt").write_bytes(b"first")
    objects = [
        {"path": p.name, "size_bytes": p.stat().st_size, "sha256": runner.sha(p.read_bytes())}
        for p in tmp_path.iterdir()
    ]
    first = await runner.Inputs(tmp_path, {"objects": objects}, runner.offline_upload).materialize(
        {"$json_artifact": "template.json"}
    )
    (tmp_path / "input.txt").write_bytes(b"other")
    for obj in objects:
        if obj["path"] == "input.txt":
            obj["sha256"] = runner.sha(b"other")
    second = await runner.Inputs(tmp_path, {"objects": objects}, runner.offline_upload).materialize(
        {"$json_artifact": "template.json"}
    )
    assert first["sha256"] != second["sha256"]
