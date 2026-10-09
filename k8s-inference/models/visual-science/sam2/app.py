from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import json
import os
import subprocess
import tempfile
import zipfile
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Literal, Protocol

import numpy as np
from fastapi import FastAPI, HTTPException, Response
from PIL import Image
from pydantic import BaseModel, ConfigDict, Field, model_validator

MODEL_ID = "facebook/sam2.1-hiera-large"
MODEL_REVISION = "665f8e2ad61cf5f53d65644ff27c8ee525124610"
CHECKPOINT_PATH = Path(
    os.environ.get("SAM2_CHECKPOINT", "/opt/sam2/checkpoints/sam2.1_hiera_large.pt")
)
EXPECTED_CHECKPOINT_SHA256 = os.environ.get("SAM2_CHECKPOINT_SHA256", "")
MAX_INPUT_BYTES = 64 * 1024 * 1024
MAX_PIXELS = 2_073_600
MAX_VIDEO_FRAMES = 320
MAX_OBJECTS = 8


class PromptPoint(BaseModel):
    model_config = ConfigDict(extra="forbid")

    x: Annotated[float, Field(ge=0, le=16384)]
    y: Annotated[float, Field(ge=0, le=16384)]
    label: Literal[0, 1] = 1
    object_id: Annotated[int, Field(ge=1, le=65535)] = 1


class SegmentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: Literal["prompted-image", "automatic-image", "prompted-video"]
    media_base64: Annotated[str, Field(min_length=4)]
    media_type: Literal["image/png", "image/jpeg", "video/mp4"]
    points: Annotated[list[PromptPoint], Field(max_length=64)] = Field(
        default_factory=list
    )
    box: Annotated[list[float] | None, Field(min_length=4, max_length=4)] = None
    object_id: Annotated[int, Field(ge=1, le=65535)] = 1
    prompt_frame: Annotated[int, Field(ge=0, lt=MAX_VIDEO_FRAMES)] = 0
    max_masks: Annotated[int, Field(ge=1, le=128)] = 32

    @model_validator(mode="after")
    def validate_mode(self) -> "SegmentRequest":
        image = self.media_type.startswith("image/")
        if self.mode in {"prompted-image", "automatic-image"} and not image:
            raise ValueError("image modes require image/png or image/jpeg")
        if self.mode == "prompted-video" and self.media_type != "video/mp4":
            raise ValueError("prompted-video requires video/mp4")
        if self.mode.startswith("prompted") and not self.points and self.box is None:
            raise ValueError("prompted modes require at least one point or one box")
        if self.mode == "automatic-image" and (self.points or self.box is not None):
            raise ValueError("automatic-image does not accept prompts")
        if (
            len(
                {point.object_id for point in self.points}
                | ({self.object_id} if self.box else set())
            )
            > MAX_OBJECTS
        ):
            raise ValueError("at most eight prompted objects are accepted")
        if self.box is not None and not (
            self.box[0] < self.box[2] and self.box[1] < self.box[3]
        ):
            raise ValueError("box must be [x0,y0,x1,y1] with increasing coordinates")
        return self


@dataclass(frozen=True)
class VideoInfo:
    width: int
    height: int
    frame_count: int
    frame_rate: str


class Backend(Protocol):
    def prompted_image(
        self, image: np.ndarray, request: SegmentRequest
    ) -> tuple[np.ndarray, list[dict[str, object]]]: ...

    def automatic_image(
        self, image: np.ndarray, request: SegmentRequest
    ) -> tuple[np.ndarray, list[dict[str, object]]]: ...

    def prompted_video(
        self, frames: Path, info: VideoInfo, request: SegmentRequest
    ) -> tuple[dict[int, np.ndarray], list[dict[str, object]]]: ...


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _groups(request: SegmentRequest) -> dict[int, list[PromptPoint]]:
    grouped: dict[int, list[PromptPoint]] = {}
    for point in request.points:
        grouped.setdefault(point.object_id, []).append(point)
    if request.box is not None:
        grouped.setdefault(request.object_id, [])
    return dict(sorted(grouped.items()))


class Sam2Backend:
    def __init__(self) -> None:
        from sam2.automatic_mask_generator import SAM2AutomaticMaskGenerator
        from sam2.build_sam import build_sam2, build_sam2_video_predictor
        from sam2.sam2_image_predictor import SAM2ImagePredictor

        config = "configs/sam2.1/sam2.1_hiera_l.yaml"
        image_model = build_sam2(
            config, str(CHECKPOINT_PATH), device="cuda", mode="eval"
        )
        self.image_predictor = SAM2ImagePredictor(image_model)
        self.automatic_generator = SAM2AutomaticMaskGenerator(
            image_model,
            points_per_side=32,
            pred_iou_thresh=0.88,
            stability_score_thresh=0.95,
            min_mask_region_area=64,
        )
        self.video_predictor = build_sam2_video_predictor(
            config,
            str(CHECKPOINT_PATH),
            device="cuda",
            mode="eval",
            vos_optimized=False,
        )

    @staticmethod
    def _validate_prompts(request: SegmentRequest, width: int, height: int) -> None:
        if any(point.x >= width or point.y >= height for point in request.points):
            raise HTTPException(
                status_code=422,
                detail="a prompt point lies outside the media dimensions",
            )
        if request.box is not None and (
            request.box[2] > width or request.box[3] > height
        ):
            raise HTTPException(
                status_code=422,
                detail="the prompt box lies outside the media dimensions",
            )

    def prompted_image(
        self, image: np.ndarray, request: SegmentRequest
    ) -> tuple[np.ndarray, list[dict[str, object]]]:
        height, width = image.shape[:2]
        self._validate_prompts(request, width, height)
        self.image_predictor.set_image(image)
        labels = np.zeros((height, width), dtype=np.uint16)
        objects: list[dict[str, object]] = []
        for object_id, points in _groups(request).items():
            coords = (
                np.asarray([[point.x, point.y] for point in points], dtype=np.float32)
                if points
                else None
            )
            point_labels = (
                np.asarray([point.label for point in points], dtype=np.int32)
                if points
                else None
            )
            box = (
                np.asarray(request.box, dtype=np.float32)
                if request.box is not None and object_id == request.object_id
                else None
            )
            masks, scores, _ = self.image_predictor.predict(
                point_coords=coords,
                point_labels=point_labels,
                box=box,
                multimask_output=False,
            )
            mask = np.asarray(masks[0], dtype=bool)
            labels[mask] = object_id
            objects.append(
                {
                    "object_id": object_id,
                    "score": float(scores[0]),
                    "area_px": int(mask.sum()),
                }
            )
        return labels, objects

    def automatic_image(
        self, image: np.ndarray, request: SegmentRequest
    ) -> tuple[np.ndarray, list[dict[str, object]]]:
        generated = sorted(
            self.automatic_generator.generate(image),
            key=lambda item: int(item["area"]),
            reverse=True,
        )
        labels = np.zeros(image.shape[:2], dtype=np.uint16)
        objects: list[dict[str, object]] = []
        for object_id, item in enumerate(generated[: request.max_masks], start=1):
            mask = np.asarray(item["segmentation"], dtype=bool) & (labels == 0)
            if not mask.any():
                continue
            labels[mask] = object_id
            objects.append(
                {
                    "object_id": object_id,
                    "score": float(item.get("predicted_iou", 0)),
                    "stability_score": float(item.get("stability_score", 0)),
                    "area_px": int(mask.sum()),
                }
            )
        return labels, objects

    def prompted_video(
        self, frames: Path, info: VideoInfo, request: SegmentRequest
    ) -> tuple[dict[int, np.ndarray], list[dict[str, object]]]:
        self._validate_prompts(request, info.width, info.height)
        if request.prompt_frame >= info.frame_count:
            raise HTTPException(
                status_code=422, detail="prompt_frame lies outside the decoded video"
            )
        state = self.video_predictor.init_state(
            video_path=str(frames), offload_video_to_cpu=True
        )
        objects: list[dict[str, object]] = []
        for object_id, points in _groups(request).items():
            coords = (
                np.asarray([[point.x, point.y] for point in points], dtype=np.float32)
                if points
                else None
            )
            point_labels = (
                np.asarray([point.label for point in points], dtype=np.int32)
                if points
                else None
            )
            box = (
                np.asarray(request.box, dtype=np.float32)
                if request.box is not None and object_id == request.object_id
                else None
            )
            self.video_predictor.add_new_points_or_box(
                inference_state=state,
                frame_idx=request.prompt_frame,
                obj_id=object_id,
                points=coords,
                labels=point_labels,
                box=box,
            )
            objects.append({"object_id": object_id})
        output: dict[int, np.ndarray] = {}
        for frame_index, object_ids, logits in self.video_predictor.propagate_in_video(
            state
        ):
            labels = np.zeros((info.height, info.width), dtype=np.uint16)
            for index, object_id in enumerate(object_ids):
                mask = np.asarray((logits[index] > 0).cpu(), dtype=bool).squeeze()
                labels[mask] = int(object_id)
            output[int(frame_index)] = labels
        return output, objects


backend: Backend | None = None
inference_lock = asyncio.Lock()
checkpoint_sha256 = ""


def _decode(raw_base64: str) -> bytes:
    try:
        raw = base64.b64decode(raw_base64, validate=True)
    except ValueError as error:
        raise HTTPException(
            status_code=422, detail="media_base64 is not valid base64"
        ) from error
    if not raw or len(raw) > MAX_INPUT_BYTES:
        raise HTTPException(
            status_code=413, detail="decoded media must be between 1 byte and 64 MiB"
        )
    return raw


def _decode_image(raw: bytes) -> np.ndarray:
    try:
        with Image.open(io.BytesIO(raw)) as opened:
            image = np.asarray(opened.convert("RGB"))
    except Exception as error:
        raise HTTPException(
            status_code=422, detail="image could not be decoded"
        ) from error
    if image.shape[0] * image.shape[1] > MAX_PIXELS:
        raise HTTPException(
            status_code=413, detail="image exceeds the 2,073,600-pixel limit"
        )
    return image


def _png(image: np.ndarray) -> bytes:
    target = io.BytesIO()
    Image.fromarray(image).save(target, format="PNG", optimize=True)
    return target.getvalue()


def _zip_write(archive: zipfile.ZipFile, name: str, content: bytes) -> None:
    entry = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
    entry.compress_type = zipfile.ZIP_DEFLATED
    entry.external_attr = 0o100644 << 16
    archive.writestr(entry, content, compresslevel=6)


def _overlay(image: np.ndarray, labels: np.ndarray) -> np.ndarray:
    source = image.astype(np.float32)
    encoded = labels.astype(np.uint32)
    colors = np.stack(
        [
            (encoded * 47 + 43) % 255,
            (encoded * 89 + 97) % 255,
            (encoded * 131 + 151) % 255,
        ],
        axis=-1,
    ).astype(np.float32)
    foreground = encoded > 0
    output = source.copy()
    output[foreground] = source[foreground] * 0.38 + colors[foreground] * 0.62
    boundary = foreground & (
        (encoded != np.roll(encoded, 1, 0))
        | (encoded != np.roll(encoded, -1, 0))
        | (encoded != np.roll(encoded, 1, 1))
        | (encoded != np.roll(encoded, -1, 1))
    )
    output[boundary] = 255
    return np.clip(output, 0, 255).astype(np.uint8)


def _video_frames(raw: bytes, root: Path) -> tuple[Path, VideoInfo]:
    source = root / "input.mp4"
    frames = root / "frames"
    frames.mkdir()
    source.write_bytes(raw)
    probe_command = [
        "ffprobe",
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-count_frames",
        "-show_entries",
        "stream=width,height,avg_frame_rate,nb_read_frames",
        "-of",
        "json",
        str(source),
    ]
    try:
        probe = json.loads(
            subprocess.run(
                probe_command, check=True, capture_output=True, text=True, timeout=30
            ).stdout
        )
        stream = probe["streams"][0]
        info = VideoInfo(
            int(stream["width"]),
            int(stream["height"]),
            int(stream["nb_read_frames"]),
            stream["avg_frame_rate"],
        )
    except (
        OSError,
        subprocess.SubprocessError,
        KeyError,
        IndexError,
        TypeError,
        ValueError,
        json.JSONDecodeError,
    ) as error:
        raise HTTPException(
            status_code=422, detail="video could not be decoded"
        ) from error
    if (
        info.width * info.height > MAX_PIXELS
        or not 1 <= info.frame_count <= MAX_VIDEO_FRAMES
    ):
        raise HTTPException(
            status_code=413,
            detail="video exceeds the pixel or 320-frame interactive limit",
        )
    decode_command = [
        "ffmpeg",
        "-nostdin",
        "-v",
        "error",
        "-i",
        str(source),
        "-an",
        "-vsync",
        "0",
        "-q:v",
        "2",
        str(frames / "%06d.jpg"),
    ]
    try:
        subprocess.run(decode_command, check=True, capture_output=True, timeout=120)
    except (OSError, subprocess.SubprocessError) as error:
        raise HTTPException(
            status_code=422, detail="video frames could not be decoded"
        ) from error
    if len(list(frames.glob("*.jpg"))) != info.frame_count:
        raise HTTPException(
            status_code=422, detail="decoded video frame count is inconsistent"
        )
    return frames, info


def _image_result(raw: bytes, request: SegmentRequest) -> bytes:
    assert backend is not None
    image = _decode_image(raw)
    labels, objects = (
        backend.prompted_image(image, request)
        if request.mode == "prompted-image"
        else backend.automatic_image(image, request)
    )
    manifest = {
        "schema": "fs2.nebius.ai/sam2-result/v1",
        "model": MODEL_ID,
        "revision": MODEL_REVISION,
        "checkpoint_sha256": checkpoint_sha256,
        "mode": request.mode,
        "input_sha256": hashlib.sha256(raw).hexdigest(),
        "width": int(image.shape[1]),
        "height": int(image.shape[0]),
        "objects": objects,
    }
    output = io.BytesIO()
    with zipfile.ZipFile(
        output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6
    ) as archive:
        _zip_write(
            archive,
            "manifest.json",
            json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode(),
        )
        _zip_write(archive, "mask.png", _png(labels))
        _zip_write(archive, "overlay.png", _png(_overlay(image, labels)))
    return output.getvalue()


def _video_result(raw: bytes, request: SegmentRequest) -> bytes:
    assert backend is not None
    with tempfile.TemporaryDirectory(prefix="fs2-sam2-video-") as temporary:
        root = Path(temporary)
        frames, info = _video_frames(raw, root)
        labels_by_frame, objects = backend.prompted_video(frames, info, request)
        rendered = root / "rendered"
        masks = root / "masks"
        rendered.mkdir()
        masks.mkdir()
        for index, source in enumerate(sorted(frames.glob("*.jpg"))):
            image = np.asarray(Image.open(source).convert("RGB"))
            labels = labels_by_frame.get(
                index, np.zeros((info.height, info.width), dtype=np.uint16)
            )
            Image.fromarray(_overlay(image, labels)).save(
                rendered / f"{index + 1:06d}.png"
            )
            Image.fromarray(labels).save(masks / f"{index + 1:06d}.png")
        overlay_video = root / "overlay.mp4"
        command = [
            "ffmpeg",
            "-nostdin",
            "-v",
            "error",
            "-framerate",
            info.frame_rate,
            "-i",
            str(rendered / "%06d.png"),
            "-an",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-movflags",
            "+faststart",
            str(overlay_video),
        ]
        try:
            subprocess.run(command, check=True, capture_output=True, timeout=180)
        except (OSError, subprocess.SubprocessError) as error:
            raise HTTPException(
                status_code=500, detail="overlay video encoding failed"
            ) from error
        manifest = {
            "schema": "fs2.nebius.ai/sam2-result/v1",
            "model": MODEL_ID,
            "revision": MODEL_REVISION,
            "checkpoint_sha256": checkpoint_sha256,
            "mode": request.mode,
            "input_sha256": hashlib.sha256(raw).hexdigest(),
            "width": info.width,
            "height": info.height,
            "frame_count": info.frame_count,
            "frame_rate": info.frame_rate,
            "prompt_frame": request.prompt_frame,
            "objects": objects,
        }
        output = io.BytesIO()
        with zipfile.ZipFile(
            output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6
        ) as archive:
            _zip_write(
                archive,
                "manifest.json",
                json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode(),
            )
            _zip_write(archive, "overlay.mp4", overlay_video.read_bytes())
            for path in sorted(masks.glob("*.png")):
                _zip_write(archive, f"masks/{path.name}", path.read_bytes())
        return output.getvalue()


@asynccontextmanager
async def lifespan(_: FastAPI):
    global backend, checkpoint_sha256
    import torch

    if not torch.cuda.is_available():
        raise RuntimeError("CUDA is required for the qualified SAM 2 deployment")
    if not CHECKPOINT_PATH.is_file():
        raise RuntimeError("pinned SAM 2.1 checkpoint is missing")
    checkpoint_sha256 = _sha256(CHECKPOINT_PATH)
    if (
        not EXPECTED_CHECKPOINT_SHA256
        or checkpoint_sha256 != EXPECTED_CHECKPOINT_SHA256
    ):
        raise RuntimeError("SAM 2.1 checkpoint digest differs from the image contract")
    backend = Sam2Backend()
    yield
    backend = None


app = FastAPI(
    title="Nebius Scientific AI SAM 2.1",
    version="1.0.0",
    description="Prompted image/video segmentation and bounded automatic image masks.",
    lifespan=lifespan,
)


@app.get("/v1/health/live")
def live() -> dict[str, str]:
    return {"status": "live", "model": MODEL_ID}


@app.get("/v1/health/ready")
def ready() -> dict[str, str]:
    if backend is None:
        raise HTTPException(status_code=503, detail="model is not ready")
    return {
        "status": "ready",
        "model": MODEL_ID,
        "revision": MODEL_REVISION,
        "checkpoint_sha256": checkpoint_sha256,
    }


@app.post("/v1/segment-track")
async def segment_track(request: SegmentRequest) -> Response:
    if backend is None:
        raise HTTPException(status_code=503, detail="model is not ready")
    raw = _decode(request.media_base64)
    async with inference_lock:
        result = await asyncio.to_thread(
            _video_result if request.mode == "prompted-video" else _image_result,
            raw,
            request,
        )
    return Response(
        result,
        media_type="application/zip",
        headers={
            "X-FS2-Model-Revision": MODEL_REVISION,
            "X-FS2-SAM2-Mode": request.mode,
        },
    )
