#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import io
import json
import time
import urllib.error
import urllib.request
import wave
from datetime import UTC, datetime
from pathlib import Path


REQUESTS = (
    {
        "prompt": "Instrumental cinematic electronic music for a scientific product demo, precise, optimistic, no vocals",
        "lyrics": "[Instrumental]",
        "duration_seconds": 12,
        "thinking": True,
        "seed": 7,
    },
    {
        "prompt": "Colorful molecular discovery montage, restrained percussion, warm synth pulse, polished technology launch music",
        "lyrics": "[Instrumental]",
        "duration_seconds": 12,
        "thinking": True,
        "seed": 19,
    },
)


def canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def post(url: str, payload: dict[str, object], timeout: float) -> tuple[int, str, bytes]:
    request = urllib.request.Request(
        url,
        data=canonical(payload),
        headers={"content-type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.headers.get_content_type(), response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.headers.get_content_type(), exc.read()


def inspect_wav(body: bytes) -> dict[str, object]:
    with wave.open(io.BytesIO(body), "rb") as audio:
        frames = audio.getnframes()
        rate = audio.getframerate()
        assert audio.getcomptype() == "NONE"
        assert 1 <= audio.getnchannels() <= 2
        assert audio.getsampwidth() in {2, 3, 4}
        assert frames > 0 and 8000 <= rate <= 192000
        decoded = audio.readframes(frames + 1)
        assert len(decoded) == frames * audio.getnchannels() * audio.getsampwidth()
        return {
            "channels": audio.getnchannels(),
            "sample_width_bytes": audio.getsampwidth(),
            "sample_rate_hz": rate,
            "frames": frames,
            "duration_seconds": frames / rate,
        }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    endpoint = args.base_url.rstrip("/") + "/generate"

    evidence: dict[str, object] = {
        "schema": "fs2-serve.nebius.ai/ace-step-qualification/v1",
        "captured_at": datetime.now(UTC).isoformat(),
        "model_id": "ace-step-1-5",
        "source_revision": "19671f406d603126926c1b7e2adc169acbcade22",
        "lm_revision": "0a3ec94b557aea7d508da38b31cfe7341f6ff737",
        "requests": [],
    }
    bodies: list[bytes] = []
    for payload in REQUESTS:
        started = time.monotonic()
        status, media_type, body = post(endpoint, payload, 960)
        assert status == 200 and media_type == "audio/wav"
        assert body[:4] == b"RIFF" and int.from_bytes(body[4:8], "little") + 8 == len(body)
        bodies.append(body)
        evidence["requests"].append({
            "payload_sha256": hashlib.sha256(canonical(payload)).hexdigest(),
            "response_sha256": hashlib.sha256(body).hexdigest(),
            "response_bytes": len(body),
            "elapsed_seconds": time.monotonic() - started,
            "wav": inspect_wav(body),
        })

    assert hashlib.sha256(bodies[0]).digest() != hashlib.sha256(bodies[1]).digest()
    status, media_type, rejected = post(endpoint, {"prompt": "x", "duration_seconds": 61}, 30)
    assert status == 422 and media_type == "application/json"
    evidence["invalid_request"] = {
        "status": status,
        "response_sha256": hashlib.sha256(rejected).hexdigest(),
    }
    evidence["distinct_responses"] = True
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(evidence, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({
        "output": str(args.output),
        "sha256": hashlib.sha256(args.output.read_bytes()).hexdigest(),
        "response_sha256": [hashlib.sha256(body).hexdigest() for body in bodies],
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
