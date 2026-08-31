#!/usr/bin/env python3
"""Persistent, bounded SDXL service used by the FS2 model catalog."""

from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import socket
import threading
import time
import uuid
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import torch
from diffusers import StableDiffusionXLPipeline
from PIL import Image, ImageStat

MODEL_ID = os.environ.get("FS2_MODEL_ID", "sdxl")
MODEL_REPOSITORY = os.environ.get(
    "FS2_MODEL_REPOSITORY", "stabilityai/stable-diffusion-xl-base-1.0"
)
MODEL_REVISION = os.environ.get(
    "FS2_MODEL_REVISION", "462165984030d82259a11f4367a4eed129e94a7b"
)
CACHE_DIR = os.environ.get("HF_HOME", "/model-cache")
PORT = int(os.environ.get("PORT", "8000"))
MAX_BODY_BYTES = 1_048_576
BACKEND_ID = os.environ.get("POD_NAME", socket.gethostname())


class Runtime:
    def __init__(self) -> None:
        self.started_at = time.monotonic()
        self.ready = False
        self.lock = threading.Lock()
        self.admission = threading.BoundedSemaphore(value=1)
        self.accepted = 0
        self.completed = 0
        self.failed = 0
        self.rejected = 0
        self.model_seconds = 0.0
        self.pipeline = StableDiffusionXLPipeline.from_pretrained(
            MODEL_REPOSITORY,
            revision=MODEL_REVISION,
            cache_dir=CACHE_DIR,
            torch_dtype=torch.float16,
            use_safetensors=True,
            variant="fp16",
        )
        self.pipeline.to("cuda")
        self.pipeline.set_progress_bar_config(disable=True)
        self.ready = True
        print(
            json.dumps(
                {
                    "event": "model-ready",
                    "model_id": MODEL_ID,
                    "repository": MODEL_REPOSITORY,
                    "revision": MODEL_REVISION,
                    "backend_id": BACKEND_ID,
                    "gpu_name": torch.cuda.get_device_name(0),
                    "compute_capability": list(torch.cuda.get_device_capability(0)),
                    "torch": torch.__version__,
                    "cuda": torch.version.cuda,
                    "startup_seconds": time.monotonic() - self.started_at,
                },
                sort_keys=True,
            ),
            flush=True,
        )

    def generate(self, payload: dict[str, Any]) -> tuple[bytes, dict[str, Any]]:
        prompt = payload.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip():
            raise ValueError("prompt must be a non-empty string")
        negative_prompt = payload.get("negative_prompt")
        if negative_prompt is not None and not isinstance(negative_prompt, str):
            raise ValueError("negative_prompt must be a string")
        seed = int(payload.get("seed", 0))
        steps = int(payload.get("steps", 20))
        guidance = float(payload.get("guidance", payload.get("guidance_scale", 5.0)))
        width = int(payload.get("width", 512))
        height = int(payload.get("height", 512))
        if not 1 <= steps <= 50:
            raise ValueError("steps must be in 1..50")
        if width != 512 or height != 512:
            raise ValueError("this qualified endpoint currently accepts 512x512 only")
        if not 0.0 <= guidance <= 20.0:
            raise ValueError("guidance must be in 0..20")

        generator = torch.Generator(device="cuda").manual_seed(seed)
        started = time.monotonic()
        with self.lock, torch.inference_mode():
            image = self.pipeline(
                prompt=prompt,
                negative_prompt=negative_prompt,
                num_inference_steps=steps,
                guidance_scale=guidance,
                width=width,
                height=height,
                generator=generator,
            ).images[0]
        model_seconds = time.monotonic() - started
        if not isinstance(image, Image.Image) or image.size != (width, height):
            raise RuntimeError("pipeline returned an invalid image")
        extrema = ImageStat.Stat(image.convert("RGB")).extrema
        if not any(high > low for low, high in extrema):
            raise RuntimeError("pipeline returned a constant image")
        output = io.BytesIO()
        image.save(output, format="PNG")
        result = output.getvalue()
        if not result.startswith(b"\x89PNG\r\n\x1a\n"):
            raise RuntimeError("PNG encoding failed")
        self.model_seconds += model_seconds
        return result, {
            "seed": seed,
            "steps": steps,
            "guidance": guidance,
            "width": width,
            "height": height,
            "model_seconds": model_seconds,
        }

    def metrics(self) -> bytes:
        ready = 1 if self.ready else 0
        lines = [
            "# TYPE fs2_model_ready gauge",
            f'fs2_model_ready{{model="{MODEL_ID}"}} {ready}',
            "# TYPE fs2_requests_total counter",
            f'fs2_requests_total{{model="{MODEL_ID}",outcome="accepted"}} {self.accepted}',
            f'fs2_requests_total{{model="{MODEL_ID}",outcome="completed"}} {self.completed}',
            f'fs2_requests_total{{model="{MODEL_ID}",outcome="failed"}} {self.failed}',
            f'fs2_requests_total{{model="{MODEL_ID}",outcome="rejected"}} {self.rejected}',
            "# TYPE fs2_model_seconds_total counter",
            f'fs2_model_seconds_total{{model="{MODEL_ID}"}} {self.model_seconds:.6f}',
        ]
        return ("\n".join(lines) + "\n").encode()


RUNTIME = Runtime()


class Handler(BaseHTTPRequestHandler):
    server_version = "fs2-sdxl/1"

    def log_message(self, format: str, *args: Any) -> None:
        print(f"http {self.address_string()} {format % args}", flush=True)

    def _send_json(
        self,
        status: int,
        body: dict[str, Any],
        headers: dict[str, str] | None = None,
    ) -> None:
        encoded = json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("X-Backend-Id", BACKEND_ID)
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(encoded)

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/healthz":
            self._send_json(HTTPStatus.OK, {"ok": True})
            return
        if self.path == "/readyz":
            status = HTTPStatus.OK if RUNTIME.ready else HTTPStatus.SERVICE_UNAVAILABLE
            self._send_json(
                status,
                {
                    "ready": RUNTIME.ready,
                    "model": MODEL_ID,
                    "repository": MODEL_REPOSITORY,
                    "revision": MODEL_REVISION,
                },
            )
            return
        if self.path == "/metrics":
            encoded = RUNTIME.metrics()
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/plain; version=0.0.4")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)
            return
        self.send_error(HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:  # noqa: N802
        if self.path != "/generate":
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        operation_id = self.headers.get("X-FS2-Operation-ID", "")
        request_id = (
            operation_id
            if operation_id
            and len(operation_id) <= 128
            and all(0x20 <= ord(character) < 0x7F for character in operation_id)
            else str(uuid.uuid4())
        )
        if not RUNTIME.admission.acquire(blocking=False):
            RUNTIME.rejected += 1
            self._send_json(
                HTTPStatus.TOO_MANY_REQUESTS,
                {"error": "queue_full", "request_id": request_id, "retryable": True},
            )
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > MAX_BODY_BYTES:
                raise ValueError("request body size is invalid")
            payload = json.loads(self.rfile.read(length))
            if not isinstance(payload, dict):
                raise ValueError("request body must be a JSON object")
            response_format = payload.get("response_format", "image/png")
            if response_format not in {"image/png", "b64_json"}:
                raise ValueError("response_format must be image/png or b64_json")
            RUNTIME.accepted += 1
            png, effective = RUNTIME.generate(payload)
            RUNTIME.completed += 1
            common_headers = {
                "X-Request-Id": request_id,
                "X-Model-Id": MODEL_ID,
                "X-Model-Revision": MODEL_REVISION,
                "X-Effective-Seed": str(effective["seed"]),
                "X-Effective-Steps": str(effective["steps"]),
                "X-Model-Seconds": f"{effective['model_seconds']:.6f}",
            }
            if response_format == "b64_json":
                self._send_json(
                    HTTPStatus.OK,
                    {
                        "backend_id": BACKEND_ID,
                        "data": [{"b64_json": base64.b64encode(png).decode("ascii")}],
                        "guidance": effective["guidance"],
                        "height": effective["height"],
                        "mime_type": "image/png",
                        "model": MODEL_ID,
                        "png_bytes": len(png),
                        "png_sha256": hashlib.sha256(png).hexdigest(),
                        "repository": MODEL_REPOSITORY,
                        "request_id": request_id,
                        "revision": MODEL_REVISION,
                        "seed": effective["seed"],
                        "steps": effective["steps"],
                        "width": effective["width"],
                    },
                    headers=common_headers,
                )
                return
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(png)))
            self.send_header("X-Backend-Id", BACKEND_ID)
            for name, value in common_headers.items():
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(png)
        except (ValueError, TypeError, json.JSONDecodeError) as error:
            RUNTIME.failed += 1
            self._send_json(
                HTTPStatus.BAD_REQUEST,
                {
                    "error": "invalid_request",
                    "message": str(error),
                    "request_id": request_id,
                },
            )
        except Exception as error:  # noqa: BLE001
            RUNTIME.failed += 1
            print(
                json.dumps(
                    {"event": "generation-failed", "type": type(error).__name__}
                ),
                flush=True,
            )
            self._send_json(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                {
                    "error": "generation_failed",
                    "request_id": request_id,
                    "retryable": True,
                },
            )
        finally:
            RUNTIME.admission.release()


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
