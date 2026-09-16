"""Retain reproducible synthetic-only GPU voice benchmarks; no tokens in output."""

import argparse
import asyncio
import base64
import hashlib
import json
import math
import time
import wave
from pathlib import Path

import httpx
import numpy as np
from websockets.asyncio.client import connect

TEXT = "The workshop starts at nine. Please bring your notebook and meet us at the observatory."
MAGPIE = "magpie-tts-multilingual-357m"
PARAKEET = "parakeet-realtime-eou-120m-v1"
SORTFORMER = "diar-streaming-sortformer-4spk-v2-1"


async def synth(client, url, output, voice, text=TEXT, language="en"):
    payload = dict(
        model=MAGPIE,
        text=text,
        language=language,
        voice=voice,
        apply_text_normalization=False,
    )
    started, first, pcm, count, terminal = time.monotonic(), None, bytearray(), 0, None
    async with client.stream(
        "POST", url + "/v1/voice/synthesize", json=payload
    ) as response:
        response.raise_for_status()
        async for line in response.aiter_lines():
            event = json.loads(line)
            if event["type"] == "audio.chunk":
                assert event["sequence"] == count
                first = first or time.monotonic() - started
                pcm.extend(base64.b64decode(event["audio_base64"], validate=True))
                count += 1
            elif event["type"] == "audio.done":
                terminal = event
            elif event["type"] == "error":
                raise RuntimeError(event)
    elapsed = time.monotonic() - started
    assert terminal and terminal["samples"] == len(pcm) // 2 and pcm
    with wave.open(str(output), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(22050)
        handle.writeframes(pcm)
    return {
        "voice": voice,
        "language": language,
        "text": text,
        "first_audio_seconds": first,
        "elapsed_seconds": elapsed,
        "audio_seconds": len(pcm) / 44100,
        "rtf": elapsed / (len(pcm) / 44100),
        "chunks": count,
        "pcm_sha256": hashlib.sha256(pcm).hexdigest(),
        "wav": output.name,
    }


def read_pcm(path, *, noisy=False):
    with wave.open(str(path)) as handle:
        samples = (
            np.frombuffer(handle.readframes(handle.getnframes()), dtype="<i2").astype(
                np.float32
            )
            / 32768
        )
        rate = handle.getframerate()
    count = round(len(samples) * 16000 / rate)
    audio = np.interp(np.arange(count) * rate / 16000, np.arange(len(samples)), samples)
    if noisy:
        # Headset-like bandwidth plus deterministic 15 dB SNR broadband noise.
        audio = np.convolve(audio, np.array([0.2, 0.6, 0.2]), mode="same")
        rms = math.sqrt(float(np.mean(audio**2)))
        audio += np.random.default_rng(20260916).normal(
            0, rms / 10 ** (15 / 20), len(audio)
        )
    return (np.clip(audio, -1, 1) * 32767).astype("<i2").tobytes() + bytes(32000)


async def stream(url, model, pcm, realtime=False):
    started, events, first = time.monotonic(), [], None
    async with connect(
        url.replace("http:", "ws:") + "/v1/voice/stream", max_size=1048576, proxy=None
    ) as socket:
        await socket.send(json.dumps({"type": "session.start", "model": model}))
        ready = json.loads(await socket.recv())
        assert ready["type"] == "session.ready", ready

        async def send():
            for offset in range(0, len(pcm), 2560):
                await socket.send(pcm[offset : offset + 2560])
                if realtime:
                    await asyncio.sleep(0.08)
            await socket.send('{"type":"session.finish"}')

        sender = asyncio.create_task(send())
        try:
            while True:
                event = json.loads(await asyncio.wait_for(socket.recv(), 60))
                if event["type"] == "error":
                    raise RuntimeError(event)
                events.append(event)
                if event["type"] in {"transcript.partial", "speaker.activity"}:
                    first = first or time.monotonic() - started
                if event["type"] == "session.done":
                    break
            await sender
        finally:
            sender.cancel()
            await asyncio.gather(sender, return_exceptions=True)
    elapsed = time.monotonic() - started
    return {
        "model": model,
        "realtime_input": realtime,
        "first_output_seconds": first,
        "elapsed_seconds": elapsed,
        "audio_seconds": len(pcm) / 32000,
        "rtf": elapsed / (len(pcm) / 32000),
        "events": events,
    }


async def lifecycle(url, model):
    endpoint = url.replace("http:", "ws:") + "/v1/voice/stream"
    async with connect(endpoint, proxy=None) as active:
        await active.send(json.dumps({"type": "session.start", "model": model}))
        assert json.loads(await active.recv())["type"] == "session.ready"
        async with connect(endpoint, proxy=None) as second:
            await second.send(json.dumps({"type": "session.start", "model": model}))
            overloaded = json.loads(await second.recv())
            assert overloaded["code"] == "worker_busy", overloaded
        await active.send('{"type":"session.reset"}')
        reset = json.loads(await active.recv())
        assert reset["type"] == "session.reset", reset
        await active.send('{"type":"session.cancel"}')
        cancelled = json.loads(await active.recv())
        assert cancelled["type"] == "session.cancelled", cancelled
    reconnected = await stream(url, model, bytes(32000))
    return {
        "overload": overloaded,
        "reset": reset,
        "cancel": cancelled,
        "reconnect_completed": reconnected["events"][-1]["type"] == "session.done",
    }


async def main(args):
    root = Path(args.output)
    root.mkdir(parents=True, exist_ok=True)
    results = {}
    async with httpx.AsyncClient(timeout=240, trust_env=False) as client:
        for name, url in (
            ("magpie", args.magpie),
            ("parakeet", args.parakeet),
            ("sortformer", args.sortformer),
        ):
            if url:
                results[name + "_readiness"] = (
                    await client.get(url + "/readyz")
                ).json()
        if args.magpie:
            results["synthesis"] = []
            for voice in ("Aria", "Jason", "John", "Leo", "Sofia", "Sofia", "Sofia"):
                item = await synth(
                    client,
                    args.magpie,
                    root / f"{voice}-{len(results['synthesis'])}.wav",
                    voice,
                )
                results["synthesis"].append(item)
                print(json.dumps(item), flush=True)
            assert len({x["pcm_sha256"] for x in results["synthesis"][:5]}) == 5
            results["german"] = await synth(
                client,
                args.magpie,
                root / "de-Sofia.wav",
                "Sofia",
                "Guten Morgen. Der Workshop beginnt um neun Uhr.",
                "de",
            )
        fixture = root / "Sofia-4.wav"
        for name, url, model in (
            ("parakeet", args.parakeet, PARAKEET),
            ("sortformer", args.sortformer, SORTFORMER),
        ):
            if not url:
                continue
            results[name] = []
            for noisy in (False, False, False, True):
                item = await stream(url, model, read_pcm(fixture, noisy=noisy))
                item["noisy_headset_15db"] = noisy
                results[name].append(item)
                print(
                    json.dumps({k: v for k, v in item.items() if k != "events"}),
                    flush=True,
                )
            results[name + "_realtime"] = await stream(
                url, model, read_pcm(fixture), realtime=True
            )
            results[name + "_lifecycle"] = await lifecycle(url, model)
    (root / "results.json").write_text(json.dumps(results, indent=2) + "\n")
    print(json.dumps({"complete": True, "results": str(root / "results.json")}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--magpie")
    parser.add_argument("--parakeet")
    parser.add_argument("--sortformer")
    parser.add_argument("--output", required=True)
    asyncio.run(main(parser.parse_args()))
