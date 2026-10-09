from __future__ import annotations

import asyncio
import base64
import hashlib
import io
from contextlib import asynccontextmanager
from importlib.metadata import version
from typing import Annotated, Literal

import numpy as np
import tifffile
import torch
from cellpose import models
from fastapi import FastAPI, HTTPException
from PIL import Image
from pydantic import BaseModel, Field

MODEL_ID = "mouseland/cellpose-sam/cpsam_v2"
MAX_INPUT_BYTES = 16 * 1024 * 1024
MAX_PIXELS = 4_194_304

model: models.CellposeModel | None = None
model_sha256 = ""
inference_lock = asyncio.Lock()


class SegmentRequest(BaseModel):
    image_base64: Annotated[str, Field(min_length=4)]
    media_type: Literal["image/png", "image/jpeg", "image/tiff"] = "image/png"
    diameter: Annotated[float | None, Field(gt=0, le=2048)] = None
    research_only: Literal[True]


class SegmentResponse(BaseModel):
    model_id: str
    model_sha256: str
    cellpose_version: str
    input_sha256: str
    object_count: int
    image_shape: list[int]
    mask_media_type: Literal["image/png"] = "image/png"
    mask_base64: str
    overlay_media_type: Literal["image/png"] = "image/png"
    overlay_base64: str
    object_areas_px: list[int]
    research_only: Literal[True] = True
    commercial_use: Literal[False] = False


def decode_image(request: SegmentRequest) -> tuple[np.ndarray, str]:
    try:
        raw = base64.b64decode(request.image_base64, validate=True)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="image_base64 is not valid base64") from exc
    if not raw or len(raw) > MAX_INPUT_BYTES:
        raise HTTPException(status_code=413, detail="decoded image must be 1 byte to 16 MiB")

    try:
        if request.media_type == "image/tiff":
            image = np.asarray(tifffile.imread(io.BytesIO(raw)))
        else:
            with Image.open(io.BytesIO(raw)) as opened:
                image = np.asarray(opened.convert("RGB") if opened.mode not in {"L", "RGB"} else opened)
    except Exception as exc:
        raise HTTPException(status_code=422, detail="image could not be decoded") from exc

    if image.ndim not in {2, 3}:
        raise HTTPException(status_code=422, detail="only bounded 2D grayscale or RGB images are accepted")
    if int(np.prod(image.shape[:2])) > MAX_PIXELS:
        raise HTTPException(status_code=413, detail="image exceeds the 4-megapixel interactive limit")
    if image.ndim == 3 and image.shape[-1] not in {1, 2, 3}:
        raise HTTPException(status_code=422, detail="2D images may contain at most three channels")
    return image, hashlib.sha256(raw).hexdigest()


def png_base64(image: np.ndarray, *, mode: str | None = None) -> str:
    buffer = io.BytesIO()
    Image.fromarray(image, mode=mode).save(buffer, format="PNG")
    return base64.b64encode(buffer.getvalue()).decode("ascii")


def encode_mask(mask: np.ndarray) -> str:
    if mask.max(initial=0) > np.iinfo(np.uint16).max:
        raise HTTPException(status_code=500, detail="mask contains too many objects for PNG encoding")
    return png_base64(mask.astype(np.uint16), mode="I;16")


def color_overlay(image: np.ndarray, mask: np.ndarray) -> str:
    if image.ndim == 2:
        source = np.repeat(image[..., None], 3, axis=2)
    else:
        source = image[..., :3]
        if source.shape[-1] == 1:
            source = np.repeat(source, 3, axis=2)
        elif source.shape[-1] == 2:
            source = np.concatenate([source, source[..., :1]], axis=2)
    source = source.astype(np.float32)
    if source.max(initial=0) > 255 or source.min(initial=0) < 0:
        low, high = np.percentile(source, [1, 99])
        source = np.clip((source - low) * 255 / max(high - low, 1e-6), 0, 255)
    labels = mask.astype(np.uint32)
    colors = np.stack(
        [
            (labels * 37 + 29) % 255,
            (labels * 73 + 71) % 255,
            (labels * 109 + 113) % 255,
        ],
        axis=-1,
    ).astype(np.float32)
    foreground = labels > 0
    overlay = source.copy()
    overlay[foreground] = source[foreground] * 0.45 + colors[foreground] * 0.55
    boundaries = foreground & (
        (labels != np.roll(labels, 1, axis=0))
        | (labels != np.roll(labels, -1, axis=0))
        | (labels != np.roll(labels, 1, axis=1))
        | (labels != np.roll(labels, -1, axis=1))
    )
    overlay[boundaries] = 255
    return png_base64(np.clip(overlay, 0, 255).astype(np.uint8))


def weight_digest() -> str:
    path = models.MODEL_DIR / "cpsam_v2"
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


@asynccontextmanager
async def lifespan(_: FastAPI):
    global model, model_sha256
    if not torch.cuda.is_available():
        raise RuntimeError("CUDA is required for this deployment")
    model = models.CellposeModel(gpu=True, pretrained_model="cpsam_v2")
    model_sha256 = weight_digest()
    yield
    model = None


app = FastAPI(
    title="Nebius Scientific AI Cellpose CPSAM v2",
    version="1.1.0",
    description="Research-only bounded 2D cell segmentation backend.",
    lifespan=lifespan,
)


@app.get("/v1/health/live")
def live() -> dict[str, str]:
    return {"status": "live"}


@app.get("/v1/health/ready")
def ready() -> dict[str, str]:
    if model is None or not torch.cuda.is_available():
        raise HTTPException(status_code=503, detail="model is not ready")
    return {"status": "ready", "model": MODEL_ID, "model_sha256": model_sha256}


@app.post("/v1/segment", response_model=SegmentResponse)
async def segment(request: SegmentRequest) -> SegmentResponse:
    if model is None:
        raise HTTPException(status_code=503, detail="model is not ready")
    image, input_sha256 = decode_image(request)
    async with inference_lock:
        masks, _, _ = await asyncio.to_thread(
            model.eval,
            image,
            diameter=request.diameter,
            do_3D=False,
        )
    mask = np.asarray(masks)
    labels, counts = np.unique(mask, return_counts=True)
    object_areas = counts[labels > 0].astype(int).tolist()
    return SegmentResponse(
        model_id=MODEL_ID,
        model_sha256=model_sha256,
        cellpose_version=version("cellpose"),
        input_sha256=input_sha256,
        object_count=len(object_areas),
        image_shape=list(image.shape),
        mask_base64=encode_mask(mask),
        overlay_base64=color_overlay(image, mask),
        object_areas_px=object_areas,
    )
