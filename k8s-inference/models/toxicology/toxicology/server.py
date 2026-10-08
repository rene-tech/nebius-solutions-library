"""Bounded, chunk-fair HTTP execution; FS2 owns auth and durable admission."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import PlainTextResponse

from .contracts import ADMETRequest, CToxRequest
from .inputs import parse_rows, prepare_rows

LOGGER = logging.getLogger("fs2.toxicology")
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))


def create_app(model_id=None, *, runtime_factory=None):
    selected = model_id or os.environ.get("TOX_MODEL", "admet-ai")
    if selected not in {"admet-ai", "ctoxpred2"}:
        raise ValueError("TOX_MODEL must be admet-ai or ctoxpred2")
    capacity = int(os.environ.get("TOX_MAX_INFLIGHT", "8"))
    chunk_size = int(os.environ.get("TOX_CHUNK_SIZE", "32"))
    if not 1 <= capacity <= 64 or not 1 <= chunk_size <= 256:
        raise ValueError("invalid runtime concurrency configuration")
    state = {"runtime": None, "active": 0, "requests": 0, "failures": 0, "molecules": 0, "invalid_molecules": 0, "seconds": 0.0}
    execution = asyncio.Lock()

    @asynccontextmanager
    async def lifespan(app):
        started = time.perf_counter()
        if runtime_factory:
            state["runtime"] = await asyncio.to_thread(runtime_factory)
        elif selected == "admet-ai":
            from .admet import ADMETRuntime
            state["runtime"] = await asyncio.to_thread(ADMETRuntime)
        else:
            from .ctox import CToxRuntime
            state["runtime"] = await asyncio.to_thread(CToxRuntime)
        state["load_seconds"] = time.perf_counter() - started
        LOGGER.info(json.dumps({"event": "model_ready", "model_id": selected, "load_seconds": state["load_seconds"]}))
        yield
        state["runtime"] = None

    app = FastAPI(title=f"Scientific AI {selected}", version="1", lifespan=lifespan)
    app.state.runtime_state = state

    @app.get("/healthz")
    @app.get("/v1/health/live")
    async def live():
        return {"status": "alive"}

    @app.get("/readyz")
    @app.get("/v1/health/ready")
    async def ready():
        if state["runtime"] is None:
            raise HTTPException(503, detail="model is loading")
        return {"status": "ready", "model_id": selected, "load_seconds": state["load_seconds"]}

    @app.get("/v1/metadata")
    async def metadata():
        if state["runtime"] is None:
            raise HTTPException(503, detail="model is loading")
        return state["runtime"].metadata()

    async def invoke(payload, request):
        runtime = state["runtime"]
        if runtime is None:
            raise HTTPException(503, detail="model is loading")
        if state["active"] >= capacity:
            raise HTTPException(429, detail={"code": "runtime_busy", "retryable": True}, headers={"Retry-After": "2"})
        if payload.endpoints and set(payload.endpoints) - set(runtime.endpoints):
            raise HTTPException(422, detail={"code": "unknown_endpoint", "supported": list(runtime.endpoints)})
        state["active"] += 1
        state["requests"] += 1
        started = time.perf_counter()
        try:
            rows = await asyncio.to_thread(parse_rows, payload)
            valid, results = await asyncio.to_thread(prepare_rows, rows)
            for offset in range(0, len(valid), chunk_size):
                chunk = valid[offset:offset + chunk_size]
                # asyncio.Lock is FIFO: each operation yields after a chunk,
                # allowing other customers to make progress during large batches.
                async with execution:
                    work = asyncio.create_task(asyncio.to_thread(runtime.predict, [row.smiles for row in chunk], endpoints=payload.endpoints, method=getattr(payload, "method", None), seed=getattr(payload, "seed", 0)))
                    try:
                        values = await asyncio.shield(work)
                    except asyncio.CancelledError:
                        # A cancelled HTTP request must not release shared model
                        # state while its CPU thread is still executing.
                        await work
                        raise
                if len(values) != len(chunk):
                    raise RuntimeError("prediction count differs from submitted records")
                for row, value in zip(chunk, values):
                    results[row.index].update(status="succeeded", **value)
                state["molecules"] += len(chunk)
            invalid = len(rows) - len(valid)
            state["invalid_molecules"] += invalid
            elapsed = time.perf_counter() - started
            LOGGER.info(json.dumps({"event": "inference_completed", "model_id": selected, "molecule_count": len(rows), "succeeded": len(valid), "invalid": invalid, "seconds": elapsed}))
            return {
                "model_id": selected,
                "model_version": runtime.metadata()["model_version"],
                "status": "succeeded" if not invalid else ("partial" if valid else "failed"),
                "molecule_count": len(rows), "succeeded": len(valid), "failed": invalid,
                "execution_seconds": elapsed, "chunk_size": chunk_size,
                "seed": getattr(payload, "seed", None),
                "results": [results[i] for i in range(len(rows))],
                "endpoint_metadata": {key: value for key, value in runtime.endpoints.items() if not payload.endpoints or key in payload.endpoints},
                "limitations": runtime.metadata()["limitations"],
            }
        except ValueError as exc:
            state["failures"] += 1
            raise HTTPException(422, detail={"code": "invalid_molecular_input", "message": str(exc)}) from exc
        except Exception:
            state["failures"] += 1
            LOGGER.exception("toxicology execution failed model_id=%s", selected)
            raise
        finally:
            state["seconds"] += time.perf_counter() - started
            state["active"] -= 1

    if selected == "admet-ai":
        @app.post("/v1/predict")
        async def predict_admet(payload: ADMETRequest, request: Request):
            return await invoke(payload, request)
    else:
        @app.post("/v1/predict")
        async def predict_ctox(payload: CToxRequest, request: Request):
            return await invoke(payload, request)

    @app.get("/metrics", response_class=PlainTextResponse)
    async def metrics():
        names = {"active": "inflight", "requests": "requests_total", "failures": "failures_total", "molecules": "molecules_total", "invalid_molecules": "invalid_molecules_total", "seconds": "execution_seconds_total"}
        return "\n".join(f'fs2_toxicology_{metric}{{model="{selected}"}} {state[key]}' for key, metric in names.items()) + "\n"

    return app


app = create_app()
