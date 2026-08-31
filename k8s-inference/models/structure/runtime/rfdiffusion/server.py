#!/usr/bin/env python3
"""Bounded HTTP adapter for the exact upstream RFdiffusion revision."""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import tempfile
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


SOURCE_REVISION = "86507b6538f51fce57b5a72477165f03999ed7ae"
CHECKPOINT_SHA256 = "0fcf7d7c32b4848030aca3a051e6768de194616f96ba6c38186351a33bfc6eca"
CHECKPOINT = Path("/opt/fs2/models/Base_ckpt.pt")
SOURCE = Path("/opt/fs2/rfdiffusion")
SCHEDULES = Path("/tmp/fs2-rf-schedules")
MAX_BODY_BYTES = 4 * 1024 * 1024
CONTIG = re.compile(r"^[A-Za-z0-9/ .,_-]{1,256}$")
INFERENCE_LOCK = threading.Lock()
STARTED = time.monotonic()
IDENTITY: dict[str, Any] = {
    "model_id": "RosettaCommons/RFdiffusion",
    "candidate_id": "rfdiffusion-upstream-blackwell-sm103",
    "revision": SOURCE_REVISION,
    "checkpoint": "Base_ckpt.pt",
    "checkpoint_sha256": CHECKPOINT_SHA256,
    "license": "BSD-3-Clause",
    "relationship": "exact-upstream-direct-profile-independent",
    "vendor_nim_state": "incompatible-sm103",
    "routing_state": "disabled",
    "public_route": False,
}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _preflight() -> None:
    if not (SOURCE / "scripts/run_inference.py").is_file():
        raise RuntimeError("exact upstream source is absent")
    if _sha256(CHECKPOINT) != CHECKPOINT_SHA256:
        raise RuntimeError("RFdiffusion checkpoint digest mismatch")
    SCHEDULES.mkdir(parents=True, exist_ok=True)
    Path(os.environ["FS2_RUNTIME_HOME"]).mkdir(parents=True, exist_ok=True)
    Path(os.environ["XDG_CACHE_HOME"]).mkdir(parents=True, exist_ok=True)
    Path(os.environ["XDG_CONFIG_HOME"]).mkdir(parents=True, exist_ok=True)
    Path(os.environ["MPLCONFIGDIR"]).mkdir(parents=True, exist_ok=True)
    completed = subprocess.run(
        [
            "python3",
            "-c",
            (
                "import torch,dgl; "
                "from rfdiffusion.RoseTTAFoldModel import RoseTTAFoldModule; "
                "assert torch.cuda.is_available(); "
                "print(torch.__version__,torch.version.cuda,torch.cuda.get_device_capability())"
            ),
        ],
        cwd=SOURCE,
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        timeout=120,
    )
    if completed.returncode != 0:
        raise RuntimeError("RFdiffusion CUDA/import preflight failed: " + completed.stdout[-1200:])
    IDENTITY["cuda_preflight_sha256"] = hashlib.sha256(completed.stdout.encode()).hexdigest()
    IDENTITY["load_state"] = "ready"
    IDENTITY["load_seconds"] = round(time.monotonic() - STARTED, 6)


def _run(payload: dict[str, Any]) -> dict[str, Any]:
    input_pdb = payload.get("input_pdb")
    contigs = payload.get("contigs")
    steps = payload.get("diffusion_steps")
    seed = payload.get("random_seed")
    if not isinstance(input_pdb, str) or not input_pdb.startswith(("ATOM", "HEADER")):
        raise ValueError("input_pdb must contain PDB text")
    if not isinstance(contigs, str) or CONTIG.fullmatch(contigs) is None:
        raise ValueError("contigs has unsupported syntax")
    if isinstance(steps, bool) or not isinstance(steps, int) or not 1 <= steps <= 100:
        raise ValueError("diffusion_steps must be an integer from 1 to 100")
    if isinstance(seed, bool) or not isinstance(seed, int) or not 0 <= seed <= 2_147_483_647:
        raise ValueError("random_seed must be a nonnegative 32-bit integer")

    started = time.monotonic()
    with INFERENCE_LOCK, tempfile.TemporaryDirectory(prefix="fs2-rfdiffusion-") as tmp_name:
        tmp = Path(tmp_name)
        input_path = tmp / "input.pdb"
        prefix = tmp / "design"
        input_path.write_text(input_pdb, encoding="ascii")
        command = [
            "python3",
            "/opt/fs2/runtime/run_upstream.py",
            f"inference.output_prefix={prefix}",
            f"inference.model_directory_path={CHECKPOINT.parent}",
            f"inference.schedule_directory_path={SCHEDULES}",
            f"inference.input_pdb={input_path}",
            "inference.num_designs=1",
            f"inference.design_startnum={seed}",
            "inference.deterministic=True",
            "inference.write_trajectory=False",
            "inference.cautious=False",
            f"diffuser.T={steps}",
            f"contigmap.contigs=[{contigs}]",
            "hydra.job.chdir=False",
            f"hydra.run.dir={tmp / 'hydra'}",
        ]
        completed = subprocess.run(
            command,
            cwd=SOURCE,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=float(os.environ.get("FS2_RF_TIMEOUT_SECONDS", "900")),
        )
        if completed.returncode != 0:
            log_hash = hashlib.sha256(completed.stdout.encode()).hexdigest()
            raise RuntimeError(f"upstream inference failed; log_sha256={log_hash}")
        output_path = Path(f"{prefix}_{seed}.pdb")
        if not output_path.is_file():
            raise RuntimeError("upstream inference did not create a PDB")
        output_pdb = output_path.read_text(encoding="ascii")
    return {
        "output_pdb": output_pdb,
        "elapsed_ms": round((time.monotonic() - started) * 1000.0, 3),
        "model_revision": SOURCE_REVISION,
        "checkpoint_sha256": CHECKPOINT_SHA256,
    }


class Handler(BaseHTTPRequestHandler):
    server_version = "fs2-rfdiffusion-upstream/1"

    def _json(self, status: int, value: Any) -> None:
        body = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Model-Revision", SOURCE_REVISION)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        if self.path in {"/v1/health/ready", "/v1/health/live", "/readyz", "/healthz"}:
            self._json(HTTPStatus.OK, {"status": "ready"})
        elif self.path == "/identity":
            self._json(HTTPStatus.OK, IDENTITY)
        else:
            self._json(HTTPStatus.NOT_FOUND, {"error": "not_found"})

    def do_POST(self) -> None:  # noqa: N802
        if self.path not in {"/biology/ipd/rfdiffusion/generate", "/v1/infer"}:
            self._json(HTTPStatus.NOT_FOUND, {"error": "not_found"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= MAX_BODY_BYTES:
                raise ValueError("invalid request size")
            payload = json.loads(self.rfile.read(length))
            if not isinstance(payload, dict):
                raise ValueError("request must be a JSON object")
            response = _run(payload)
        except (UnicodeError, json.JSONDecodeError, ValueError) as exc:
            self._json(HTTPStatus.BAD_REQUEST, {"error": type(exc).__name__, "detail": str(exc)[:200]})
        except subprocess.TimeoutExpired:
            self._json(HTTPStatus.GATEWAY_TIMEOUT, {"error": "inference_timeout"})
        except Exception as exc:  # retain typed bounded failures without raw model input
            self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": type(exc).__name__, "detail": str(exc)[:240]})
        else:
            self._json(HTTPStatus.OK, response)

    def log_message(self, fmt: str, *args: Any) -> None:
        print(json.dumps({"event": "http_access", "message": fmt % args}), flush=True)


def main() -> None:
    _preflight()
    port = int(os.environ.get("FS2_PORT", "8000"))
    print(json.dumps({"event": "server_started", "port": port, "identity": IDENTITY}), flush=True)
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()


if __name__ == "__main__":
    main()
