"""B300-capable HTTP adapter for the public GenMol v2 implementation."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import math
import os
import re
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import torch
from fastapi import FastAPI, HTTPException
from fastapi.responses import PlainTextResponse
from huggingface_hub import hf_hub_download
from pydantic import BaseModel, ConfigDict, Field, field_validator
from rdkit import Chem
from rdkit.Chem import Crippen, QED


LOGGER = logging.getLogger("fs2.genmol")
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))

MODEL_REPOSITORY = os.environ["GENMOL_MODEL_REPOSITORY"]
MODEL_REVISION = os.environ["GENMOL_MODEL_REVISION"]
MODEL_SHA256 = os.environ["GENMOL_MODEL_SHA256"]
SOURCE_REVISION = os.environ["GENMOL_SOURCE_REVISION"]
MODEL_CACHE = Path(os.getenv("MODEL_CACHE", "/models"))
MASK_RANGE = re.compile(r"^\[\*\{(?P<minimum>[0-9]+)-(?P<maximum>[0-9]+)\}\]$")


class GenerateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    smiles: str
    num_molecules: int = Field(default=1, ge=1, le=16)
    scoring: str = "QED"
    temperature: float | str = 1.0
    noise: float | str = 1.0
    step_size: int = Field(default=1, ge=1, le=100)
    unique: bool = False

    @field_validator("scoring")
    @classmethod
    def validate_scoring(cls, value: str) -> str:
        normalized = value.upper()
        if normalized not in {"QED", "LOGP"}:
            raise ValueError("scoring must be QED or LogP")
        return normalized


class Runtime:
    sampler: Any | None = None
    ready = False
    startup_seconds = 0.0
    requests = 0
    failures = 0
    generated = 0
    lock = asyncio.Lock()


RUNTIME = Runtime()


def _finite(value: float | str, name: str) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=f"{name} must be numeric") from exc
    if not math.isfinite(result):
        raise HTTPException(status_code=422, detail=f"{name} must be finite")
    return result


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(8 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_runtime() -> None:
    started = time.monotonic()
    MODEL_CACHE.mkdir(parents=True, exist_ok=True)
    checkpoint = Path(
        hf_hub_download(
            repo_id=MODEL_REPOSITORY,
            filename="model_v2.ckpt",
            revision=MODEL_REVISION,
            cache_dir=MODEL_CACHE,
        )
    )
    actual_sha256 = _file_sha256(checkpoint)
    if actual_sha256 != MODEL_SHA256:
        raise RuntimeError(
            f"checkpoint digest mismatch: expected {MODEL_SHA256}, got {actual_sha256}"
        )
    if not torch.cuda.is_available():
        raise RuntimeError("CUDA is required")
    capability = torch.cuda.get_device_capability(0)
    if capability != (10, 3):
        raise RuntimeError(f"expected B300 compute capability 10.3, got {capability}")

    from genmol.sampler import Sampler

    sampler = Sampler(str(checkpoint))
    sampler.model.to(torch.device("cuda:0"))
    sampler.model.eval()
    sampler.mdlm.to_device(sampler.model.device)
    RUNTIME.sampler = sampler
    RUNTIME.startup_seconds = time.monotonic() - started
    RUNTIME.ready = True
    LOGGER.info(
        "genmol runtime ready source=%s model_revision=%s model_sha256=%s "
        "gpu=%s capability=%s startup_seconds=%.3f",
        SOURCE_REVISION,
        MODEL_REVISION,
        MODEL_SHA256,
        torch.cuda.get_device_name(0),
        capability,
        RUNTIME.startup_seconds,
    )


@asynccontextmanager
async def lifespan(_: FastAPI):
    await asyncio.to_thread(_load_runtime)
    yield


app = FastAPI(title="FS2 GenMol v2", version=SOURCE_REVISION[:12], lifespan=lifespan)


@app.get("/v1/health/ready")
def ready() -> dict[str, Any]:
    if not RUNTIME.ready:
        raise HTTPException(status_code=503, detail="model is loading")
    return {
        "status": "ready",
        "model": "genmol",
        "source_revision": SOURCE_REVISION,
        "model_revision": MODEL_REVISION,
        "model_sha256": MODEL_SHA256,
        "compute_capability": "10.3",
        "startup_seconds": round(RUNTIME.startup_seconds, 6),
    }


@app.get("/metrics", response_class=PlainTextResponse)
def metrics() -> str:
    return "\n".join(
        (
            "# TYPE fs2_model_requests_total counter",
            f'fs2_model_requests_total{{model="genmol"}} {RUNTIME.requests}',
            "# TYPE fs2_model_failures_total counter",
            f'fs2_model_failures_total{{model="genmol"}} {RUNTIME.failures}',
            "# TYPE fs2_model_outputs_total counter",
            f'fs2_model_outputs_total{{model="genmol"}} {RUNTIME.generated}',
            "",
        )
    )


@app.post("/generate")
async def generate(request: GenerateRequest) -> dict[str, Any]:
    if not RUNTIME.ready or RUNTIME.sampler is None:
        raise HTTPException(status_code=503, detail="model is loading")
    match = MASK_RANGE.fullmatch(request.smiles)
    if match is None:
        raise HTTPException(
            status_code=422,
            detail="this GenMol lane expects a de-novo mask such as [*{20-30}]",
        )
    minimum = int(match.group("minimum"))
    maximum = int(match.group("maximum"))
    if not 1 <= minimum <= maximum <= 512:
        raise HTTPException(status_code=422, detail="invalid generation length range")
    temperature = _finite(request.temperature, "temperature")
    randomness = _finite(request.noise, "noise")
    if temperature <= 0 or randomness < 0:
        raise HTTPException(status_code=422, detail="invalid sampling parameters")

    RUNTIME.requests += 1
    started = time.monotonic()
    try:
        async with RUNTIME.lock:
            samples = await asyncio.to_thread(
                RUNTIME.sampler.de_novo_generation,
                request.num_molecules,
                temperature,
                randomness,
                (minimum + maximum) // 2,
            )
        molecules: list[dict[str, Any]] = []
        seen: set[str] = set()
        for smiles in samples:
            molecule = Chem.MolFromSmiles(smiles)
            if molecule is None or molecule.GetNumAtoms() < 1:
                continue
            canonical = Chem.MolToSmiles(molecule)
            if request.unique and canonical in seen:
                continue
            seen.add(canonical)
            score = (
                float(QED.qed(molecule))
                if request.scoring == "QED"
                else float(Crippen.MolLogP(molecule))
            )
            molecules.append({"smiles": canonical, "score": score})
        if not molecules:
            raise RuntimeError("GenMol produced no valid molecule")
        RUNTIME.generated += len(molecules)
        return {
            "status": "success",
            "molecules": molecules,
            "metrics": {
                "model": "NV-GenMol-89M-v2",
                "elapsed_seconds": round(time.monotonic() - started, 6),
                "source_revision": SOURCE_REVISION,
                "model_revision": MODEL_REVISION,
            },
        }
    except HTTPException:
        RUNTIME.failures += 1
        raise
    except Exception as exc:
        RUNTIME.failures += 1
        LOGGER.exception("GenMol inference failed")
        raise HTTPException(status_code=500, detail=type(exc).__name__) from exc
