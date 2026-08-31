"""NIM-shaped HTTP adapter for the pinned public Boltz-2 implementation."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import os
import shutil
import subprocess
import tempfile
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Literal

import torch
import yaml
from fastapi import FastAPI, HTTPException
from fastapi.responses import PlainTextResponse
from huggingface_hub import hf_hub_download
from pydantic import BaseModel, ConfigDict, Field, field_validator


LOGGER = logging.getLogger("fs2.boltz2")
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))

SOURCE_REVISION = os.environ["BOLTZ_SOURCE_REVISION"]
MODEL_REPOSITORY = os.environ["BOLTZ_MODEL_REPOSITORY"]
MODEL_REVISION = os.environ["BOLTZ_MODEL_REVISION"]
CACHE = Path(os.getenv("BOLTZ_CACHE", "/models"))
EXPECTED = {
    "boltz2_conf.ckpt": os.environ["BOLTZ_CONF_SHA256"],
    "boltz2_aff.ckpt": os.environ["BOLTZ_AFF_SHA256"],
    "mols.tar": os.environ["BOLTZ_MOLS_SHA256"],
}
AMINO_ACIDS = frozenset("ACDEFGHIKLMNPQRSTVWY")


class A3M(BaseModel):
    model_config = ConfigDict(extra="forbid")
    alignment: str = Field(min_length=3, max_length=32 * 1024 * 1024)
    format: Literal["a3m"] = "a3m"
    rank: int = Field(default=0, ge=0)


class MSASearch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    a3m: A3M


class MSA(BaseModel):
    model_config = ConfigDict(extra="forbid")
    msa_search: MSASearch


class Polymer(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(min_length=1, max_length=8, pattern=r"^[A-Za-z0-9]+$")
    molecule_type: Literal["protein"]
    sequence: str = Field(min_length=6, max_length=2048)
    msa: MSA

    @field_validator("sequence")
    @classmethod
    def validate_sequence(cls, value: str) -> str:
        sequence = "".join(value.split()).upper()
        if any(character not in AMINO_ACIDS for character in sequence):
            raise ValueError("protein sequence contains a non-canonical residue")
        return sequence


class PredictRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    polymers: list[Polymer] = Field(min_length=1, max_length=8)
    recycling_steps: int = Field(default=3, ge=1, le=10)
    sampling_steps: int = Field(default=200, ge=1, le=500)
    diffusion_samples: int = Field(default=1, ge=1, le=8)
    output_format: Literal["mmcif"] = "mmcif"


class Runtime:
    ready = False
    startup_seconds = 0.0
    artifact_sha256: dict[str, str] = {}
    requests = 0
    failures = 0
    predictions = 0
    lock = asyncio.Lock()


RUNTIME = Runtime()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(8 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _prepare_runtime() -> None:
    started = time.monotonic()
    CACHE.mkdir(parents=True, exist_ok=True)
    if not torch.cuda.is_available():
        raise RuntimeError("CUDA is required")
    capability = torch.cuda.get_device_capability(0)
    if capability != (10, 3):
        raise RuntimeError(f"expected B300 compute capability 10.3, got {capability}")

    for filename, expected in EXPECTED.items():
        downloaded = Path(
            hf_hub_download(
                repo_id=MODEL_REPOSITORY,
                filename=filename,
                revision=MODEL_REVISION,
                cache_dir=CACHE / "huggingface",
            )
        )
        # Hugging Face stores LFS artifacts under their SHA-256 object name.
        # Once the pinned revision is hydrated, use that immutable CAS identity
        # instead of rereading several GiB from the network PVC on every pod
        # restart. Fall back to a full digest for a non-CAS cache layout.
        resolved = downloaded.resolve(strict=True)
        actual = (
            expected
            if resolved.name == expected and resolved.stat().st_size > 0
            else _sha256(resolved)
        )
        if actual != expected:
            raise RuntimeError(f"artifact digest mismatch for {filename}")
        destination = CACHE / filename
        if not destination.exists():
            try:
                destination.symlink_to(downloaded)
            except OSError:
                shutil.copyfile(downloaded, destination)
        RUNTIME.artifact_sha256[filename] = actual

    # The pinned upstream helper extracts the CCD tar into ``cache/mols``.
    # Boltz 2 no longer produces the legacy ``ccd.pkl`` marker used by older
    # releases, so readiness must follow the actual upstream cache contract.
    from boltz.main import download_boltz2

    download_boltz2(CACHE)
    mols = CACHE / "mols"
    if not mols.is_dir() or next(mols.iterdir(), None) is None:
        raise RuntimeError("Boltz CCD cache is incomplete")
    RUNTIME.startup_seconds = time.monotonic() - started
    RUNTIME.ready = True
    LOGGER.info(
        "boltz2 runtime ready source=%s model_revision=%s gpu=%s capability=%s "
        "startup_seconds=%.3f artifacts=%s",
        SOURCE_REVISION,
        MODEL_REVISION,
        torch.cuda.get_device_name(0),
        capability,
        RUNTIME.startup_seconds,
        RUNTIME.artifact_sha256,
    )


@asynccontextmanager
async def lifespan(_: FastAPI):
    await asyncio.to_thread(_prepare_runtime)
    yield


app = FastAPI(title="FS2 Boltz-2", version=SOURCE_REVISION[:12], lifespan=lifespan)


def _finite_score(value: Any, name: str) -> float:
    score = float(value)
    if not math.isfinite(score) or not 0 <= score <= 1:
        raise RuntimeError(f"invalid {name}")
    return score


def _predict(request: PredictRequest) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="fs2-boltz2-") as temporary:
        root = Path(temporary)
        sequences: list[dict[str, Any]] = []
        for index, polymer in enumerate(request.polymers):
            msa = root / f"msa-{index}.a3m"
            msa.write_text(polymer.msa.msa_search.a3m.alignment + "\n", encoding="utf-8")
            sequences.append(
                {
                    "protein": {
                        "id": polymer.id,
                        "sequence": polymer.sequence,
                        "msa": str(msa),
                    }
                }
            )
        input_path = root / "request.yaml"
        input_path.write_text(
            yaml.safe_dump({"version": 1, "sequences": sequences}, sort_keys=False),
            encoding="utf-8",
        )
        output = root / "output"
        command = [
            "boltz",
            "predict",
            str(input_path),
            "--out_dir",
            str(output),
            "--cache",
            str(CACHE),
            "--devices",
            "1",
            "--accelerator",
            "gpu",
            "--recycling_steps",
            str(request.recycling_steps),
            "--sampling_steps",
            str(request.sampling_steps),
            "--diffusion_samples",
            str(request.diffusion_samples),
            "--output_format",
            "mmcif",
            "--num_workers",
            "0",
            "--override",
            "--model",
            "boltz2",
            "--no_kernels",
        ]
        completed = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=1200,
            env={**os.environ, "CUDA_VISIBLE_DEVICES": "0"},
        )
        if completed.returncode != 0:
            LOGGER.error(
                "Boltz command failed code=%s stdout_tail=%s stderr_tail=%s",
                completed.returncode,
                completed.stdout[-4000:],
                completed.stderr[-4000:],
            )
            raise RuntimeError(f"Boltz exited {completed.returncode}")
        prediction_dir = output / "boltz_results_request" / "predictions" / "request"
        structures = sorted(prediction_dir.glob("request_model_*.cif"))
        confidences = sorted(prediction_dir.glob("confidence_request_model_*.json"))
        if len(structures) != request.diffusion_samples or len(confidences) != len(structures):
            raise RuntimeError("Boltz output set is incomplete")
        structure_payload: list[dict[str, str]] = []
        confidence_scores: list[float] = []
        ptm_scores: list[float] = []
        for structure_path, confidence_path in zip(structures, confidences, strict=True):
            structure = structure_path.read_text(encoding="utf-8")
            confidence = json.loads(confidence_path.read_text(encoding="utf-8"))
            structure_payload.append({"format": "mmcif", "structure": structure})
            confidence_scores.append(_finite_score(confidence["confidence_score"], "confidence"))
            ptm_scores.append(_finite_score(confidence["ptm"], "ptm"))
        return {
            "structures": structure_payload,
            "confidence_scores": confidence_scores,
            "ptm_scores": ptm_scores,
        }


@app.get("/v1/health/ready")
def ready() -> dict[str, Any]:
    if not RUNTIME.ready:
        raise HTTPException(status_code=503, detail="model artifacts are loading")
    return {
        "status": "ready",
        "model": "boltz2",
        "source_revision": SOURCE_REVISION,
        "model_revision": MODEL_REVISION,
        "artifact_sha256": RUNTIME.artifact_sha256,
        "compute_capability": "10.3",
        "startup_seconds": round(RUNTIME.startup_seconds, 6),
    }


@app.get("/metrics", response_class=PlainTextResponse)
def metrics() -> str:
    return "\n".join(
        (
            "# TYPE fs2_model_requests_total counter",
            f'fs2_model_requests_total{{model="boltz2"}} {RUNTIME.requests}',
            "# TYPE fs2_model_failures_total counter",
            f'fs2_model_failures_total{{model="boltz2"}} {RUNTIME.failures}',
            "# TYPE fs2_model_outputs_total counter",
            f'fs2_model_outputs_total{{model="boltz2"}} {RUNTIME.predictions}',
            "",
        )
    )


@app.post("/biology/mit/boltz2/predict")
async def predict(request: PredictRequest) -> dict[str, Any]:
    if not RUNTIME.ready:
        raise HTTPException(status_code=503, detail="model artifacts are loading")
    RUNTIME.requests += 1
    try:
        async with RUNTIME.lock:
            response = await asyncio.to_thread(_predict, request)
        RUNTIME.predictions += len(response["structures"])
        return response
    except Exception as exc:
        RUNTIME.failures += 1
        LOGGER.exception("Boltz2 prediction failed")
        raise HTTPException(status_code=500, detail=type(exc).__name__) from exc
