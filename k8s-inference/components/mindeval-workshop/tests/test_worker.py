import base64
import json

import httpx
import pytest
import respx
from test_store import IDENTITY, create

from fs2_workshop.models import Settings
from fs2_workshop.worker import RemoteFailure, Worker

SETTINGS = Settings(
    database_url="unused", gateway_url="http://gateway", platform_url="http://platform", mindguard_model=None
)


@respx.mock
async def test_real_queue_drives_canonical_sequence_and_judge(store):
    row = await create(store, max_turns=2)
    respx.post(f"http://gateway/v1/mindeval/runs/{row['id']}/register").respond(200, json={"run_id": str(row["id"])})
    respx.get("http://gateway/v1/mindeval/profiles/profile-000").respond(
        200, json={"patient_system_prompt": "p", "clinician_system_prompt": "c"}
    )
    completion = respx.post("http://gateway/v1/mindeval/completions").respond(
        200, json={"content": "A complete turn", "usage": {"total_tokens": 10}}
    )
    respx.post("http://gateway/v1/mindeval/judgments").respond(
        200, json={"judgment": {f"criterion{i}": 3.0 for i in range(5)}, "overall_score": 3}
    )
    async with httpx.AsyncClient() as client:
        worker = Worker(store, SETTINGS, client)
        for _ in range(6):
            claimed = await store.claim(worker.owner, 90, 5)
            await worker.step(claimed)
    final = await store.get(row["id"], IDENTITY)
    assert final["status"] == "completed"
    assert final["state"]["benchmark_eligible"] is True
    assert len(final["state"]["transcript"]) == 5
    calls = [json.loads(c.request.content) for c in completion.calls]
    assert [c["role"] for c in calls] == ["clinician", "patient", "clinician", "patient"]
    assert calls[0]["messages"][1] == {"role": "user", "content": "Hello"}
    assert calls[1]["messages"][1] == {"role": "assistant", "content": "Hello"}
    assert final["credential_ciphertext"] is None


@respx.mock
async def test_speech_waits_for_durable_asr_operation(store, monkeypatch):
    async def immediate(_):
        pass

    monkeypatch.setattr("fs2_workshop.worker.asyncio.sleep", immediate)
    events = [
        {"type": "audio.chunk", "sample_rate_hz": 22050, "audio_base64": base64.b64encode(b"\0\0" * 20).decode()},
        {"type": "audio.done"},
    ]
    respx.post("http://platform/v1/voice/synthesize").respond(200, text="\n".join(map(json.dumps, events)))
    respx.post("http://platform/v1/audio/transcriptions").respond(
        202, json={"status": "queued"}, headers={"location": "/v1/operations/123"}
    )
    poll = respx.get("http://platform/v1/operations/123").mock(
        side_effect=[httpx.Response(200, json={"status": "running"}), httpx.Response(200, json={"status": "succeeded"})]
    )
    respx.get("http://platform/v1/operations/123/result").respond(200, json={"text": "Recognized speech"})
    async with httpx.AsyncClient() as client:
        audio, text, meta = await Worker(store, SETTINGS, client).speak_and_listen(
            "key", "words", "patient", {"language": "en", "patient_voice": "Sofia"}
        )
    assert audio.startswith(b"RIFF") and text == "Recognized speech" and poll.call_count == 2
    assert meta["first_audio_seconds"] is not None


@respx.mock
async def test_incomplete_tts_never_looks_successful(store):
    respx.post("http://platform/v1/voice/synthesize").respond(200, text='{"type":"audio.start"}')
    async with httpx.AsyncClient() as client:
        with pytest.raises(RemoteFailure, match="complete audio"):
            await Worker(store, SETTINGS, client).speak_and_listen(
                "key", "words", "patient", {"language": "en", "patient_voice": "Sofia"}
            )


@respx.mock
@pytest.mark.parametrize("observer_status", [200, 421])
async def test_observer_uses_operator_public_authority_and_retains_failure_status(store, observer_status):
    row = await create(store, max_turns=2)
    claimed = await store.claim("worker", 90, 5)
    claimed["state"]["registration"] = {"run_id": str(row["id"])}
    claimed["state"]["transcript"].extend(
        [{"role": role, "content": "A turn"} for role in ["clinician", "patient", "clinician", "patient"]]
    )
    respx.post("http://gateway/v1/mindeval/judgments").respond(200, json={"judgment": {"axis": 3}, "overall_score": 3})

    def observe(request):
        assert request.headers["host"] == "workshop.example.test"
        assert request.headers["authorization"].startswith("Bearer ")
        return httpx.Response(observer_status, json={"status": "completed", "evaluated_user_turns": 3})

    respx.post("http://platform/v1/mindguard/assess").mock(side_effect=observe)
    settings = SETTINGS.model_copy(
        update={"public_origin": "https://workshop.example.test", "mindguard_model": "mindguard-4b"}
    )
    async with httpx.AsyncClient() as client:
        await Worker(store, settings, client).step(claimed)
    final = await store.get(row["id"], IDENTITY)
    assert final["status"] == "completed"
    classification = final["state"]["classification"]
    if observer_status == 200:
        assert classification["status"] == "completed"
    else:
        assert classification["status"] == "unavailable"
        assert classification["error"]["http_status"] == 421
