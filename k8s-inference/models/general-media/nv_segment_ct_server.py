#!/usr/bin/env python3
"""Persistent non-clinical NV-Segment-CT HTTP service."""

from __future__ import annotations

import base64
import json
import os
import socket
import sys
import tempfile
import threading
import time
import uuid
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import nibabel as nib
import numpy as np
import torch
from huggingface_hub import snapshot_download

MODEL_ID = os.environ.get("FS2_MODEL_ID", "nv-segment-ct")
MODEL_REPOSITORY = os.environ.get("FS2_MODEL_REPOSITORY", "nvidia/NV-Segment-CT")
MODEL_REVISION = os.environ.get(
    "FS2_MODEL_REVISION", "afb51518689f71e6abb367ee6301b2cd0225c66a"
)
CACHE_DIR = os.environ.get("HF_HOME", "/model-cache")
PORT = int(os.environ.get("PORT", "8000"))
MAX_BODY_BYTES = 48 * 1024 * 1024
MAX_INPUT_BYTES = 32 * 1024 * 1024
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
        snapshot = snapshot_download(
            repo_id=MODEL_REPOSITORY,
            revision=MODEL_REVISION,
            cache_dir=CACHE_DIR,
        )
        self.snapshot = Path(snapshot)
        sys.path.insert(0, str(self.snapshot))
        from hugging_face_pipeline import HuggingFacePipelineHelper

        helper = HuggingFacePipelineHelper("vista3d")
        self.pipeline = helper.init_pipeline(
            str(self.snapshot / "vista3d_pretrained_model"),
            device=torch.device("cuda:0"),
            metadata_path=str(self.snapshot / "metadata.json"),
        )
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

    def segment(self, payload: dict[str, Any]) -> dict[str, Any]:
        encoded = payload.get("input_nifti_base64")
        if not isinstance(encoded, str) or not encoded:
            raise ValueError("input_nifti_base64 must be a non-empty base64 string")
        try:
            nifti_bytes = base64.b64decode(encoded, validate=True)
        except ValueError as error:
            raise ValueError("input_nifti_base64 is not valid base64") from error
        if not nifti_bytes or len(nifti_bytes) > MAX_INPUT_BYTES:
            raise ValueError("decoded NIfTI size is invalid")
        label_prompt = payload.get("label_prompt")
        points = payload.get("points")
        point_labels = payload.get("point_labels")
        if label_prompt is None and points is None:
            raise ValueError("label_prompt or points must be provided")
        request: dict[str, Any] = {}
        if label_prompt is not None:
            if not isinstance(label_prompt, list) or not label_prompt:
                raise ValueError("label_prompt must be a non-empty list")
            request["label_prompt"] = [int(value) for value in label_prompt]
        if points is not None:
            if not isinstance(points, list) or not points:
                raise ValueError("points must be a non-empty list")
            request["points"] = points
            if not isinstance(point_labels, list) or len(point_labels) != len(points):
                raise ValueError("point_labels must match points")
            request["point_labels"] = point_labels

        with tempfile.TemporaryDirectory(prefix="fs2-segment-") as temporary:
            temporary_path = Path(temporary)
            input_path = temporary_path / "input.nii.gz"
            output_path = temporary_path / "output"
            input_path.write_bytes(nifti_bytes)
            input_image = nib.load(str(input_path))
            input_shape = tuple(int(value) for value in input_image.shape)
            if len(input_shape) != 3 or any(
                value < 8 or value > 512 for value in input_shape
            ):
                raise ValueError("NIfTI must be a bounded three-dimensional volume")
            input_data = np.asanyarray(input_image.dataobj)
            if not np.isfinite(input_data).all():
                raise ValueError("NIfTI contains non-finite values")
            request["image"] = str(input_path)
            output_path.mkdir()
            started = time.monotonic()
            with self.lock, torch.inference_mode():
                self.pipeline([request], output_dir=str(output_path))
            model_seconds = time.monotonic() - started
            candidates = sorted(output_path.rglob("*.nii.gz"))
            if not candidates:
                raise RuntimeError("pipeline did not produce a NIfTI result")
            result_path = candidates[0]
            result_image = nib.load(str(result_path))
            result_data = np.asanyarray(result_image.dataobj)
            if result_data.shape != input_data.shape:
                raise RuntimeError("segmentation shape does not match input")
            if not np.isfinite(result_data).all():
                raise RuntimeError("segmentation contains non-finite values")
            unique, counts = np.unique(result_data.astype(np.int32), return_counts=True)
            labels = {
                str(int(label)): int(count)
                for label, count in zip(unique, counts, strict=True)
            }
            if len(labels) < 1:
                raise RuntimeError("segmentation contains no labels")
            result_bytes = result_path.read_bytes()
            self.model_seconds += model_seconds
            return {
                "model": MODEL_ID,
                "repository": MODEL_REPOSITORY,
                "revision": MODEL_REVISION,
                "non_clinical": True,
                "mime_type": "application/gzip",
                "shape": list(result_data.shape),
                "labels": labels,
                "output_bytes": len(result_bytes),
                "output_nifti_base64": base64.b64encode(result_bytes).decode(),
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
    server_version = "fs2-nv-segment-ct/1"

    def log_message(self, format: str, *args: Any) -> None:
        print(f"http {self.address_string()} {format % args}", flush=True)

    def _send_json(self, status: int, body: dict[str, Any]) -> None:
        encoded = json.dumps(body, sort_keys=True).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("X-Backend-Id", BACKEND_ID)
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
                    "non_clinical": True,
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
        if self.path != "/segment":
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        request_id = str(uuid.uuid4())
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
            RUNTIME.accepted += 1
            result = RUNTIME.segment(payload)
            RUNTIME.completed += 1
            result["request_id"] = request_id
            self._send_json(HTTPStatus.OK, result)
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
                    {"event": "segmentation-failed", "type": type(error).__name__}
                ),
                flush=True,
            )
            self._send_json(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                {
                    "error": "segmentation_failed",
                    "request_id": request_id,
                    "retryable": True,
                },
            )
        finally:
            RUNTIME.admission.release()


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
