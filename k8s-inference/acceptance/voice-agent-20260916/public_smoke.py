"""Ordinary public grants, complete WAV and sibling ASR regression; revoke test keys."""

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
from uuid import uuid4

import httpx

ASR = ["nemotron-speech-en-0-6b", "nemotron-speech-multilingual-0-6b"]
MAGPIE = "magpie-tts-multilingual-357m"
VOICE_MODELS = [
    MAGPIE,
    "parakeet-realtime-eou-120m-v1",
    "diar-streaming-sortformer-4spk-v2-1",
]


def main(args):
    kubectl = ["kubectl", "--kubeconfig", args.kubeconfig, "--context", args.context]
    raw = json.loads(
        subprocess.check_output(
            kubectl
            + ["-n", "fs2-system", "get", "secret", "fs2-serve-admin", "-o", "json"]
        )
    )
    admin_token = base64.b64decode(raw["data"]["token"]).decode().strip()
    receipt = {
        "scope": "public synthetic voice acceptance",
        "measurements": [],
        "key_revoked": False,
    }
    key_id = None
    with httpx.Client(
        base_url=args.origin,
        headers={"origin": args.origin},
        verify=False,
        timeout=240,
        trust_env=False,
    ) as admin:
        try:
            admin.post(
                "/admin/api/v1/session",
                headers={"authorization": "Bearer " + admin_token},
            ).raise_for_status()
            response = admin.post(
                "/admin/api/v1/keys",
                json={
                    "name": "voice-acceptance-" + uuid4().hex[:10],
                    "tenant_id": "rene",
                    "principal_id": "rene",
                    "models": ASR + (VOICE_MODELS if args.voice else []),
                    "scopes": [
                        "catalog.read",
                        "mcp.invoke",
                        "inference.invoke",
                        "operations.read",
                        "operations.result",
                        "operations.cancel",
                        "artifacts.write",
                        "use.nonclinical",
                    ],
                    "max_concurrency": 2,
                    "expires_at": (
                        datetime.now(UTC) + timedelta(minutes=45)
                    ).isoformat(),
                },
            )
            response.raise_for_status()
            disclosure = response.json()["data"]
            key_id, key = disclosure["key"]["id"], disclosure["secret"]
            with httpx.Client(
                base_url=args.origin,
                headers={"authorization": "Bearer " + key},
                verify=False,
                timeout=240,
                trust_env=False,
            ) as client:
                models = client.get("/v1/models")
                models.raise_for_status()
                receipt["catalog_has_sibling_models"] = all(
                    m in models.text for m in ASR
                )
                assert receipt["catalog_has_sibling_models"]
                for model, source, language in (
                    (ASR[0], args.english, "en"),
                    (ASR[1], args.german, "de"),
                ):
                    path = Path(source)
                    started = time.monotonic()
                    response = client.post(
                        "/v1/audio/transcriptions",
                        data={
                            "model": model,
                            "language": language,
                            "response_format": "verbose_json",
                        },
                        files={
                            "file": ("synthetic.wav", path.read_bytes(), "audio/wav")
                        },
                        headers={"idempotency-key": "voice-sibling-" + uuid4().hex},
                    )
                    response.raise_for_status()
                    result = response.json()
                    assert result.get("text", "").strip(), result
                    receipt["measurements"].append(
                        {
                            "model": model,
                            "status": response.status_code,
                            "elapsed_seconds": time.monotonic() - started,
                            "input_sha256": hashlib.sha256(
                                path.read_bytes()
                            ).hexdigest(),
                            "result": result,
                            "operation_id": response.headers.get("x-fs2-operation-id"),
                        }
                    )
                if args.voice:
                    pcm, done, first = bytearray(), None, None
                    started = time.monotonic()
                    with client.stream(
                        "POST",
                        "/v1/voice/synthesize",
                        json={
                            "model": MAGPIE,
                            "text": "The public workshop audio is preserved.",
                            "voice": "Jason",
                            "language": "en",
                        },
                    ) as stream:
                        stream.raise_for_status()
                        for line in stream.iter_lines():
                            event = json.loads(line)
                            if event["type"] == "audio.chunk":
                                first = first or time.monotonic() - started
                                pcm.extend(
                                    base64.b64decode(
                                        event["audio_base64"], validate=True
                                    )
                                )
                            elif event["type"] == "audio.done":
                                done = event
                            elif event["type"] == "error":
                                raise RuntimeError(event["code"])
                    assert pcm and done
                    result = client.get(done["result_path"])
                    result.raise_for_status()
                    envelope = result.json()
                    artifact = envelope["artifact"]
                    response = client.get(
                        "/v1/artifacts/" + artifact["artifact_id"] + "/content"
                    )
                    response.raise_for_status()
                    assert (
                        hashlib.sha256(response.content).hexdigest()
                        == artifact["sha256"]
                    )
                    with wave.open(io.BytesIO(response.content)) as audio:
                        assert (
                            audio.getframerate() == 22050
                            and audio.readframes(audio.getnframes()) == pcm
                        )
                    Path(args.output).with_suffix(".wav").write_bytes(response.content)
                    receipt["synthesis"] = {
                        "first_audio_seconds": first,
                        "audio_seconds": len(pcm) / 44100,
                        "done": done,
                        "artifact": artifact,
                        "stream_matches_durable_wav": True,
                    }
                    from public_stream import run

                    receipt["voice_streams"] = asyncio.run(
                        run(args.origin, key, args.english)
                    )
                    if args.mcp:
                        from public_mcp import run as mcp_run

                        receipt["typed_mcp"] = asyncio.run(
                            mcp_run(args.origin, key, args.english)
                        )
                else:
                    denied = client.post(
                        "/v1/voice/synthesize",
                        json={"model": MAGPIE, "text": "Not granted."},
                    )
                    receipt["ungranted_voice_status"] = denied.status_code
            receipt["status"] = "passed"
        finally:
            if key_id:
                receipt["key_revoked"] = (
                    admin.delete("/admin/api/v1/keys/" + key_id).status_code == 200
                )
            admin.delete("/admin/api/v1/session")
            Path(args.output).write_text(
                json.dumps(receipt, indent=2, ensure_ascii=False) + "\n"
            )
    print(
        json.dumps(
            {
                "status": receipt.get("status"),
                "key_revoked": receipt["key_revoked"],
                "models": ASR,
                "voice": args.voice,
            }
        )
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    for key in ("kubeconfig", "context", "origin", "english", "german", "output"):
        parser.add_argument("--" + key, required=True)
    parser.add_argument("--voice", action="store_true")
    parser.add_argument("--mcp", action="store_true")
    main(parser.parse_args())
