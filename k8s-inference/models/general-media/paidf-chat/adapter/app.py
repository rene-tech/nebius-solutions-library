"""Preserve vLLM's actual JSON/SSE response across durable platform transport."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
from contextlib import suppress
from contextlib import asynccontextmanager
from urllib.parse import urlsplit

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from jsonschema import Draft202012Validator

from contracts import MODELS, contract

MAX_REQUEST_BYTES = 192 * 1024**2
MAX_RESPONSE_BYTES = 16 * 1024**2


class BoundedRequest:
    def __init__(self, app):
        self.app = app
        self.lock = asyncio.Lock()

    async def __call__(self, scope, receive, send):
        if (
            scope["type"] != "http"
            or scope.get("path") != "/v1/reference-chat"
            or scope.get("method") != "POST"
        ):
            return await self.app(scope, receive, send)
        if self.lock.locked():
            return await JSONResponse({"detail": "runtime_busy"}, 429)(
                scope, receive, send
            )
        async with self.lock:
            body = bytearray()
            try:
                async with asyncio.timeout(60):
                    while True:
                        message = await receive()
                        if message["type"] == "http.disconnect":
                            return
                        body.extend(message.get("body", b""))
                        if len(body) > MAX_REQUEST_BYTES:
                            return await JSONResponse(
                                {"detail": "transport_byte_limit"}, 413
                            )(scope, receive, send)
                        if not message.get("more_body"):
                            break
            except TimeoutError:
                return await JSONResponse({"detail": "input_timeout"}, 408)(
                    scope, receive, send
                )
            pending = True

            async def replay():
                nonlocal pending
                if pending:
                    pending = False
                    return {
                        "type": "http.request",
                        "body": bytes(body),
                        "more_body": False,
                    }
                return await receive()

            async def disconnected():
                while True:
                    if (await receive())["type"] == "http.disconnect":
                        return

            worker = asyncio.create_task(self.app(scope, replay, send))
            monitor = asyncio.create_task(disconnected())
            try:
                completed, _ = await asyncio.wait({worker, monitor}, return_when=asyncio.FIRST_COMPLETED)
                if worker in completed:
                    return await worker
                # Closing the upstream HTTP stream propagates cancellation to
                # vLLM. Keep the single-flight lock until that close completes.
                worker.cancel()
                with suppress(asyncio.CancelledError):
                    await worker
            finally:
                for task in (worker, monitor):
                    if not task.done():
                        task.cancel()
                    with suppress(asyncio.CancelledError):
                        await task


@asynccontextmanager
async def lifespan(app):
    app_id = os.environ["REFERENCE_APP_ID"]
    model, revision = MODELS[app_id]
    upstream = os.environ.get("REFERENCE_VLLM_URL", "http://127.0.0.1:8001")
    parsed = urlsplit(upstream)
    if (
        parsed.scheme != "http"
        or parsed.hostname != "127.0.0.1"
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        raise RuntimeError(
            "Reference model must use the operator-configured loopback sidecar."
        )
    app.state.app_id, app.state.model, app.state.revision = app_id, model, revision
    app.state.validator = Draft202012Validator(contract(app_id))
    async with httpx.AsyncClient(
        base_url=upstream,
        timeout=httpx.Timeout(7100, connect=10),
        trust_env=False,
        follow_redirects=False,
    ) as client:
        app.state.upstream = client
        yield


app = FastAPI(title="Scientific AI reference chat transport", lifespan=lifespan)
app.add_middleware(BoundedRequest)


@app.get("/v1/health/live")
def live():
    return {"status": "live"}


@app.get("/v1/health/ready")
async def ready():
    try:
        response = await app.state.upstream.get("/v1/models", timeout=5)
        response.raise_for_status()
        if not any(
            item.get("id") == app.state.model for item in response.json()["data"]
        ):
            raise ValueError("Different model")
    except (httpx.HTTPError, KeyError, ValueError):
        raise HTTPException(503, "reference_model_not_ready") from None
    return {
        "status": "ready",
        "model": app.state.model,
        "model_revision": app.state.revision,
    }


def validate_response(raw, content_type, streaming, model):
    """Require complete original response; do not synthesize missing tokens."""
    if not streaming:
        if content_type != "application/json":
            raise ValueError("Wrong JSON response type")
        value = json.loads(raw)
        if (
            not isinstance(value, dict)
            or value.get("model") != model
            or not isinstance(value.get("choices"), list)
            or not value["choices"]
            or any(
                not isinstance(choice, dict)
                or choice.get("finish_reason") is None
                or not isinstance(choice.get("message"), dict)
                for choice in value["choices"]
            )
        ):
            raise ValueError("Wrong model or empty choices")
        return value.get("usage")
    if content_type != "text/event-stream":
        raise ValueError("Wrong streaming response type")
    done, finished, usage = False, False, None
    for event in raw.replace("\r\n", "\n").split("\n\n"):
        data = "\n".join(
            line[5:].lstrip() for line in event.splitlines() if line.startswith("data:")
        )
        if not data:
            continue
        if done:
            raise ValueError("Data after stream termination")
        if data == "[DONE]":
            done = True
            continue
        value = json.loads(data)
        if (
            not isinstance(value, dict)
            or value.get("model") != model
            or not isinstance(value.get("choices"), list)
            or any(not isinstance(choice, dict) for choice in value["choices"])
        ):
            raise ValueError("Invalid upstream stream event")
        finished |= any(
            choice.get("finish_reason") is not None for choice in value["choices"]
        )
        if value.get("usage") is not None:
            usage = value["usage"]
    if not done or not finished:
        raise ValueError("Incomplete upstream token stream")
    return usage


@app.post("/v1/reference-chat")
async def chat(request: Request):
    try:
        payload = await request.json()
        if not app.state.validator.is_valid(payload):
            raise ValueError("Invalid reference request")
    except (ValueError, TypeError, RecursionError):
        raise HTTPException(422, "invalid_reference_request") from None
    body = json.dumps(
        payload, allow_nan=False, ensure_ascii=False, separators=(",", ":")
    ).encode()
    try:
        async with app.state.upstream.stream(
            "POST",
            "/v1/chat/completions",
            content=body,
            headers={"content-type": "application/json"},
        ) as response:
            if not response.is_success:
                # No public model echo, request bytes, credentials, or raw traces.
                raise HTTPException(response.status_code, "reference_upstream_rejected")
            content_type = response.headers.get("content-type", "").split(";", 1)[0]
            content = bytearray()
            async for chunk in response.aiter_bytes():
                content.extend(chunk)
                if len(content) > MAX_RESPONSE_BYTES:
                    raise ValueError("Reference response exceeds transport limit")
        raw = content.decode("utf-8")
        usage = validate_response(
            raw, content_type, payload.get("stream", False), app.state.model
        )
    except httpx.HTTPError:
        raise HTTPException(502, "reference_upstream_transport_incomplete") from None
    except (ValueError, KeyError, TypeError):
        raise HTTPException(502, "reference_upstream_response_incomplete") from None
    return {
        "schema": "scientific-reference-chat/v1",
        "model": app.state.model,
        "model_revision": app.state.revision,
        "stream": payload.get("stream", False),
        "content_type": content_type,
        "response_body": raw,
        "response_sha256": hashlib.sha256(content).hexdigest(),
        "request_sha256": hashlib.sha256(body).hexdigest(),
        "usage": usage,
    }
