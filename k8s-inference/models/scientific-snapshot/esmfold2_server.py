#!/usr/bin/env python3
"""Request-ready ESMFold2 process for the isolated persistent snapshot lane.

Load fixed runtime artifacts once; consume sequences and sampling settings only
after readiness. This process deliberately has no per-user state at capture.
The scientific controller remains responsible for artifact authorization.
"""

from __future__ import annotations

import hashlib
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import math
import os
import re
import time
import sys


def main() -> None:
    started = time.monotonic()
    os.environ.setdefault("ESMCFOLD_CCD_PATH", "/databases/esmfold2/ccd.pkl")
    import torch
    from esm.models.esmc import EsmcModel
    from esm.models.esmfold2 import ESMFold2InputBuilder, EsmFold2Model
    import esm.models.esmfold2.layers as esmfold2_layers
    from esm.utils.structure.input_builder import deserialize_structure_prediction_input

    model_dir = os.environ.get("FS2_SNAPSHOT_MODEL_DIR", "/models/esmfold2")
    esmc_dir = os.environ.get("FS2_ESMC_MODEL_DIR", "/models/esmc-6b")
    attention = os.environ.get(
        "FS2_SNAPSHOT_ATTENTION", "flash_attention_2" if torch.cuda.get_device_capability(0) == (9, 0) else "sdpa"
    )
    precision = os.environ.get("FS2_SNAPSHOT_ESMC_PRECISION", "bf16")
    if attention != "flash_attention_2":
        esmfold2_layers.FLASH_ATTN_AVAILABLE = False
    model = EsmFold2Model.from_pretrained(model_dir, load_esmc=False, device="cuda").eval()
    model.esmc = EsmcModel.from_pretrained(
        esmc_dir, device="cuda", attn_implementation=attention,
    )
    model.set_esmc_precision(precision)
    model._fs2_snapshot_binding = {
        "model_dir": model_dir, "esmc_dir": esmc_dir,
        "ccd_path": os.environ["ESMCFOLD_CCD_PATH"], "esmc_precision": precision,
        "attention": attention, "runtime_id": os.environ.get("FS2_RUNTIME_ID", ""),
    }
    torch.cuda.synchronize()
    readiness = {
        "ready": True, "pid": os.getpid(), "load_seconds": time.monotonic() - started,
        "gpu": torch.cuda.get_device_name(0), "cuda": torch.version.cuda,
        "torch": torch.__version__, "allocated_bytes": torch.cuda.memory_allocated(),
    }

    class Handler(BaseHTTPRequestHandler):
        def respond(self, code: int, document: dict) -> None:
            payload = json.dumps(document).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def do_GET(self) -> None:
            if self.path == "/model-state":
                digest = hashlib.sha256()
                total_bytes = 0
                tensors = model.state_dict()
                for name, tensor in sorted(tensors.items()):
                    digest.update(json.dumps([name, str(tensor.dtype), list(tensor.shape)]).encode())
                    data = tensor.detach().contiguous().view(torch.uint8).cpu().numpy()
                    digest.update(memoryview(data))
                    total_bytes += data.nbytes
                self.respond(200, {
                    "sha256": digest.hexdigest(), "tensor_count": len(tensors), "bytes": total_bytes,
                })
                return
            self.respond(200 if self.path == "/health" else 404, readiness if self.path == "/health" else {})

        def do_POST(self) -> None:
            if self.path == "/execute":
                # Run the existing scientific wrapper in this already-loaded
                # interpreter. It owns immutable input checks and collectors'
                # ordinary output envelope; only model loading is bypassed.
                from contextlib import redirect_stderr, redirect_stdout
                from io import StringIO

                output, errors = StringIO(), StringIO()
                exit_code = 0
                try:
                    request = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
                    arguments = request["argv"]
                    if not isinstance(arguments, list) or not arguments or arguments[0] != "fold":
                        raise ValueError("snapshot worker executes the scientific fold stage only")
                    if not all(isinstance(argument, str) for argument in arguments):
                        raise ValueError("scientific stage arguments must be strings")
                    if "/opt/fs2" not in sys.path:
                        sys.path.insert(0, "/opt/fs2")
                    import run_esmfold2

                    environment = request.get("environment", {})
                    if not isinstance(environment, dict) or any(
                        name not in run_esmfold2.REQUEST_ENVIRONMENT or not isinstance(value, str)
                        for name, value in environment.items()
                    ):
                        raise ValueError("scientific request environment differs from its supported contract")
                    previous = {name: os.environ.get(name) for name in run_esmfold2.REQUEST_ENVIRONMENT}
                    try:
                        for name in run_esmfold2.REQUEST_ENVIRONMENT:
                            os.environ.pop(name, None)
                        os.environ.update(environment)
                        with redirect_stdout(output), redirect_stderr(errors):
                            run_esmfold2.main(arguments, preloaded_model=model)
                    finally:
                        for name, value in previous.items():
                            if value is None:
                                os.environ.pop(name, None)
                            else:
                                os.environ[name] = value
                except SystemExit as error:
                    exit_code = error.code if isinstance(error.code, int) else 1
                    if not isinstance(error.code, int):
                        errors.write(str(error.code) + "\n")
                except Exception as error:
                    exit_code = 1
                    errors.write(str(error) + "\n")
                self.respond(200, {"exit_code": exit_code, "stdout": output.getvalue(), "stderr": errors.getvalue()})
                return
            if self.path == "/prepare-snapshot":
                # HTTPServer serializes requests. Release unused allocator
                # blocks only after the preceding fold has returned; never
                # change live tensors, precision, seed or scientific options.
                import gc

                torch.cuda.synchronize()
                before = torch.cuda.memory_reserved()
                gc.collect()
                torch.cuda.empty_cache()
                torch.cuda.synchronize()
                self.respond(200, {
                    "allocated_bytes": torch.cuda.memory_allocated(),
                    "reserved_before_bytes": before,
                    "reserved_after_bytes": torch.cuda.memory_reserved(),
                })
                return
            if self.path != "/fold":
                self.respond(404, {})
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 1 <= length <= 1024 * 1024:
                    raise ValueError("request length is invalid")
                request = json.loads(self.rfile.read(length))
                sequence = request["sequence"]
                if not isinstance(sequence, str) or not re.fullmatch(r"[ACDEFGHIKLMNPQRSTVWY]{1,4096}", sequence):
                    raise ValueError("sequence must contain protein amino acids")
                loops = int(request.get("num_loops", 20))
                steps = int(request.get("num_sampling_steps", 200))
                if not 1 <= loops <= 100 or not 1 <= steps <= 1000:
                    raise ValueError("sampling settings are invalid")
                seed = int(request.get("seed", 0))
            except (ValueError, TypeError, KeyError) as error:
                self.respond(400, {"error": str(error)})
                return
            started = time.monotonic()
            prepared = deserialize_structure_prediction_input({
                "sequences": [{"type": "protein", "id": "A", "sequence": sequence, "msa": None}]
            })
            result = ESMFold2InputBuilder().fold(
                model, prepared, num_loops=loops, num_sampling_steps=steps,
                num_diffusion_samples=1, seed=seed, lm_dropout=0.0, complex_id="fs2-snapshot",
            )
            torch.cuda.synchronize()
            cif = result.complex.to_mmcif()
            confidence = float(result.plddt.detach().float().mean().cpu())
            atoms = len(re.findall(r"^(?:ATOM|HETATM)\s", cif, re.MULTILINE))
            if not math.isfinite(confidence) or not 0 <= confidence <= 1 or atoms < len(sequence) * 3:
                self.respond(500, {"error": "invalid scientific output"})
                return
            self.respond(200, {
                "cif": cif, "cif_sha256": hashlib.sha256(cif.encode()).hexdigest(),
                "sequence_sha256": hashlib.sha256(sequence.encode()).hexdigest(),
                "sequence_length": len(sequence), "atoms": atoms, "plddt_mean": confidence,
                "inference_seconds": time.monotonic() - started,
            })

    HTTPServer(("0.0.0.0", int(os.environ.get("PORT", "8000"))), Handler).serve_forever()


if __name__ == "__main__":
    main()
