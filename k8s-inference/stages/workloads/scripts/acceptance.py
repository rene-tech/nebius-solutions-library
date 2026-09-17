"""Bounded authenticated edge smoke used by the optional Terraform Job."""

from __future__ import annotations

import asyncio
import json
import os
import ssl
import urllib.request
from urllib.parse import urlsplit

import httpx2
from mcp import Client
from mcp.client.streamable_http import streamable_http_client


base_url = os.environ["FS2_BASE_URL"].rstrip("/")
origin = os.environ["FS2_ORIGIN"].rstrip("/")
authority = urlsplit(origin).netloc
access_token = os.environ["FS2_ACCESS_TOKEN"]
context = ssl.create_default_context()


def request(
    path: str, *, token: str, body: dict[str, object] | None = None
) -> dict[str, object]:
    payload = None if body is None else json.dumps(body).encode("utf-8")
    headers = {
        "Authorization": f"Bearer {token}",
        "Host": authority,
        "Origin": origin,
    }
    if payload is not None:
        headers["Content-Type"] = "application/json"
    call = urllib.request.Request(base_url + path, data=payload, headers=headers)
    with urllib.request.urlopen(call, context=context, timeout=30) as response:
        if response.status != 200:
            raise RuntimeError(f"{path} returned HTTP {response.status}")
        return json.load(response)


if not access_token.startswith("fs2_pat_"):
    raise RuntimeError("acceptance access credential is not a scoped PAT")

models = request("/v1/models", token=access_token)
if not isinstance(models.get("data"), list) or not models["data"]:
    raise RuntimeError("authenticated model catalog is empty")


async def discover_tools() -> int:
    async with httpx2.AsyncClient(
        headers={
            "Authorization": f"Bearer {access_token}",
            "Origin": origin,
            "Host": authority,
        },
        follow_redirects=False,
        trust_env=False,
        verify=True,
    ) as transport:
        async with Client(
            streamable_http_client(f"{base_url}/mcp", http_client=transport),
            mode="2026-07-28",
        ) as session:
            if session.protocol_version != "2026-07-28":
                raise RuntimeError("MCP protocol negotiation failed")
            listed = await session.list_tools()
            if (
                listed.ttl_ms != 0
                or listed.cache_scope != "private"
                or not listed.tools
            ):
                raise RuntimeError("authenticated MCP tool discovery is invalid")
            return len(listed.tools)


tool_count = asyncio.run(discover_tools())

print(
    json.dumps({"models": len(models["data"]), "mcp_tools": tool_count}, sort_keys=True)
)
