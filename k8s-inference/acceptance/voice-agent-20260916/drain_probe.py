"""Drain only the explicitly supplied disposable qualification worker."""

import argparse
import asyncio
import json
import time
from pathlib import Path

import httpx
from websockets.asyncio.client import connect

from probe import MAGPIE, PARAKEET, read_pcm


async def main(args):
    results = {}
    async with httpx.AsyncClient(timeout=60, trust_env=False) as client:
        payload = {
            "model": MAGPIE,
            "text": "The workshop begins at nine. " * 60,
            "voice": "Sofia",
        }
        async with client.stream(
            "POST", args.magpie + "/v1/voice/synthesize", json=payload
        ) as response:
            assert response.status_code == 200
            async for line in response.aiter_lines():
                event = json.loads(line)
                if event["type"] == "audio.chunk":
                    busy = await client.post(
                        args.magpie + "/v1/voice/synthesize", json={"text": "Hello"}
                    )
                    assert busy.status_code == 429
                    results["tts_overload_status"] = busy.status_code
                    break
        started = time.monotonic()
        while True:
            ready = (await client.get(args.magpie + "/readyz")).json()
            if not ready["busy"]:
                break
            if time.monotonic() - started > 15:
                raise TimeoutError("tts_cancellation_not_released")
            await asyncio.sleep(0.1)
        results["tts_disconnect_to_idle_seconds"] = time.monotonic() - started
        reconnect = await client.post(
            args.magpie + "/generate", json={"text": "The new request completed."}
        )
        assert reconnect.status_code == 200 and reconnect.content.startswith(b"RIFF")
        results["tts_reconnected_wav_bytes"] = len(reconnect.content)
        endpoint = args.parakeet.replace("http:", "ws:") + "/v1/voice/stream"
        async with connect(endpoint, proxy=None) as active:
            await active.send(json.dumps({"type": "session.start", "model": PARAKEET}))
            assert json.loads(await active.recv())["type"] == "session.ready"
            pcm = read_pcm(Path(args.fixture))
            await active.send(pcm[:2560])
            response = await client.post(args.parakeet + "/drain")
            assert response.json() == {"draining": True, "active": 1}
            async with connect(endpoint, proxy=None) as rejected:
                await rejected.send(
                    json.dumps({"type": "session.start", "model": PARAKEET})
                )
                event = json.loads(await rejected.recv())
                assert event["code"] == "worker_draining"
                results["new_session_rejected"] = event

            async def finish():
                for offset in range(2560, len(pcm), 2560):
                    await active.send(pcm[offset : offset + 2560])
                await active.send('{"type":"session.finish"}')

            sender = asyncio.create_task(finish())
            events = []
            async for raw in active:
                event = json.loads(raw)
                events.append(event)
                if event["type"] == "session.done":
                    break
                assert event["type"] != "error", event
            await sender
            assert events[-1]["type"] == "session.done"
            results["admitted_session_completed"] = True
            results["final_text"] = " ".join(
                e["text"] for e in events if e["type"] == "transcript.final"
            )
        results["drained_readiness_status"] = (
            await client.get(args.parakeet + "/readyz")
        ).status_code
        assert results["drained_readiness_status"] == 503
    Path(args.output).write_text(json.dumps(results, indent=2) + "\n")
    print(json.dumps(results))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    for key in ("magpie", "parakeet", "fixture", "output"):
        parser.add_argument("--" + key, required=True)
    asyncio.run(main(parser.parse_args()))
