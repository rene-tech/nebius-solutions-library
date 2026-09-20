#!/usr/bin/env python3
"""Direct-runtime semantic qualification for SAM 2.1 and Wan2.2 NIM."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fixtures import (
    sam_automatic_request,
    sam_requests,
    wan_i2v_requests,
    wan_t2v_requests,
)


def request(
    url: str, *, payload: dict[str, object] | None = None, timeout: int = 1200
) -> tuple[bytes, dict[str, str], float]:
    body = (
        None if payload is None else json.dumps(payload, separators=(",", ":")).encode()
    )
    call = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json"}
    )
    started = time.monotonic()
    try:
        with urllib.request.urlopen(call, timeout=timeout) as response:
            return (
                response.read(),
                {key.lower(): value for key, value in response.headers.items()},
                time.monotonic() - started,
            )
    except urllib.error.HTTPError as error:
        detail = error.read(4096).decode(errors="replace")
        raise RuntimeError(f"{url} returned HTTP {error.code}: {detail}") from error


def digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def probe_mp4(raw: bytes) -> dict[str, object]:
    with tempfile.NamedTemporaryFile(suffix=".mp4") as handle:
        handle.write(raw)
        handle.flush()
        output = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-show_entries",
                "stream=codec_name,width,height,avg_frame_rate,nb_frames:format=duration,format_name",
                "-of",
                "json",
                handle.name,
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
    value = json.loads(output.stdout)
    if len(value.get("streams", [])) != 1 or "mp4" not in value.get("format", {}).get(
        "format_name", ""
    ):
        raise RuntimeError("result is not a single-stream MP4")
    return value


def validate_sam(raw: bytes, expected_mode: str) -> dict[str, object]:
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        names = set(archive.namelist())
        manifest = json.loads(archive.read("manifest.json"))
        if manifest["mode"] != expected_mode or not manifest.get("checkpoint_sha256"):
            raise RuntimeError("SAM result manifest differs from the request")
        if expected_mode == "prompted-video":
            if "overlay.mp4" not in names or not any(
                name.startswith("masks/") for name in names
            ):
                raise RuntimeError("SAM video result lacks overlay or masks")
            probe = probe_mp4(archive.read("overlay.mp4"))
        else:
            if not {"mask.png", "overlay.png"}.issubset(names):
                raise RuntimeError("SAM image result lacks mask or overlay")
            probe = None
    return {"manifest": manifest, "files": sorted(names), "overlay_probe": probe}


def run_sam(url: str) -> dict[str, object]:
    ready_raw, _, _ = request(url.rstrip("/") + "/v1/health/ready")
    records: list[dict[str, object]] = []
    first_body = b""
    for fixture_id, payload in sam_requests():
        raw, headers, elapsed = request(
            url.rstrip("/") + "/v1/segment-track", payload=payload
        )
        detail = validate_sam(raw, str(payload["mode"]))
        records.append(
            {
                "fixture_id": fixture_id,
                "elapsed_seconds": elapsed,
                "bytes": len(raw),
                "sha256": digest(raw),
                "headers": headers,
                **detail,
            }
        )
        if not first_body:
            first_body = raw
            replay, _, _ = request(
                url.rstrip("/") + "/v1/segment-track", payload=payload
            )
            if replay != first_body:
                raise RuntimeError("SAM deterministic replay differed")
    payload = sam_automatic_request()
    automatic, headers, elapsed = request(
        url.rstrip("/") + "/v1/segment-track", payload=payload
    )
    records.append(
        {
            "fixture_id": "sam2-automatic-image",
            "elapsed_seconds": elapsed,
            "bytes": len(automatic),
            "sha256": digest(automatic),
            "headers": headers,
            **validate_sam(automatic, "automatic-image"),
        }
    )
    return {"ready": json.loads(ready_raw), "requests": records}


def run_wan(
    url: str, fixtures: list[tuple[str, dict[str, object]]], variant: str
) -> dict[str, object]:
    ready_raw, _, _ = request(url.rstrip("/") + "/v1/health/ready")
    records: list[dict[str, object]] = []
    for fixture_id, payload in fixtures:
        raw, headers, elapsed = request(
            url.rstrip("/") + "/v1/generate", payload=payload
        )
        probe = probe_mp4(raw)
        stream = probe["streams"][0]
        if (stream["width"], stream["height"]) != (832, 480):
            raise RuntimeError("Wan output dimensions differ from the semantic fixture")
        records.append(
            {
                "fixture_id": fixture_id,
                "elapsed_seconds": elapsed,
                "bytes": len(raw),
                "sha256": digest(raw),
                "headers": headers,
                "probe": probe,
            }
        )
    return {"variant": variant, "ready": json.loads(ready_raw), "requests": records}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sam-url")
    parser.add_argument("--wan-t2v-url")
    parser.add_argument("--wan-i2v-url")
    parser.add_argument("--evidence", type=Path, required=True)
    args = parser.parse_args()
    if not any((args.sam_url, args.wan_t2v_url, args.wan_i2v_url)):
        parser.error("at least one runtime URL is required")
    evidence: dict[str, Any] = {
        "schema": "fs2.nebius.ai/wan2-sam2-direct-qualification/v1",
        "observed_at": datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z"),
        "models": {},
    }
    if args.sam_url:
        evidence["models"]["sam2-1-hiera-large"] = run_sam(args.sam_url)
    if args.wan_t2v_url:
        evidence["models"]["wan2-2-t2v-nim"] = run_wan(
            args.wan_t2v_url, wan_t2v_requests(), "t2v"
        )
    if args.wan_i2v_url:
        evidence["models"]["wan2-2-i2v-nim"] = run_wan(
            args.wan_i2v_url, wan_i2v_requests(), "i2v"
        )
    args.evidence.parent.mkdir(parents=True, exist_ok=True)
    args.evidence.write_text(json.dumps(evidence, sort_keys=True, indent=2) + "\n")
    print(
        json.dumps(
            {
                "evidence": str(args.evidence),
                "sha256": digest(args.evidence.read_bytes()),
                "models": sorted(evidence["models"]),
            }
        )
    )


if __name__ == "__main__":
    main()
