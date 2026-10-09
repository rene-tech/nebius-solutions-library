"""Browser WebSocket -> workshop -> strict speech protocol -> real PostgreSQL."""

import asyncio
import io
import json
import socket
import wave

import httpx
import pytest
import uvicorn
from test_api import authentication
from test_store import IDENTITY, create
from test_worker import SETTINGS
from websockets.asyncio.client import connect
from websockets.asyncio.server import serve

from fs2_workshop.app import create_app
from fs2_workshop.models import Intervention
from fs2_workshop.voice_policy import PARAKEET


@pytest.mark.parametrize("outcome", ["finish", "cancel", "error", "superseded", "auto_eou", "auto_silence"])
async def test_microphone_proxy_finish_cancel_and_atomic_persistence(store, outcome):
    automatic = outcome.startswith("auto_")
    row = await create(store, mode="spoken")
    taken = await store.intervene(row["id"], IDENTITY, Intervention(action="takeover", role="clinician"))
    pcm, observed = b"\x00\x00\x01\x00" * (256 if automatic else 160), []
    chunks = 42 if outcome == "auto_silence" else 1

    async def speech(upstream):
        # This stub enforces the existing CP contract, including its exact
        # control dictionaries. A forwarded session.finish must fail the test.
        assert upstream.request.headers["Authorization"] == "Bearer team1"
        start = json.loads(await upstream.recv())
        assert start == {
            "type": "session.start",
            **({"model": PARAKEET} if automatic else {"options": {"model": "nemotron-speech-en-0-6b"}}),
            "audio": {"encoding": "pcm_s16le", "sample_rate_hz": 16000, "channels": 1},
        }
        await upstream.send(json.dumps({"type": "session.ready", "session_id": "test-session"}))
        assert await upstream.recv() == pcm
        await upstream.send(json.dumps({"type": "transcript.partial", "text": "A partial"}))
        if outcome == "auto_eou":
            await upstream.send(json.dumps({"type": "turn.eou", "source": "model_token", "segment": 0}))
        for _ in range(chunks - 1):
            assert await upstream.recv() == pcm
        control = json.loads(await upstream.recv())
        observed.append(control)
        assert control == {
            "type": "session.cancel" if outcome == "cancel" else "session.finish" if automatic else "input.finish"
        }
        if outcome == "cancel":
            await upstream.send(json.dumps({"type": "session.cancelled", "session_id": "test-session"}))
            return
        if outcome == "error":
            await upstream.send(json.dumps({"type": "session.error", "code": "speech_session_rejected"}))
            return
        if outcome == "superseded":
            await store.intervene(row["id"], IDENTITY, Intervention(action="pause"))
        for sequence, text in enumerate(["A complete", "human turn."]):
            await upstream.send(json.dumps({"type": "transcript.final", "sequence": sequence, "text": text}))
        await upstream.send(
            json.dumps({"type": "session.done" if automatic else "session.completed", "session_id": "test-session"})
        )

    class Probabilities:
        def probability(self, samples, state, context):
            updated = state.copy()
            updated[0, 0, 0] += 1
            return (0.9 if updated[0, 0, 0] <= 4 else 0.0), updated, context

    async with serve(speech, "127.0.0.1", 0) as speech_server:
        speech_port = speech_server.sockets[0].getsockname()[1]
        settings = SETTINGS.model_copy(
            update={
                "auth_url": "http://platform/internal/ext-authz",
                "speech_url": f"ws://127.0.0.1:{speech_port}/v1/audio/stream",
            }
        )
        async with httpx.AsyncClient(transport=httpx.MockTransport(authentication)) as client:
            app = create_app(settings, store=store, client=client, start_workers=False)
            with socket.socket() as listener:
                listener.bind(("127.0.0.1", 0))
                listener.listen()
                port = listener.getsockname()[1]
                server = uvicorn.Server(uvicorn.Config(app, log_level="error", ws="websockets-sansio"))
                serving = asyncio.create_task(server.serve(sockets=[listener]))
                try:
                    async with asyncio.timeout(5):
                        while not server.started:
                            await asyncio.sleep(0.01)
                    if automatic:
                        app.state.silero = Probabilities()
                    url = f"ws://127.0.0.1:{port}/v1/workshop/runs/{row['id']}/microphone"
                    events = []
                    async with asyncio.timeout(5), connect(url, origin=settings.public_origin) as browser:
                        await browser.send(
                            json.dumps(
                                {"token": "team1", **({"model": PARAKEET, "auto_finish": True} if automatic else {})}
                            )
                        )
                        assert json.loads(await browser.recv())["type"] == "session.ready"
                        for _ in range(chunks):
                            await browser.send(pcm)
                        if not automatic:
                            assert json.loads(await browser.recv())["type"] == "transcript.partial"
                            control_type = "session.cancel" if outcome == "cancel" else "session.finish"
                            await browser.send(json.dumps({"type": control_type}))
                        async for payload in browser:
                            events.append(json.loads(payload))
                finally:
                    server.should_exit = True
                    await asyncio.wait_for(serving, 5)

    assert observed == [
        {"type": "session.cancel" if outcome == "cancel" else "session.finish" if automatic else "input.finish"}
    ]
    final = await store.get(row["id"], IDENTITY)
    recordings = await store.pool.fetch("SELECT * FROM fs2_workshop.audio WHERE run_id=$1", row["id"])
    if outcome == "finish" or automatic:
        assert [
            event["type"]
            for event in events
            if not event["type"].startswith("voice_policy.") and event["type"] not in {"transcript.partial", "turn.eou"}
        ] == [
            "transcript.final",
            "transcript.final",
            "session.done" if automatic else "session.completed",
            "workshop.message_submitted",
        ]
        turn = final["state"]["transcript"][-1]
        assert turn["content"] == "A complete human turn."
        assert turn["human"] and turn["source"] == "microphone" and turn["role"] == "clinician"
        assert final["version"] == taken["version"] + 1 and final["status"] == "queued"
        assert len(recordings) == 1
        with wave.open(io.BytesIO(recordings[0]["wav"])) as recording:
            assert recording.getframerate() == 16000 and recording.getnchannels() == 1
            assert recording.readframes(recording.getnframes()) == pcm * chunks
        assert events[-1]["run"]["state"]["transcript"][-1]["audio_url"].endswith("/audio/1")
        if automatic:
            metadata = json.loads(recordings[0]["metadata"])["voice_policy"]
            source = "parakeet_model_eou" if outcome == "auto_eou" else "silero_vad_silence"
            assert any(event["source"] == source for event in metadata["events"])
            assert sum(event["type"] == "voice_policy.input_finish" for event in events) == 1
    else:
        assert not recordings and final["state"]["transcript"] == taken["state"]["transcript"]
        expected = {"cancel": "session.cancelled", "error": "session.error", "superseded": "workshop.error"}
        assert events[-1]["type"] == expected[outcome]
        assert not any(event["type"] == "workshop.message_submitted" for event in events)
        assert final["status"] == ("paused" if outcome == "superseded" else "takeover")
