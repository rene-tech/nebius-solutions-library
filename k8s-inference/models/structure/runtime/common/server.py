#!/usr/bin/env python3
"""Small bounded HTTP server shared by the exact model adapters."""

from __future__ import annotations

import importlib
import json
import math
import os
import signal
import socket
import threading
import time
import uuid
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, cast

MAX_REQUEST_BYTES = int(os.environ.get("FS2_MAX_REQUEST_BYTES", str(16 * 1024 * 1024)))
HOST = os.environ.get("FS2_HOST", "0.0.0.0")
PORT = int(os.environ.get("FS2_PORT", "8080"))
MODEL = os.environ.get("FS2_MODEL", "")


class ClientError(ValueError):
    """A safe request validation error."""


class RuntimeState:
    def __init__(self, model: str) -> None:
        self.model_name = model
        self.adapter: Any | None = None
        self.load_state = "loading"
        self.load_started = time.monotonic()
        self.load_seconds: float | None = None
        self.active = 0
        self.metrics = {
            "accepted": 0,
            "completed": 0,
            "failed": 0,
            "rejected": 0,
            "request_seconds_sum": 0.0,
            "model_seconds_sum": 0.0,
        }
        self.lock = threading.Lock()
        self.inference_semaphore = threading.BoundedSemaphore(value=1)
        self.backend_id = os.environ.get("HOSTNAME", socket.gethostname())

    def load(self) -> None:
        try:
            if not self.model_name.replace("-", "_").isidentifier():
                raise RuntimeError("invalid FS2_MODEL")
            module = importlib.import_module(
                f"adapters.{self.model_name.replace('-', '_')}"
            )
            adapter = module.Adapter()
            adapter.load()
            self.adapter = adapter
            self.load_state = "ready"
        except Exception as exc:  # keep health available but do not leak exception text
            print(
                json.dumps(
                    {
                        "level": "error",
                        "event": "model_load_failed",
                        "error_type": type(exc).__name__,
                    },
                    sort_keys=True,
                ),
                flush=True,
            )
            self.load_state = "failed"
        finally:
            self.load_seconds = time.monotonic() - self.load_started

    def identity(self) -> dict[str, Any]:
        if self.adapter is None:
            return {
                "model": self.model_name,
                "load_state": self.load_state,
                "routing_state": "disabled",
            }
        value = dict(self.adapter.identity)
        value.update(
            {
                "backend_id": self.backend_id,
                "load_state": self.load_state,
                "load_seconds": self.load_seconds,
                "routing_state": "disabled",
                "public_route": False,
            }
        )
        return value


STATE = RuntimeState(MODEL)


class BoundedHTTPServer(ThreadingHTTPServer):
    # Keep the existing bounded listen backlog while allowing health and
    # observability requests to run independently of long model requests.
    request_queue_size = 8
    max_request_threads = 16
    reserved_non_post_threads = 1
    max_post_threads = max_request_threads - reserved_non_post_threads
    # The previous single-threaded server completed its in-flight request
    # before server_close returned. Preserve that graceful shutdown behavior.
    # Kubernetes still enforces its outer termination grace if inference runs
    # longer than the Pod's shutdown window.
    daemon_threads = False
    block_on_close = True

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self._request_thread_slots = threading.BoundedSemaphore(
            value=self.max_request_threads
        )
        # Do not let queued inference requests consume the final handler slot.
        # That slot keeps health/readiness/metrics responsive during overload.
        self._post_thread_slots = threading.BoundedSemaphore(
            value=self.max_post_threads
        )
        super().__init__(*args, **kwargs)

    def acquire_post_slot(self) -> bool:
        return self._post_thread_slots.acquire(blocking=False)

    def release_post_slot(self) -> None:
        self._post_thread_slots.release()

    def process_request(self, request: Any, client_address: Any) -> None:
        self._request_thread_slots.acquire()
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._request_thread_slots.release()
            raise

    def process_request_thread(self, request: Any, client_address: Any) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._request_thread_slots.release()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "fs2-open-runtime/1"
    sys_version = ""

    def log_message(self, format_string: str, *args: object) -> None:
        print(
            json.dumps(
                {
                    "level": "info",
                    "event": "http_access",
                    "client": self.client_address[0],
                    "message": format_string % args,
                },
                sort_keys=True,
            ),
            flush=True,
        )

    def _send_json(
        self,
        status: HTTPStatus,
        value: dict[str, Any],
        request_id: str | None = None,
    ) -> None:
        payload = json.dumps(
            value, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Backend-Id", STATE.backend_id)
        if request_id:
            self.send_header("X-Request-Id", request_id)
        self.end_headers()
        try:
            self.wfile.write(payload)
        except (BrokenPipeError, ConnectionResetError):
            # A readiness client can time out while an overloaded runtime is
            # still writing its response.  The model request itself is already
            # complete, so avoid turning a disconnected probe into log noise.
            pass

    def _send_text(self, status: HTTPStatus, value: str) -> None:
        payload = value.encode()
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; version=0.0.4")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self) -> None:  # noqa: N802 - stdlib API
        if self.path == "/healthz":
            self._send_json(HTTPStatus.OK, {"status": "alive"})
            return
        if self.path == "/readyz":
            status = (
                HTTPStatus.OK
                if STATE.load_state == "ready"
                else HTTPStatus.SERVICE_UNAVAILABLE
            )
            self._send_json(status, {"status": STATE.load_state})
            return
        if self.path == "/identity":
            self._send_json(HTTPStatus.OK, STATE.identity())
            return
        if self.path == "/metrics":
            self._send_text(HTTPStatus.OK, render_metrics())
            return
        self._send_json(
            HTTPStatus.NOT_FOUND, {"error": {"code": "not_found", "retryable": False}}
        )

    def do_POST(self) -> None:  # noqa: N802 - stdlib API
        request_id = self.headers.get("X-Request-Id") or str(uuid.uuid4())
        runtime_server = cast(BoundedHTTPServer, self.server)
        if not runtime_server.acquire_post_slot():
            with STATE.lock:
                STATE.metrics["rejected"] += 1
            # The request body has not been consumed, so close this connection
            # after the response instead of allowing an invalid keep-alive.
            self.close_connection = True
            self._send_json(
                HTTPStatus.SERVICE_UNAVAILABLE,
                {
                    "request_id": request_id,
                    "error": {"code": "runtime_busy", "retryable": True},
                },
                request_id,
            )
            return
        try:
            self._handle_post(request_id)
        finally:
            runtime_server.release_post_slot()

    def _handle_post(self, request_id: str) -> None:
        adapter = STATE.adapter
        if STATE.load_state != "ready" or adapter is None:
            with STATE.lock:
                STATE.metrics["rejected"] += 1
            self._send_json(
                HTTPStatus.SERVICE_UNAVAILABLE,
                {
                    "request_id": request_id,
                    "error": {"code": "model_not_ready", "retryable": True},
                },
                request_id,
            )
            return
        if self.path not in adapter.paths:
            self._send_json(
                HTTPStatus.NOT_FOUND,
                {
                    "request_id": request_id,
                    "error": {"code": "not_found", "retryable": False},
                },
                request_id,
            )
            return
        content_type = (
            self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
        )
        if content_type != "application/json":
            self._client_error(
                request_id, "content_type", HTTPStatus.UNSUPPORTED_MEDIA_TYPE
            )
            return
        try:
            length = int(self.headers.get("Content-Length", ""))
        except ValueError:
            self._client_error(request_id, "content_length")
            return
        if length <= 0 or length > MAX_REQUEST_BYTES:
            self._client_error(
                request_id, "request_size", HTTPStatus.REQUEST_ENTITY_TOO_LARGE
            )
            return
        raw = self.rfile.read(length)
        try:
            request = json.loads(raw)
            if not isinstance(request, dict):
                raise ClientError("request must be an object")
        except (json.JSONDecodeError, ClientError):
            self._client_error(request_id, "invalid_json")
            return

        started = time.monotonic()
        with STATE.inference_semaphore:
            with STATE.lock:
                STATE.active = 1
                STATE.metrics["accepted"] += 1
            model_started = time.monotonic()
            try:
                output = adapter.infer(request)
                model_seconds = time.monotonic() - model_started
                total_seconds = time.monotonic() - started
                _finite_tree(output)
                response = {
                    "schema": "fs2-serve.nebius.ai/open-runtime-response/v1",
                    "request_id": request_id,
                    "model": adapter.identity["model_id"],
                    "revision": adapter.identity["revision"],
                    "backend_id": STATE.backend_id,
                    "timings": {
                        "model_seconds": model_seconds,
                        "total_seconds": total_seconds,
                    },
                    "output": output,
                }
                with STATE.lock:
                    STATE.metrics["completed"] += 1
                    STATE.metrics["request_seconds_sum"] += total_seconds
                    STATE.metrics["model_seconds_sum"] += model_seconds
                self._send_json(HTTPStatus.OK, response, request_id)
            except (ClientError, ValueError) as exc:
                with STATE.lock:
                    STATE.metrics["rejected"] += 1
                self._send_json(
                    HTTPStatus.BAD_REQUEST,
                    {
                        "request_id": request_id,
                        "error": {
                            "code": "invalid_request",
                            "retryable": False,
                            "message": str(exc)[:160],
                        },
                    },
                    request_id,
                )
            except Exception as exc:
                with STATE.lock:
                    STATE.metrics["failed"] += 1
                print(
                    json.dumps(
                        {
                            "level": "error",
                            "event": "inference_failed",
                            "request_id": request_id,
                            "error_type": type(exc).__name__,
                        },
                        sort_keys=True,
                    ),
                    flush=True,
                )
                self._send_json(
                    HTTPStatus.INTERNAL_SERVER_ERROR,
                    {
                        "request_id": request_id,
                        "error": {"code": "model_failure", "retryable": False},
                    },
                    request_id,
                )
            finally:
                with STATE.lock:
                    STATE.active = 0

    def _client_error(
        self,
        request_id: str,
        code: str,
        status: HTTPStatus = HTTPStatus.BAD_REQUEST,
    ) -> None:
        with STATE.lock:
            STATE.metrics["rejected"] += 1
        self._send_json(
            status,
            {"request_id": request_id, "error": {"code": code, "retryable": False}},
            request_id,
        )


def _finite_tree(value: Any) -> None:
    if isinstance(value, float) and not math.isfinite(value):
        raise RuntimeError("adapter returned a non-finite number")
    if isinstance(value, dict):
        for child in value.values():
            _finite_tree(child)
    elif isinstance(value, list):
        for child in value:
            _finite_tree(child)


def render_metrics() -> str:
    with STATE.lock:
        values = dict(STATE.metrics)
        active = STATE.active
    ready = int(STATE.load_state == "ready")
    lines = [
        "# TYPE fs2_model_ready gauge",
        f'fs2_model_ready{{model="{STATE.model_name}"}} {ready}',
        "# TYPE fs2_active_requests gauge",
        f'fs2_active_requests{{model="{STATE.model_name}"}} {active}',
    ]
    for name in ("accepted", "completed", "failed", "rejected"):
        lines.extend(
            [
                f"# TYPE fs2_requests_{name}_total counter",
                f'fs2_requests_{name}_total{{model="{STATE.model_name}"}} {values[name]}',
            ]
        )
    lines.extend(
        [
            "# TYPE fs2_request_seconds_sum counter",
            f'fs2_request_seconds_sum{{model="{STATE.model_name}"}} {values["request_seconds_sum"]:.9f}',
            "# TYPE fs2_model_seconds_sum counter",
            f'fs2_model_seconds_sum{{model="{STATE.model_name}"}} {values["model_seconds_sum"]:.9f}',
        ]
    )
    return "\n".join(lines) + "\n"


def main() -> int:
    if not MODEL:
        raise SystemExit("FS2_MODEL is required")
    server = BoundedHTTPServer((HOST, PORT), Handler)
    loader = threading.Thread(target=STATE.load, name="model-loader", daemon=True)
    loader.start()

    def stop(_signum: int, _frame: Any) -> None:
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    print(
        json.dumps({"event": "server_started", "model": MODEL, "port": PORT}),
        flush=True,
    )
    try:
        server.serve_forever(poll_interval=0.2)
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
