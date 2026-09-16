"""Attendee API and browser UI for durable text/spoken MindEval runs."""

import asyncio
import contextlib
import json
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import UUID

import asyncpg
import httpx
import uvicorn
from fastapi import FastAPI, Header, HTTPException, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.encoders import jsonable_encoder
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from prometheus_client import Counter, Gauge, generate_latest
from websockets.asyncio.client import connect

from .models import CreateRuns, Intervention, Settings
from .store import LiveAudioBus, Store, public_run
from .worker import RemoteFailure, Worker, json_call, wav_bytes

REQUESTS = Counter("fs2_workshop_requests_total", "Workshop API requests", ["method", "status"])
QUEUED = Gauge("fs2_workshop_runs", "Durable workshop runs", ["status"])


class Identity:
    def __init__(self, client, url):
        self.client, self.url = client, url

    async def verify(self, authorization):
        if not authorization or not authorization.startswith("Bearer ") or len(authorization) > 4096:
            raise HTTPException(401, "A Scientific AI API key is required")
        token = authorization[7:]
        try:
            response = await self.client.get(self.url, headers={"Authorization": authorization}, timeout=10)
        except httpx.HTTPError as exc:
            raise HTTPException(503, "Platform authentication is temporarily unavailable") from exc
        if response.status_code != 200:
            raise HTTPException(401 if response.status_code in {401, 403} else 503, "Platform authentication failed")
        try:
            identity = {
                name: response.headers[header]
                for name, header in {
                    "tenant_id": "x-fs2-tenant",
                    "principal_id": "x-fs2-principal",
                    "token_id": "x-fs2-token-id",
                }.items()
            }
            scopes = json.loads(response.headers["x-fs2-scopes"])
            models = json.loads(response.headers["x-fs2-models"])
        except (KeyError, ValueError) as exc:
            raise HTTPException(503, "Platform authentication contract is unavailable") from exc
        if "inference.invoke" not in scopes or not ({"*", "mindeval"} & set(models)):
            raise HTTPException(403, "This API key does not include the MindEval workshop")
        identity["max_concurrency"] = int(response.headers.get("x-fs2-max-concurrency", "1"))
        return identity, token


def create_app(settings=None, *, store=None, client=None, start_workers=True):
    settings = settings or Settings()

    @asynccontextmanager
    async def lifespan(app):
        nonlocal store, client
        owns_client, owns_store = client is None, store is None
        client = client or httpx.AsyncClient(timeout=settings.request_timeout_seconds, trust_env=False)
        if store is None:
            pool = await asyncpg.create_pool(
                settings.database_url,
                min_size=2,
                max_size=min(settings.workers + 5, 12),
                server_settings={"application_name": "fs2-workshop"},
            )
            store = Store(pool, Path(settings.credential_key_file).read_bytes().strip())
        app.state.store, app.state.client = store, client
        app.state.auth = Identity(client, settings.auth_url)
        app.state.audio_bus = LiveAudioBus(store.pool)
        await app.state.audio_bus.start()
        workers = [Worker(store, settings, client) for _ in range(settings.workers)] if start_workers else []
        tasks = [asyncio.create_task(w.loop()) for w in workers]
        try:
            yield
        finally:
            for worker in workers:
                worker.stopping = True
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await app.state.audio_bus.close()
            if owns_client:
                await client.aclose()
            if owns_store:
                await store.pool.close()

    app = FastAPI(
        title="Scientific AI MindEval Workshop", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan
    )

    @app.middleware("http")
    async def transport(request: Request, call_next):
        if request.headers.get("content-length", "").isdigit() and int(request.headers["content-length"]) > 256 * 1024:
            return JSONResponse({"detail": "Request exceeds 256 KiB"}, status_code=413)
        if request.headers.get("origin") not in {None, settings.public_origin}:
            return JSONResponse({"detail": "Origin not allowed"}, status_code=403)
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        REQUESTS.labels(request.method, str(response.status_code)).inc()
        return response

    @app.exception_handler(KeyError)
    async def missing(_request, _exc):
        return JSONResponse({"detail": "Run not found"}, status_code=404)

    @app.exception_handler(ValueError)
    async def conflict(_request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=409)

    @app.exception_handler(OverflowError)
    async def full(_request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=429, headers={"Retry-After": "5"})

    @app.exception_handler(RemoteFailure)
    async def remote(_request, exc):
        return JSONResponse({"detail": exc.message, "code": exc.code}, status_code=502)

    @app.get("/healthz")
    async def health():
        return {"ok": True}

    @app.get("/readyz")
    async def ready():
        await app.state.store.pool.fetchval("SELECT 1 FROM fs2_workshop.runs LIMIT 1")
        return {"ok": True}

    @app.get("/metrics")
    async def metrics():
        rows = await app.state.store.pool.fetch("SELECT status,count(*) AS n FROM fs2_workshop.runs GROUP BY status")
        for row in rows:
            QUEUED.labels(row["status"]).set(row["n"])
        return Response(generate_latest(), media_type="text/plain; version=0.0.4")

    @app.get("/v1/workshop/catalog")
    async def catalog(authorization: str | None = Header(default=None)):
        identity, token = await app.state.auth.verify(authorization)
        root = settings.gateway_url.rstrip("/") + "/v1/mindeval"
        catalog, profiles = await asyncio.gather(
            json_call(app.state.client, "GET", root + "/catalog", token),
            json_call(app.state.client, "GET", root + "/profiles", token),
        )
        return {
            "catalog": catalog,
            "profiles": profiles,
            "identity": identity,
            "limits": {"profiles": 20, "workers_per_team": min(5, identity["max_concurrency"])},
            "modes": ["canonical", "spoken"],
        }

    @app.post("/v1/workshop/runs", status_code=202)
    async def create(
        request: CreateRuns,
        authorization: str | None = Header(default=None),
        idempotency_key: str | None = Header(default=None),
    ):
        identity, token = await app.state.auth.verify(authorization)
        if not idempotency_key or not 1 <= len(idempotency_key) <= 200:
            raise HTTPException(400, "Supply an Idempotency-Key header, at most 200 characters")
        rows = await app.state.store.create(identity, token, request, idempotency_key)
        return {"data": rows}

    @app.get("/v1/workshop/runs")
    async def list_runs(authorization: str | None = Header(default=None)):
        identity, _ = await app.state.auth.verify(authorization)
        return {"data": await app.state.store.list(identity)}

    @app.get("/v1/workshop/runs/{run_id}")
    async def run(run_id: UUID, authorization: str | None = Header(default=None)):
        identity, _ = await app.state.auth.verify(authorization)
        return public_run(await app.state.store.get(run_id, identity))

    @app.post("/v1/workshop/runs/{run_id}/interventions")
    async def intervene(run_id: UUID, command: Intervention, authorization: str | None = Header(default=None)):
        identity, _ = await app.state.auth.verify(authorization)
        return await app.state.store.intervene(run_id, identity, command)

    @app.get("/v1/workshop/runs/{run_id}/events")
    async def events(run_id: UUID, after: int = 0, authorization: str | None = Header(default=None)):
        identity, _ = await app.state.auth.verify(authorization)
        await app.state.store.get(run_id, identity)
        rows = await app.state.store.pool.fetch(
            "SELECT id,kind,data,created_at FROM fs2_workshop.events WHERE run_id=$1 AND id>$2 ORDER BY id LIMIT 500",
            run_id,
            after,
        )
        return {"data": [{**dict(r), "data": json.loads(r["data"])} for r in rows]}

    @app.get("/v1/workshop/runs/{run_id}/report")
    async def report(run_id: UUID, authorization: str | None = Header(default=None)):
        identity, _ = await app.state.auth.verify(authorization)
        row = await app.state.store.get(run_id, identity)
        logs = await app.state.store.pool.fetch(
            "SELECT id,kind,data,created_at FROM fs2_workshop.events WHERE run_id=$1 ORDER BY id", run_id
        )
        body = {
            "schema": "fs2-workshop-report/v1",
            "run": public_run(row),
            "events": [{**dict(r), "data": json.loads(r["data"])} for r in logs],
        }
        return JSONResponse(
            jsonable_encoder(body), headers={"Content-Disposition": f'attachment; filename="{run_id}.json"'}
        )

    @app.get("/v1/workshop/runs/{run_id}/audio/{turn_index}")
    async def audio(run_id: UUID, turn_index: int, authorization: str | None = Header(default=None)):
        identity, _ = await app.state.auth.verify(authorization)
        await app.state.store.get(run_id, identity)
        content = await app.state.store.pool.fetchval(
            "SELECT wav FROM fs2_workshop.audio WHERE run_id=$1 AND turn_index=$2", run_id, turn_index
        )
        if content is None:
            raise HTTPException(404, "No recording for this turn")
        return Response(bytes(content), media_type="audio/wav")

    @app.get("/v1/workshop/runs/{run_id}/audio/{turn_index}/segments/{attempt_id}/{segment_index}")
    async def audio_segment(
        run_id: UUID,
        turn_index: int,
        attempt_id: UUID,
        segment_index: int,
        authorization: str | None = Header(default=None),
    ):
        identity, _ = await app.state.auth.verify(authorization)
        await app.state.store.get(run_id, identity)
        content = await app.state.store.pool.fetchval(
            "SELECT wav FROM fs2_workshop.audio_segments "
            "WHERE run_id=$1 AND turn_index=$2 AND attempt_id=$3 AND segment_index=$4",
            run_id,
            turn_index,
            attempt_id,
            segment_index,
        )
        if content is None:
            raise HTTPException(404, "No recording for this segment")
        return Response(bytes(content), media_type="audio/wav")

    @app.websocket("/v1/workshop/runs/{run_id}/playback")
    async def playback(socket: WebSocket, run_id: UUID):
        if socket.headers.get("origin") not in {None, settings.public_origin}:
            await socket.close(code=1008)
            return
        await socket.accept()
        queue, receiving, pending = None, None, None
        try:
            first = await asyncio.wait_for(socket.receive_text(), 10)
            if len(first) > 8192:
                raise ValueError("Authentication message too large")
            identity, _ = await app.state.auth.verify("Bearer " + str(json.loads(first).get("token", "")))
            # Ownership is checked before subscribing: shared database notifications
            # never carry credentials, and cannot bypass this per-run authorization.
            await app.state.store.get(run_id, identity)
            queue = app.state.audio_bus.subscribe(run_id)
            row = await app.state.store.get(run_id, identity)
            await socket.send_json(
                {
                    "type": "playback.ready",
                    "run_id": str(run_id),
                    "version": row["version"],
                    "status": row["status"],
                    "mode": row["state"]["config"]["mode"],
                    "recordings": [
                        {"turn_index": index, "url": recording["audio_url"]}
                        for index, turn in enumerate(row["state"]["transcript"])
                        for recording in turn.get("audio_segments", [turn] if turn.get("audio_url") else [])
                    ],
                    "replay": "retained_wav_segments_or_legacy_turn",
                }
            )
            receiving = asyncio.create_task(socket.receive())
            pending = asyncio.create_task(queue.get())
            while True:
                done, _ = await asyncio.wait({receiving, pending}, timeout=20, return_when=asyncio.FIRST_COMPLETED)
                if receiving in done:
                    message = receiving.result()
                    if message["type"] == "websocket.disconnect":
                        return
                    receiving = asyncio.create_task(socket.receive())
                if pending in done:
                    await asyncio.wait_for(socket.send_json(pending.result()), 10)
                    pending = asyncio.create_task(queue.get())
                if not done:
                    await socket.send_json({"type": "playback.keepalive"})
        except WebSocketDisconnect:
            pass
        except (HTTPException, KeyError, ValueError, TimeoutError):
            with contextlib.suppress(RuntimeError, WebSocketDisconnect):
                await socket.send_json(
                    {"type": "playback.error", "message": "Playback unavailable; verify run ownership and reconnect"}
                )
        finally:
            if queue is not None:
                app.state.audio_bus.unsubscribe(run_id, queue)
            tasks = [task for task in (receiving, pending) if task is not None]
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            with contextlib.suppress(RuntimeError, WebSocketDisconnect):
                await socket.close()

    @app.websocket("/v1/workshop/runs/{run_id}/microphone")
    async def microphone(socket: WebSocket, run_id: UUID):
        if socket.headers.get("origin") not in {None, settings.public_origin}:
            await socket.close(code=1008)
            return
        await socket.accept()
        try:
            first = await asyncio.wait_for(socket.receive_text(), 10)
            if len(first) > 8192:
                raise ValueError("Authentication message too large")
            first = json.loads(first)
            identity, token = await app.state.auth.verify("Bearer " + str(first.get("token", "")))
            row = await app.state.store.get(run_id, identity)
            role = row["state"].get("takeover_role")
            if row["status"] != "takeover" or role != row["state"]["next_role"]:
                raise ValueError("Take over the current speaker before starting the microphone")
            model = first.get("model", "nemotron-speech-en-0-6b")
            if model not in {"nemotron-speech-en-0-6b", "nemotron-speech-multilingual-0-6b"}:
                raise ValueError("Choose an available speech recognition model")
            async with connect(
                settings.speech_url,
                additional_headers={"Authorization": f"Bearer {token}"},
                max_size=1024 * 1024,
                open_timeout=15,
            ) as upstream:
                await upstream.send(
                    json.dumps(
                        {
                            "type": "session.start",
                            "options": {"model": model},
                            "audio": {"encoding": "pcm_s16le", "sample_rate_hz": 16000, "channels": 1},
                        }
                    )
                )
                pcm, finals = bytearray(), []

                async def upload():
                    while True:
                        event = await asyncio.wait_for(socket.receive(), 120)
                        if event["type"] == "websocket.disconnect":
                            raise WebSocketDisconnect()
                        if event.get("bytes") is not None:
                            chunk = event["bytes"]
                            if len(chunk) > 65536 or len(pcm) + len(chunk) > 16000 * 2 * 120:
                                raise ValueError("Microphone turn exceeds two minutes or maximum chunk size")
                            pcm.extend(chunk)
                            await upstream.send(chunk)
                        elif event.get("text"):
                            control = json.loads(event["text"])
                            if control not in ({"type": "session.finish"}, {"type": "session.cancel"}):
                                raise ValueError("Invalid microphone control")
                            # The browser finishes a recording; the shared speech
                            # protocol finishes its PCM input, not the session.
                            await upstream.send(
                                json.dumps(
                                    {"type": "input.finish"}
                                    if control["type"] == "session.finish"
                                    else control
                                )
                            )
                            return

                reader = asyncio.create_task(upload())

                def upload_finished(task):
                    if not task.cancelled() and task.exception() is not None:
                        asyncio.create_task(upstream.close())

                reader.add_done_callback(upload_finished)
                try:
                    async with asyncio.timeout(240):
                        async for payload in upstream:
                            event = json.loads(payload)
                            if event.get("type") == "transcript.final":
                                finals.append(event)
                            await socket.send_json(event)
                            if event.get("type") == "session.completed":
                                text = " ".join(str(e.get("text", "")) for e in finals).strip()
                                if not text:
                                    raise ValueError("No complete transcript was returned")
                                current = await app.state.store.get(run_id, identity)
                                if current["version"] != row["version"]:
                                    raise ValueError(
                                        "Run changed while microphone was active; recording was not submitted"
                                    )
                                updated = await app.state.store.intervene(
                                    run_id,
                                    identity,
                                    Intervention(action="say", role=role, text=text, source="microphone"),
                                    version=row["version"],
                                    audio=(
                                        wav_bytes(bytes(pcm), 16000),
                                        {"source": "microphone", "model": model, "segments": finals},
                                    ),
                                )
                                await socket.send_json(
                                    {"type": "workshop.message_submitted", "run": jsonable_encoder(updated)}
                                )
                                break
                            if event.get("type") in {"session.error", "session.cancelled"}:
                                break
                finally:
                    reader.cancel()
                    with contextlib.suppress(asyncio.CancelledError, WebSocketDisconnect):
                        await reader
        except WebSocketDisconnect:
            return
        except Exception as exc:
            with contextlib.suppress(RuntimeError, WebSocketDisconnect):
                await socket.send_json(
                    {
                        "type": "workshop.error",
                        "message": str(exc)
                        if isinstance(exc, ValueError)
                        else "Microphone session ended; reconnect or use typed takeover",
                    }
                )
        finally:
            with contextlib.suppress(RuntimeError, WebSocketDisconnect):
                await socket.close()

    static = Path(__file__).with_name("static")
    app.mount("/workshop/static", StaticFiles(directory=static), name="workshop-static")

    @app.get("/workshop")
    @app.get("/workshop/")
    async def page():
        return FileResponse(
            static / "index.html",
            headers={
                "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self'; "
                "connect-src 'self'; "
                "media-src 'self' blob:; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'"
            },
        )

    return app


def main():
    uvicorn.run(create_app(), host="0.0.0.0", port=8080, proxy_headers=False)
