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


async def test_side_effect_free_read_recovers_503(monkeypatch):
    responses = [SimpleNamespace(status_code=503), SimpleNamespace(status_code=200)]
    http = SimpleNamespace(get=AsyncMock(side_effect=responses))
    monkeypatch.setattr(runner.asyncio, "sleep", AsyncMock())
    assert await runner.read_with_retry(http, "https://example.invalid/result", deadline=float("inf")) is responses[1]
    assert http.get.call_count == 2


async def test_read_retries_are_bounded(monkeypatch):
    response = SimpleNamespace(status_code=503)
    http = SimpleNamespace(get=AsyncMock(return_value=response))
    monkeypatch.setattr(runner.asyncio, "sleep", AsyncMock())
    assert await runner.read_with_retry(http, "https://example.invalid/result", deadline=float("inf")) is response
    assert http.get.call_count == 6


@pytest.mark.parametrize("name", ["correct-role", "filename.json"])
async def test_manifest_role_checked_before_upload(tmp_path, name):
    manifest = runner.encoded(
        {
            "entries": [
                {
                    "name": name,
                    "semantic_type": "input/v1",
                    "artifact": {"media_type": "application/json", "compression": "none", "size_bytes": 10},
                }
            ]
        }
    )
    (tmp_path / "manifest.json").write_bytes(manifest)
    inputs = runner.Inputs(
        tmp_path,
        {"objects": [{"path": "manifest.json", "size_bytes": len(manifest), "sha256": runner.sha(manifest)}]},
        AsyncMock(),
    )
    recipe = {"protocol": "scientific-batch-v1", "arguments": {"input_manifest": {"$manifest": "manifest.json"}}}
    contract = {
        "input_artifact_contract": {
            "entry": {
                "name": "correct-role",
                "semantic_type": "input/v1",
                "media_type": "application/json",
                "compression": "none",
                "maximum_bytes": 20,
            }
        }
    }
    if name == "correct-role":
        await runner.validate_manifest_roles(recipe, contract, inputs)
    else:
        with pytest.raises(ValueError, match="scientific_manifest_role_mismatch"):
            await runner.validate_manifest_roles(recipe, contract, inputs)
    inputs.upload.assert_not_called()


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
