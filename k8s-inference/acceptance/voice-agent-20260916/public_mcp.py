"""Ordinary typed MCP discovery/invoke/results against all three voice Apps."""

import asyncio
import hashlib
import io
import time
import wave
from pathlib import Path
from uuid import uuid4

import httpx
import httpx2
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

from public_smoke import MAGPIE, VOICE_MODELS


def data(result):
    if result.is_error or not isinstance(result.structured_content, dict):
        raise RuntimeError("voice_typed_mcp_failed")
    return result.structured_content


async def run(origin, key, fixture):
    receipt = {"measurements": []}
    content = Path(fixture).read_bytes()
    async with (
        httpx2.AsyncClient(
            headers={"authorization": "Bearer " + key, "origin": origin},
            verify=False,
            trust_env=False,
            timeout=120,
        ) as transport,
        httpx.AsyncClient(
            base_url=origin,
            headers={"authorization": "Bearer " + key},
            verify=False,
            trust_env=False,
            timeout=120,
        ) as http,
        Client(
            streamable_http_client(origin + "/mcp", http_client=transport),
            mode="2026-07-28",
        ) as client,
    ):
        tools = {tool.name: tool for tool in (await client.list_tools()).tools}
        for model in VOICE_MODELS:
            view = data(
                await client.call_tool(
                    "get_model_schema", {"model_id": model, "protocol": "native"}
                )
            )
            contract = next(c for c in view["contracts"] if c["protocol"] == "native")
            name = contract["tool_name"]
            assert name in tools
            assert tools[name].input_schema == contract["input_schema"]
            row = {"model": model, "tool": name, "schema_discovered": True}
            receipt["measurements"].append(row)
            if model == MAGPIE:
                payload = {
                    "text": "A complete synthetic recording from the typed agent tool.",
                    "voice": "Jason",
                    "language": "en",
                }
            else:
                reserved = await http.post(
                    "/v1/scientific-artifacts/uploads",
                    json={
                        "model_id": model,
                        "sha256": hashlib.sha256(content).hexdigest(),
                        "size_bytes": len(content),
                        "media_type": "audio/wav",
                        "compression": "none",
                    },
                    headers={"idempotency-key": "voice-mcp-upload-" + uuid4().hex},
                )
                reserved.raise_for_status()
                upload = reserved.json()
                assert len(content) <= upload["max_content_bytes"]
                written = await http.put(
                    upload["content_path"],
                    content=content,
                    headers={"content-type": "audio/wav"},
                )
                written.raise_for_status()
                finalized = await http.post(
                    "/v1/scientific-artifacts/uploads/"
                    + upload["upload_id"]
                    + ":finalize",
                    json={"operation_id": upload["operation_id"]},
                )
                finalized.raise_for_status()
                payload = {"audio": finalized.json()}
                row["input_sha256"] = hashlib.sha256(content).hexdigest()
            row["arguments"] = (
                payload  # Only synthetic text or opaque artifact identity.
            )
            started = time.monotonic()
            operation = data(
                await client.call_tool(
                    name,
                    payload
                    | {
                        "wait_seconds": 0,
                        "idempotency_key": "voice-mcp-" + uuid4().hex,
                    },
                )
            )
            row["operation_id"] = operation["id"]
            async with asyncio.timeout(180):
                while operation["status"] not in {
                    "succeeded",
                    "failed",
                    "cancelled",
                    "expired",
                    "preempted",
                }:
                    await asyncio.sleep(1)
                    operation = data(
                        await client.call_tool(
                            "get_operation", {"operation_id": operation["id"]}
                        )
                    )
            assert operation["status"] == "succeeded", operation["status"]
            result = data(
                await client.call_tool(
                    "get_operation_result", {"operation_id": operation["id"]}
                )
            )["result"]
            row.update(
                elapsed_seconds=time.monotonic() - started,
                status=operation["status"],
                result=result,
            )
            if model == MAGPIE:
                artifact = result["artifact"]
                audio = await http.get(
                    "/v1/artifacts/" + artifact["artifact_id"] + "/content"
                )
                audio.raise_for_status()
                assert hashlib.sha256(audio.content).hexdigest() == artifact["sha256"]
                with wave.open(io.BytesIO(audio.content)) as wav:
                    assert wav.getframerate() == 22050 and wav.getnframes() > 0
                    row["audio_seconds"] = wav.getnframes() / wav.getframerate()
                row["complete_wav_verified"] = True
            else:
                assert result["events"]
                if model.startswith("parakeet"):
                    assert result["text"].strip()
    receipt["session_closed"] = True
    return receipt
