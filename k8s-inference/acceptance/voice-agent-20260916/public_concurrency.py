"""Two ordinary public callers; require concurrent output on two managed workers.

Normally run after an App has two ready replicas. With --scale-from-one,
raise only that App's warm floor after first output on an established stream.
The scoped short-lived key is revoked even on failure; no cloud limits change.
"""

import argparse
import asyncio
import base64
import hashlib
import io
import json
import subprocess
import time
import wave
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import httpx

from public_smoke import MAGPIE, VOICE_MODELS
from public_stream import run


async def synthesize(client, voice, on_first_output=None):
    started = time.monotonic()
    row = {"voice": voice, "started_monotonic": started}
    pcm = bytearray()
    async with client.stream(
        "POST",
        "/v1/voice/synthesize",
        json={
            "model": MAGPIE,
            "voice": voice,
            "language": "en",
            "text": (
                "Two independent workshop participants can speak at the same time. "
                "Each voice keeps a separate complete recording for the conversation. "
                "The listener can continue while the other participant is still speaking."
            ),
        },
    ) as response:
        response.raise_for_status()
        async for line in response.aiter_lines():
            event = json.loads(line)
            if event["type"] == "audio.chunk":
                first_output = "first_audio_monotonic" not in row
                row.setdefault("first_audio_monotonic", time.monotonic())
                pcm.extend(base64.b64decode(event["audio_base64"], validate=True))
                if first_output and on_first_output is not None:
                    await on_first_output()
            elif event["type"] == "audio.done":
                row["done"] = event
                row["done_monotonic"] = time.monotonic()
            elif event["type"] == "error":
                raise RuntimeError(event["code"])
    assert pcm and row.get("done")
    result = await client.get(row["done"]["result_path"])
    result.raise_for_status()
    artifact = result.json()["artifact"]
    audio = await client.get("/v1/artifacts/" + artifact["artifact_id"] + "/content")
    audio.raise_for_status()
    assert hashlib.sha256(audio.content).hexdigest() == artifact["sha256"]
    with wave.open(io.BytesIO(audio.content)) as wav:
        assert wav.getframerate() == 22050
        assert wav.readframes(wav.getnframes()) == pcm
    row.update(
        first_audio_seconds=row["first_audio_monotonic"] - started,
        elapsed_seconds=row["done_monotonic"] - started,
        audio_seconds=len(pcm) / 44100,
        artifact=artifact,
        stream_matches_durable_wav=True,
    )
    return row


async def measure(args, key):
    if args.scale_from_one:
        from manage_apps import main as manage_app

        scale_path = str(Path(args.output).with_suffix(".scale.json"))
        timing = {}

        async def scale():
            timing["scale_started_monotonic"] = time.monotonic()
            await asyncio.to_thread(manage_app, SimpleNamespace(
                kubeconfig=args.kubeconfig, context=args.context, origin=args.origin,
                mode="scale", model=args.model, min_replicas=2, output=scale_path,
            ))
            timing["scale_applied_monotonic"] = time.monotonic()

        if args.model == MAGPIE:
            async with httpx.AsyncClient(
                base_url=args.origin, headers={"authorization": "Bearer " + key},
                verify=False, timeout=240, trust_env=False,
            ) as client:
                rows = [await synthesize(client, "Sofia", scale)]
        else:
            rows = await run(args.origin, key, args.fixture, [args.model],
                             on_first_output=scale)
        assert timing.get("scale_applied_monotonic")
        timing["existing_session_completed_monotonic"] = time.monotonic()
        return {
            "scale_applied_during_established_session": True,
            "scale": json.loads(Path(scale_path).read_text()),
            "timing": timing, "measurements": rows,
        }
    if args.model != MAGPIE:
        barrier = asyncio.Barrier(2)
        async with asyncio.timeout(180):
            values = await asyncio.gather(
                *(
                    run(args.origin, key, args.fixture, [args.model], barrier)
                    for _ in range(2)
                )
            )
        rows = [value[0] for value in values]
        assert len({row["backend_id"] for row in rows}) == 2
        return {"workers_distinct": True, "measurements": rows}
    async with httpx.AsyncClient(
        base_url=args.origin,
        headers={"authorization": "Bearer " + key},
        verify=False,
        timeout=240,
        trust_env=False,
    ) as client:
        rows = await asyncio.gather(
            synthesize(client, "Sofia"), synthesize(client, "Jason")
        )
    # One runtime serializes GPU work until its full output stream ends. Both
    # first outputs before either completion therefore prove resident overlap.
    overlap = min(row["done_monotonic"] for row in rows) - max(
        row["first_audio_monotonic"] for row in rows
    )
    assert overlap > 0, "requests completed but did not execute concurrently"
    return {"overlap_seconds": overlap, "measurements": rows}


def main(args):
    command = ["kubectl", "--kubeconfig", args.kubeconfig, "--context", args.context]
    raw = json.loads(subprocess.check_output(command + [
        "-n", "fs2-system", "get", "secret", "fs2-serve-admin", "-o", "json"
    ]))
    token = base64.b64decode(raw["data"]["token"]).decode().strip()
    receipt = {"model": args.model, "key_revoked": False}
    key_id = None
    with httpx.Client(
        base_url=args.origin, headers={"origin": args.origin}, verify=False,
        timeout=120, trust_env=False,
    ) as admin:
        try:
            admin.post("/admin/api/v1/session", headers={
                "authorization": "Bearer " + token
            }).raise_for_status()
            response = admin.post("/admin/api/v1/keys", json={
                "name": "voice-concurrency-" + uuid4().hex[:10],
                "tenant_id": "rene", "principal_id": "rene", "models": [args.model],
                "scopes": ["inference.invoke", "operations.read", "operations.result", "use.nonclinical"],
                "max_concurrency": 2,
                "expires_at": (datetime.now(UTC) + timedelta(minutes=15)).isoformat(),
            })
            response.raise_for_status()
            value = response.json()["data"]
            key_id = value["key"]["id"]
            receipt.update(asyncio.run(measure(args, value["secret"])))
            receipt["status"] = "passed"
        finally:
            if key_id:
                receipt["key_revoked"] = admin.delete(
                    "/admin/api/v1/keys/" + key_id
                ).status_code == 200
            admin.delete("/admin/api/v1/session")
            Path(args.output).write_text(json.dumps(receipt, indent=2) + "\n")
    print(json.dumps({"model": args.model, "status": receipt.get("status"),
                      "key_revoked": receipt["key_revoked"]}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("kubeconfig", "context", "origin", "output"):
        parser.add_argument("--" + name, required=True)
    parser.add_argument("--model", choices=VOICE_MODELS, required=True)
    parser.add_argument("--fixture")
    parser.add_argument("--scale-from-one", action="store_true",
                        help="Raise this App to min=2 after its first output; needs admin authority")
    main(parser.parse_args())
