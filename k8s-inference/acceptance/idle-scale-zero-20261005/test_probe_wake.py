import copy
import json

import pytest

import probe_wake


@pytest.mark.parametrize("frames,duration", [(61, "3.812500"), (65, "4.062500")])
def test_wan_video_validates_requested_duration_not_another_variants_frame_count(frames, duration):
    semantic = {"streams": [{"width": 832, "height": 480, "nb_frames": str(frames), "avg_frame_rate": "16/1"}],
                "format": {"duration": duration}}
    probe_wake.validate_wan_video(semantic, {"size": "832x480", "seconds": 4})
    wrong = copy.deepcopy(semantic)
    wrong["streams"][0]["nb_frames"] = "1"
    with pytest.raises(AssertionError):
        probe_wake.validate_wan_video(wrong, {"size": "832x480", "seconds": 4})
    with pytest.raises(AssertionError):
        probe_wake.validate_wan_video(semantic, {"size": "1280x720", "seconds": 4})
    with pytest.raises(AssertionError):
        probe_wake.validate_wan_video(semantic, {"size": "832x480", "seconds": 12})


@pytest.mark.asyncio
async def test_mcp_poll_is_paced_and_preserves_operation_identity(monkeypatch):
    states = iter(("queued", "activating", "succeeded"))
    calls, waits = [], []

    class Client:
        async def call_tool(self, name, arguments):
            calls.append((name, arguments))
            return {"id": "retained-operation", "status": next(states)}

    async def sleep(seconds):
        waits.append(seconds)

    monkeypatch.setattr(probe_wake.VISUAL, "data", lambda value: value)
    monkeypatch.setattr(probe_wake.asyncio, "sleep", sleep)
    result, observed = await probe_wake.poll(Client(), "retained-operation", 30)
    assert result["status"] == "succeeded"
    assert [row["status"] for row in observed] == ["queued", "activating", "succeeded"]
    assert waits == [5, 5]
    assert calls == [("get_operation", {"operation_id": "retained-operation"})] * 3


@pytest.mark.asyncio
async def test_mcp_poll_does_not_hide_transport_failure():
    class Client:
        async def call_tool(self, name, arguments):
            raise RuntimeError("retained test transport failure")

    with pytest.raises(RuntimeError, match="retained test transport failure"):
        await probe_wake.poll(Client(), "retained-operation", 30)


def test_mindguard_semantics_reject_wrong_revision_and_incomplete_context():
    lock = json.loads((probe_wake.ROOT / "models/mindguard/public-models.lock.json").read_text())
    model = "mindguard-4b"
    revision = lock["models"][model]["revision"]
    result = {
        "model_id": model, "model_revision": revision, "role": "safety_classifier",
        "status": "completed", "enforcement": "observe",
        "evaluated_user_turns": 2, "input_user_turns": 2,
        "assessments": [
            {"model_id": model, "model_revision": revision, "role": "safety_classifier",
             "enforcement": "observe", "status": "completed", "error": None,
             "safety": "safe", "categories": [],
             "coverage": {"target_user_message_index": index * 2,
                          "context_message_count": index * 2 + 1, "truncated": False}}
            for index in range(2)
        ],
    }
    assert probe_wake.MINDGUARD.validate(result, model)["model_revision"] == revision
    wrong = copy.deepcopy(result)
    wrong["assessments"][1]["model_revision"] = "wrong"
    with pytest.raises(AssertionError):
        probe_wake.MINDGUARD.validate(wrong, model)
    wrong = copy.deepcopy(result)
    wrong["assessments"][1]["coverage"]["context_message_count"] = 1
    with pytest.raises(AssertionError):
        probe_wake.MINDGUARD.validate(wrong, model)
