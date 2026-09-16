"""Full-recording public WebSocket cohort; no synthetic reference or readiness claim."""

import asyncio
import hashlib
import json
import time
import wave
from uuid import uuid4

import httpx
from websockets.asyncio.client import connect


async def run_one(origin, key, model, source, language, row, *, paced):
    row.update(model=model, input_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
               mode="websocket-paced" if paced else "websocket-unpaced", events=[])
    with wave.open(str(source), "rb") as audio:
        if (audio.getnchannels(), audio.getsampwidth(), audio.getframerate()) != (1, 2, 16000):
            raise ValueError("cohort requires supplied mono16k PCM16 assets")
        duration = audio.getnframes() / 16000
        row["source_audio_seconds"] = duration
        started = time.monotonic()
        ready = None
        producer = None
        finished = asyncio.Event()
        sent_bytes = 0
        url = origin.replace("https://", "wss://").replace("http://", "ws://") + "/v1/audio/stream"
        async with connect(url, additional_headers={"authorization": "Bearer " + key,
                "idempotency-key": "speech-live-public-" + uuid4().hex},
                open_timeout=30, close_timeout=5, max_size=1024*1024, max_queue=2, proxy=None) as socket:
            await socket.send(json.dumps({"type": "session.start", "options": {"model": model, "language": language},
                "audio": {"encoding": "pcm_s16le", "sample_rate_hz": 16000, "channels": 1}}))

            async def upload():
                nonlocal sent_bytes
                while chunk := audio.readframes(1600):
                    if paced:
                        await asyncio.sleep(max(0, ready + sent_bytes / 32000 - time.monotonic()))
                    await socket.send(chunk)
                    sent_bytes += len(chunk)
                    await asyncio.sleep(0)
                if paced:
                    await asyncio.sleep(max(0, ready + duration - time.monotonic()))
                finished.set()
                row["eos_seconds"] = time.monotonic() - ready
                await socket.send('{"type":"input.finish"}')

            try:
                async with asyncio.timeout(duration + 300 if paced else 600):
                    async for message in socket:
                        event = json.loads(message)
                        elapsed = time.monotonic() - started
                        row["events"].append({"elapsed_seconds": elapsed, "event": event})
                        kind = event.get("type")
                        if kind == "session.queued":
                            row["operation_id"] = event["operation_id"]
                        elif kind == "session.ready":
                            if producer is not None:
                                raise RuntimeError("duplicate_session_ready")
                            ready = time.monotonic()
                            row["ready_seconds"] = ready - started
                            print(json.dumps({"model": model, "event": "session.ready", "mode": row["mode"],
                                              "operation_id": row.get("operation_id")}), flush=True)
                            producer = asyncio.create_task(upload())
                        elif kind == "transcript.partial" and event.get("text"):
                            row.setdefault("first_partial_from_audio_start_seconds", time.monotonic()-ready)
                            row.setdefault("partial_before_eos", not finished.is_set())
                        elif kind == "session.error":
                            raise RuntimeError("public_live_" + event["code"])
                        elif kind == "session.completed":
                            if producer is None or not finished.is_set():
                                raise RuntimeError("premature_public_completion")
                            await producer
                            row["completed"] = event
                            row["wall_seconds"] = time.monotonic()-started
                            row["eos_to_completion_seconds"] = time.monotonic()-ready-row["eos_seconds"]
                            break
                if "completed" not in row:
                    raise RuntimeError("public_live_missing_completion")
            finally:
                if producer is not None:
                    producer.cancel()
                    await asyncio.gather(producer, return_exceptions=True)
    row["sent_audio_seconds"] = sent_bytes/32000
    async with httpx.AsyncClient(base_url=origin, headers={"authorization": "Bearer " + key},
                                timeout=30, trust_env=False) as client:
        response = await client.get(row["completed"]["result_path"])
        response.raise_for_status()
        row["result"] = response.json()
    finals = [item["event"] for item in row["events"] if item["event"]["type"] == "transcript.final"]
    row["matches_durable_result"] = "".join(item["text"] for item in finals).strip() == row["result"]["text"]
    if (row["sent_audio_seconds"] != duration or row["result"]["audio_seconds"] != duration
            or not row["matches_durable_result"] or not row.get("partial_before_eos") or not row["result"]["text"].strip()):
        raise RuntimeError("public_live_content_or_duration_mismatch")
    print(json.dumps({"model": model, "mode": row["mode"], "audio_seconds": duration,
        "first_partial_seconds": row["first_partial_from_audio_start_seconds"], "passed": True}), flush=True)


async def run_cohort(origin, key, assets, receipt, *, paced):
    cases = [
        ("nemotron-speech-en-0-6b", "ready/en/day1_consultation01_conversation.wav", "en"),
        ("nemotron-speech-multilingual-0-6b", "ready/de/hhu-herzrasen.wav", "de"),
    ]
    rows = [{} for _ in cases]
    receipt["measurements"] = rows
    results = await asyncio.gather(*(run_one(origin, key, model, assets / source, language, row, paced=paced)
        for (model, source, language), row in zip(cases, rows)), return_exceptions=True)
    failed = False
    for row, result in zip(rows, results):
        if isinstance(result, BaseException):
            # Public-only error: never includes a signed artifact URL or token.
            row["error"] = type(result).__name__ + ": " + str(result)
            failed = True
    if failed:
        raise RuntimeError("public_live_cohort_failed_see_retained_receipt")
