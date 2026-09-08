"""A small native HTTP worker; the existing platform owns auth, queueing and usage."""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import PlainTextResponse

from aging.contracts import AltumAgeRequest, ClinicalRequest

LOGGER = logging.getLogger("fs2.aging")
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))


def create_app(model_id: str | None = None, *, runtime_factory=None) -> FastAPI:
    selected = model_id or os.getenv("AGING_MODEL", "phenoage")
    if selected not in {"altumage", "phenoage"}:
        raise ValueError("AGING_MODEL must be altumage or phenoage")
    state = {
        "runtime": None,
        "requests": 0,
        "failures": 0,
        "samples": 0,
        "seconds": 0.0,
    }
    lock = threading.Lock()

    @asynccontextmanager
    async def lifespan(_app):
        started = time.perf_counter()
        if runtime_factory is not None:
            state["runtime"] = runtime_factory()
        elif selected == "phenoage":
            from aging.phenoage.runtime import ClinicalPhenoAgeRuntime

            if os.getenv("AGING_DEVICE", "cpu") != "cpu":
                raise ValueError(
                    "clinical PhenoAge is a CPU formula; use AGING_DEVICE=cpu"
                )
            state["runtime"] = ClinicalPhenoAgeRuntime()
        else:
            from aging.altumage.runtime import AltumAgeRuntime

            state["runtime"] = AltumAgeRuntime(
                Path(os.getenv("AGING_ARTIFACT_ROOT", "/opt/altumage")),
                device=os.getenv("AGING_DEVICE", "cpu"),
                threads=int(os.getenv("AGING_CPU_THREADS", "1")),
            )
        LOGGER.info(
            json.dumps(
                {
                    "event": "model_ready",
                    "model_id": selected,
                    "model_load_seconds": time.perf_counter() - started,
                    **state["runtime"].metadata(),
                }
            )
        )
        yield
        state["runtime"] = None

    application = FastAPI(title=f"FS2 {selected}", version="1", lifespan=lifespan)

    @application.get("/healthz")
    @application.get("/v1/health/live")
    def live():
        return {"status": "alive"}

    @application.get("/readyz")
    @application.get("/v1/health/ready")
    def ready():
        if state["runtime"] is None:
            raise HTTPException(status_code=503, detail="model is loading")
        return {"status": "ready", **state["runtime"].metadata()}

    def invoke(request):
        with lock:
            runtime = state["runtime"]
            if runtime is None:
                raise HTTPException(status_code=503, detail="model is loading")
            started = time.perf_counter()
            state["requests"] += 1
            try:
                predictions = runtime.predict(request)
            except ValueError as exc:
                state["failures"] += 1
                raise HTTPException(status_code=422, detail=str(exc)) from exc
            except Exception:
                state["failures"] += 1
                LOGGER.exception("model execution failed model_id=%s", selected)
                raise
            finally:
                elapsed = time.perf_counter() - started
                state["seconds"] += elapsed
            state["samples"] += len(predictions)
            LOGGER.info(
                json.dumps(
                    {
                        "event": "inference_completed",
                        "model_id": selected,
                        "sample_count": len(predictions),
                        "execution_seconds": elapsed,
                        "device": runtime.device,
                    }
                )
            )
            return {
                "predictions": predictions,
                "sample_count": len(predictions),
                "execution_seconds": elapsed,
                **runtime.metadata(),
            }

    if selected == "phenoage":

        @application.post("/v1/predict")
        def predict_clinical(request: ClinicalRequest):
            return invoke(request)
    else:

        @application.post("/v1/predict")
        def predict_methylation(request: AltumAgeRequest):
            return invoke(request)

    @application.get("/metrics", response_class=PlainTextResponse)
    def metrics():
        with lock:
            values = dict(state)
        entries = {
            "fs2_model_requests_total": values["requests"],
            "fs2_model_failures_total": values["failures"],
            "fs2_model_samples_total": values["samples"],
            "fs2_model_execution_seconds_total": values["seconds"],
        }
        return (
            "\n".join(
                f'{name}{{model="{selected}"}} {value}'
                for name, value in entries.items()
            )
            + "\n"
        )

    return application


app = create_app()
