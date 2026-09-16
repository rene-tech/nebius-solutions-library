"""Full medical audio through typed MCP discovery/invoke/status/result tools."""

import asyncio
import hashlib
import json
import subprocess
import tempfile
import time
import wave
from pathlib import Path
from uuid import uuid4

import httpx
import httpx2
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

from public_probe import IDS, upload_artifact


def result(value):
    if value.is_error or not isinstance(value.structured_content, dict):
        raise RuntimeError("typed_mcp_error_or_invalid_result")
    return value.structured_content


async def run_cohort(origin, key, assets, receipt):
    cases = [(IDS[0], "ready/en/day1_consultation01_conversation.wav", "en"),
             (IDS[1], "ready/de/hhu-herzrasen.wav", "de")]
    async with httpx2.AsyncClient(headers={"authorization": "Bearer " + key, "origin": origin},
                                 timeout=60, trust_env=False) as http:
        async with Client(streamable_http_client(origin + "/mcp", http_client=http), mode="2026-07-28") as client:
            listing = await client.list_tools()
            tools = {tool.name: tool.model_dump(mode="json", by_alias=True) for tool in listing.tools}
            names = ["infer_" + model.replace("-", "_") for model in IDS]
            if not all(name in tools for name in names):
                raise RuntimeError("typed_speech_tool_missing")
            receipt["tools"] = {name: tools[name] for name in names}
            for (model, relative, language), name in zip(cases, names):
                source = assets / relative
                with wave.open(str(source), "rb") as audio:
                    duration = audio.getnframes()/audio.getframerate()
                row = {"model": model, "input_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
                       "source_audio_seconds": duration, "tool": name}
                receipt["measurements"].append(row)
                started = time.monotonic()
                with tempfile.TemporaryDirectory(prefix="fs2-speech-mcp-") as directory:
                    path = Path(directory) / "recording.flac"
                    subprocess.run(["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-i", str(source),
                                    "-c:a", "flac", str(path)], check=True, timeout=90)
                    row.update(transport_sha256=hashlib.sha256(path.read_bytes()).hexdigest(), transport_bytes=path.stat().st_size)
                    with httpx.Client(base_url=origin, headers={"authorization": "Bearer " + key},
                                      timeout=60, trust_env=False) as upload_client:
                        artifact = upload_artifact(upload_client, path, model, row, "speech-mcp-" + uuid4().hex)
                schema = tools[name]["inputSchema"]
                internal_model = schema["properties"]["options"]["properties"]["model"]["const"]
                arguments = {"audio": artifact, "options": {"model": internal_model, "language": language},
                             "idempotency_key": "speech-mcp-" + uuid4().hex, "wait_seconds": 0}
                row["arguments"] = arguments  # Opaque artifact identity only, never audio or signed URL.
                operation = result(await client.call_tool(name, arguments))
                row["operation_id"] = operation["id"]
                deadline = time.monotonic()+600
                while operation["status"] not in {"succeeded", "failed", "cancelled", "expired", "preempted"}:
                    if time.monotonic() >= deadline:
                        raise RuntimeError("typed_speech_operation_timeout")
                    await asyncio.sleep(2)
                    operation = result(await client.call_tool("get_operation", {"operation_id": operation["id"]}))
                row["operation"] = operation
                if operation["status"] != "succeeded":
                    raise RuntimeError("typed_speech_operation_failed")
                row["result"] = result(await client.call_tool("get_operation_result", {"operation_id": operation["id"]}))["result"]
                row["wall_seconds"] = time.monotonic()-started
                if not row["result"]["text"].strip() or row["result"]["audio_seconds"] != duration:
                    raise RuntimeError("typed_speech_incomplete_recording")
                print(json.dumps({"model": model, "surface": "typed-mcp", "audio_seconds": duration,
                                  "wall_seconds": row["wall_seconds"], "passed": True}), flush=True)
