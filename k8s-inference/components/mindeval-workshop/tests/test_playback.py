import asyncio
import base64
import json

import httpx
import pytest
from test_api import authentication
from test_store import IDENTITY, create
from test_worker import SETTINGS

from fs2_workshop.app import create_app
from fs2_workshop.models import CreateRuns, Intervention
from fs2_workshop.store import LIVE_PAYLOAD_LIMIT, LiveAudioBus, notify_playback
from fs2_workshop.worker import RemoteFailure, Worker, speech_segments


async def test_shared_pg_listener_fans_out_only_selected_run_and_bounds_slow_clients(store):
    row = await create(store, mode="spoken")
    bus = LiveAudioBus(store.pool)
    await bus.start()
    first, second = bus.subscribe(row["id"]), bus.subscribe(row["id"])
    other = bus.subscribe("another-run")
    try:
        assert store.pool.get_size() - store.pool.get_idle_size() == 1
        await notify_playback(store.pool, row["id"], {"type": "audio.start", "stream_id": "sample"})
        assert (await asyncio.wait_for(first.get(), 2))["stream_id"] == "sample"
        assert (await asyncio.wait_for(second.get(), 2))["stream_id"] == "sample"
        assert other.empty()
        for sequence in range(66):
            bus.receive(
                None, None, None, json.dumps({"run_id": str(row["id"]), "type": "audio.chunk", "sequence": sequence})
            )
        assert first.qsize() <= 64
        assert (await first.get())["type"] == "playback.gap"
        with pytest.raises(ValueError, match="4 KiB"):
            await notify_playback(store.pool, row["id"], {"type": "audio.chunk", "audio_base64": "a" * 4096})
    finally:
        for run_id, queue in ((row["id"], first), (row["id"], second), ("another-run", other)):
            bus.unsubscribe(run_id, queue)
        await bus.close()
    assert bus.subscribers == {}


async def test_pcm_is_notified_before_tts_completion_and_split_below_notify_limit(store):
    row = await create(store, mode="spoken")
    bus = LiveAudioBus(store.pool)
    await bus.start()
    queue, continue_tts = bus.subscribe(row["id"]), asyncio.Event()
    pcm = b"\x01\x00" * 3000

    class TTSStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield (
                json.dumps(
                    {"type": "audio.chunk", "sample_rate_hz": 16000, "audio_base64": base64.b64encode(pcm).decode()}
                )
                + "\n"
            ).encode()
            await continue_tts.wait()
            yield b'{"type":"audio.done"}\n'

    async def upstream(request):
        if request.url.path.endswith("synthesize"):
            return httpx.Response(200, stream=TTSStream())
        return httpx.Response(200, json={"text": "Recognized completed utterance"})

    async def emit(event):
        await store.playback(row, {**event, "stream_id": "stream-1", "turn_index": 1, "role": "clinician"})

    try:
        async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
            task = asyncio.create_task(
                Worker(store, SETTINGS, client).speak_and_listen(
                    "never-in-notifications",
                    "words",
                    "clinician",
                    {"language": "en", "clinician_voice": "Jason"},
                    emit=emit,
                )
            )
            assert (await asyncio.wait_for(queue.get(), 2))["type"] == "audio.start"
            events = [await asyncio.wait_for(queue.get(), 2) for _ in range(3)]
            assert not task.done()
            assert [event["sequence"] for event in events] == [0, 1, 2]
            assert b"".join(base64.b64decode(event["audio_base64"]) for event in events) == pcm
            assert all(len(json.dumps(event).encode()) <= LIVE_PAYLOAD_LIMIT for event in events)
            assert all("never-in-notifications" not in json.dumps(event) for event in events)
            continue_tts.set()
            wav, recognized, metadata = await asyncio.wait_for(task, 2)
            assert wav.startswith(b"RIFF") and recognized == "Recognized completed utterance"
            assert metadata["experience_mode"] == "spoken_not_canonical"
            assert (await queue.get())["type"] == "audio.end"
    finally:
        continue_tts.set()
        bus.unsubscribe(row["id"], queue)
        await bus.close()


async def test_barge_in_cancels_inflight_spoken_work_and_retains_intervention(store):
    row = await create(store, mode="spoken")
    running = await store.claim("worker", 90, 5)
    worker = Worker(store, SETTINGS, None)
    started, cancelled = asyncio.Event(), asyncio.Event()

    async def pending(_row):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    worker._step = pending
    task = asyncio.create_task(worker.step(running))
    await started.wait()
    await store.intervene(row["id"], IDENTITY, Intervention(action="pause"))
    await asyncio.wait_for(task, 2)
    assert cancelled.is_set()
    final = await store.get(row["id"], IDENTITY)
    assert final["status"] == "paused"
    assert final["state"]["interventions"][-1]["barge_in"] is True
    assert await store.pool.fetchval("SELECT count(*) FROM fs2_workshop.audio") == 0


class Socket:
    def __init__(self, token):
        self.headers, self.token = {}, token
        self.sent, self.incoming = asyncio.Queue(), asyncio.Queue()
        self.closed = False

    async def accept(self):
        pass

    async def receive_text(self):
        return json.dumps({"token": self.token})

    async def receive(self):
        return await self.incoming.get()

    async def send_json(self, event):
        await self.sent.put(event)

    async def close(self, code=1000):
        self.closed = True


async def test_websocket_owner_auth_precedes_fanout_and_reconnect_lists_retained_wav(store):
    row = await create(store, mode="spoken")
    running = await store.claim("worker", 90, 5)
    running["state"]["transcript"].append({"role": "clinician", "content": "Retained", "audio_url": "/audio/1"})
    await store.finish_step(running, running["state"], "queued", "turn.completed", {}, audio=(1, b"RIFF-test", {}))
    settings = SETTINGS.model_copy(update={"auth_url": "http://platform/internal/ext-authz"})
    async with httpx.AsyncClient(transport=httpx.MockTransport(authentication)) as client:
        app = create_app(settings, store=store, client=client, start_workers=False)
        endpoint = next(route.endpoint for route in app.routes if getattr(route, "path", "").endswith("/playback"))
        async with app.router.lifespan_context(app):
            denied = Socket("team2")
            await endpoint(denied, row["id"])
            assert (await denied.sent.get())["type"] == "playback.error"
            assert app.state.audio_bus.subscribers == {}
            for _ in range(2):
                owner = Socket("team1")
                task = asyncio.create_task(endpoint(owner, row["id"]))
                ready = await asyncio.wait_for(owner.sent.get(), 2)
                assert ready["type"] == "playback.ready"
                assert ready["recordings"] == [{"turn_index": 1, "url": "/audio/1"}]
                await store.playback(row, {"type": "audio.start", "stream_id": "authorized"})
                assert (await asyncio.wait_for(owner.sent.get(), 2))["stream_id"] == "authorized"
                await owner.incoming.put({"type": "websocket.disconnect"})
                await asyncio.wait_for(task, 2)
                assert app.state.audio_bus.subscribers == {}
            assert (
                await store.pool.fetchval("SELECT wav FROM fs2_workshop.audio WHERE run_id=$1", row["id"])
                == b"RIFF-test"
            )


async def test_expired_lease_cannot_be_revived_or_commit(store):
    row = await create(store)
    running = await store.claim("expired", 90, 5)
    await store.pool.execute(
        "UPDATE fs2_workshop.runs SET lease_until=now()-interval '1 second' WHERE id=$1", row["id"]
    )
    await store.heartbeat(row["id"], "expired", 90)
    assert not await store.current(running)
    assert not await store.finish_step(running, running["state"], "completed", "run.completed", {})
    assert await store.claim("next", 90, 5) is None
    assert (await store.get(row["id"], IDENTITY))["status"] == "interrupted"


async def test_claim_database_outage_retries_without_worker_exit(monkeypatch):
    delays, attempts = [], []

    async def sleep(delay):
        delays.append(delay)

    class FailingStore:
        async def claim(self, *_):
            attempts.append(1)
            if len(attempts) <= 7:
                raise OSError("temporary test outage")
            worker.stopping = True

    worker = Worker(FailingStore(), SETTINGS, None)
    monkeypatch.setattr("fs2_workshop.worker.asyncio.sleep", sleep)
    await worker.loop()
    assert len(attempts) == 8
    assert delays[:7] == [1, 2, 3, 4, 5, 5, 5]


def test_reasoning_default_is_4096_tokens():
    assert CreateRuns(profile_ids=["p"], patient_model="p", clinician_models=["c"]).max_completion_tokens == 4096


@pytest.mark.parametrize("text", ["Sentence ends here. " * 400, "word " * 1500, "x" * 6915])
def test_long_speech_segmentation_preserves_every_character(text):
    pieces = speech_segments(text)
    assert "".join(pieces) == text
    assert all(0 < len(piece) <= 4096 for piece in pieces)
    if text.startswith("Sentence"):
        assert all(piece.endswith(". ") for piece in pieces)


async def test_segmented_tts_uses_same_voice_and_one_asr_upload_without_truncation(store):
    text, requested, asr = "This is a meaningful sentence. " * 240, [], []

    async def upstream(request):
        if request.url.path.endswith("synthesize"):
            body = json.loads(request.content)
            requested.append(body)
            events = [
                {"type": "audio.chunk", "sample_rate_hz": 16000, "audio_base64": "AQACAA=="},
                {"type": "audio.done"},
            ]
            return httpx.Response(200, text="\n".join(map(json.dumps, events)))
        asr.append(request)
        return httpx.Response(200, json={"text": "The full recognized turn"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
        wav, recognized, metadata = await Worker(store, SETTINGS, client).speak_and_listen(
            "key", text, "clinician", {"language": "en", "clinician_voice": "Jason"}
        )
    assert len(requested) == 2 and len(asr) == 1
    assert "".join(body["text"] for body in requested) == text
    assert {body["voice"] for body in requested} == {"Jason"}
    assert len(wav) == 44 + 8 and recognized == "The full recognized turn"
    assert len(metadata["tts_segments"]) == 2


async def test_rate_change_between_segments_fails_explicitly(store):
    calls = []

    async def upstream(request):
        calls.append(request)
        events = [
            {"type": "audio.chunk", "sample_rate_hz": 16000 if len(calls) == 1 else 22050, "audio_base64": "AQACAA=="},
            {"type": "audio.done"},
        ]
        return httpx.Response(200, text="\n".join(map(json.dumps, events)))

    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
        with pytest.raises(RemoteFailure, match="sample rate changed"):
            await Worker(store, SETTINGS, client).speak_and_listen(
                "key", "word " * 1500, "patient", {"language": "en", "patient_voice": "Sofia"}
            )
    assert len(calls) == 2


async def test_heartbeat_recovers_after_temporary_database_error(monkeypatch):
    delays, attempts = [], []

    async def sleep(delay):
        delays.append(delay)
        if len(delays) == 3:
            raise asyncio.CancelledError

    class RecoveringStore:
        async def heartbeat(self, *_):
            attempts.append(1)
            if len(attempts) == 1:
                raise OSError("temporary test outage")

    monkeypatch.setattr("fs2_workshop.worker.asyncio.sleep", sleep)
    worker = Worker(RecoveringStore(), SETTINGS, None)
    with pytest.raises(asyncio.CancelledError):
        await worker.heartbeat("run")
    assert len(attempts) == 2
    assert delays == [30, 5, 30]
