"""Use the public connection-owning operation path with real paced audio."""

import asyncio
import json
import ssl
import subprocess
import time

import httpx
from websockets.asyncio.client import connect

PARAKEET = "parakeet-realtime-eou-120m-v1"
SORTFORMER = "diar-streaming-sortformer-4spk-v2-1"


async def run(origin, key, fixture):
    pcm = subprocess.check_output(
        [
            "ffmpeg",
            "-v",
            "error",
            "-i",
            str(fixture),
            "-ac",
            "1",
            "-ar",
            "16000",
            "-f",
            "s16le",
            "pipe:1",
        ]
    )
    pcm += bytes(32000)
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    results = []
    for model in (PARAKEET, SORTFORMER):
        row = {"model": model, "events": []}
        started, producer, ready_at = time.monotonic(), None, None
        finished = asyncio.Event()
        async with connect(
            origin.replace("https://", "wss://") + "/v1/voice/stream",
            ssl=context,
            additional_headers={"authorization": "Bearer " + key},
            proxy=None,
            open_timeout=30,
            max_size=1048576,
            max_queue=2,
        ) as socket:
            await socket.send(json.dumps({"type": "session.start", "model": model}))

            async def upload():
                for offset in range(0, len(pcm), 2560):
                    await asyncio.sleep(
                        max(0, ready_at + offset / 32000 - time.monotonic())
                    )
                    await socket.send(pcm[offset : offset + 2560])
                await asyncio.sleep(
                    max(0, ready_at + len(pcm) / 32000 - time.monotonic())
                )
                finished.set()
                row["end_of_input_seconds"] = time.monotonic() - ready_at
                await socket.send('{"type":"session.finish"}')

            try:
                async with asyncio.timeout(120):
                    async for raw in socket:
                        event = json.loads(raw)
                        row["events"].append(event)
                        kind = event.get("type")
                        if kind == "session.ready":
                            assert producer is None
                            ready_at = time.monotonic()
                            row["ready_seconds"] = ready_at - started
                            producer = asyncio.create_task(upload())
                        elif kind in {"transcript.partial", "speaker.activity"}:
                            row.setdefault(
                                "first_output_from_ready_seconds",
                                time.monotonic() - ready_at,
                            )
                            row.setdefault(
                                "output_before_end_of_input", not finished.is_set()
                            )
                        elif kind == "error":
                            raise RuntimeError("public_voice_" + event["code"])
                        elif kind == "session.done":
                            assert producer and finished.is_set()
                            await producer
                            row["completed"] = event
                            row["finalization_seconds"] = (
                                time.monotonic()
                                - ready_at
                                - row["end_of_input_seconds"]
                            )
                            break
                assert row.get("completed") and row.get("output_before_end_of_input")
            finally:
                if producer:
                    producer.cancel()
                    await asyncio.gather(producer, return_exceptions=True)
        async with httpx.AsyncClient(
            base_url=origin,
            verify=False,
            trust_env=False,
            timeout=60,
            headers={"authorization": "Bearer " + key},
        ) as client:
            response = await client.get(row["completed"]["result_path"])
            response.raise_for_status()
            value = response.json()
            if (
                value.get("schema")
                == "fs2-serve.nebius.ai/operation-artifact-result/v1"
            ):
                response = await client.get(
                    "/v1/artifacts/" + value["artifact"]["artifact_id"] + "/content"
                )
                response.raise_for_status()
                value = response.json()
            row["durable_result"] = value
        if model == PARAKEET:
            assert value["text"].strip()
            assert any(e["type"] == "turn.eou" for e in row["events"])
        else:
            assert value["events"]
        row["elapsed_seconds"] = time.monotonic() - started
        results.append(row)
    return results
