"""Persistent exact OpenFold2 adapter backed by upstream model parameters.

This runtime is independent of NVIDIA NIM. It uses the pinned OpenFold source
and the exact ``finetuning_no_templ_ptm_1`` checkpoint with the documented
``model_3_ptm`` configuration. The request contract supplies no external MSA
or template, so inference uses the upstream one-sequence MSA representation and
does not substitute another model or synthetic structure.
"""

from __future__ import annotations

import datetime as dt
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import math
import os
from pathlib import Path
import re
import threading
import time
import traceback
from typing import Any


CHECKPOINT = Path(
    os.environ.get(
        "FS2_OPENFOLD2_CHECKPOINT",
        "/opt/fs2/openfold2-artifacts/finetuning_no_templ_ptm_1.pt",
    )
)
CHECKPOINT_SHA256 = "1a179937a6f61143c41f8b6a0b763ac1f4762c9dc63d2fb151240930c110bee1"
SOURCE_REVISION = "be2ec1841f16c966c65ae0e7599ebbadc725757d"
MODEL_REVISION = "6344b2a680cbbae20b8afdb7f154228cc5b0cb27"
ENDPOINT = "/biology/openfold/openfold2/predict-structure-from-msa-and-template"
IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")
SEQUENCE = re.compile(r"[ACDEFGHIKLMNPQRSTVWY]+")
MAX_REQUEST_BYTES = 64 * 1024


def event(phase: str, **fields: object) -> None:
    print(
        json.dumps(
            {
                "phase": phase,
                "time": dt.datetime.now(dt.timezone.utc).isoformat(),
                **fields,
            },
            sort_keys=True,
            allow_nan=False,
        ),
        flush=True,
    )


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(8 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def parse_request(value: object) -> tuple[str, str]:
    if not isinstance(value, dict) or set(value) != {
        "input_id",
        "relax_prediction",
        "selected_models",
        "sequence",
    }:
        raise ValueError(
            "expected input_id, sequence, selected_models and relax_prediction"
        )
    input_id = value["input_id"]
    sequence = value["sequence"]
    if not isinstance(input_id, str) or IDENTIFIER.fullmatch(input_id) is None:
        raise ValueError("input_id is not a valid bounded identifier")
    if (
        not isinstance(sequence, str)
        or not 1 <= len(sequence) <= 1024
        or SEQUENCE.fullmatch(sequence) is None
    ):
        raise ValueError("sequence must contain 1-1024 canonical amino acids")
    if value["selected_models"] != [1]:
        raise ValueError("this exact runtime exposes OpenFold parameter set 1 only")
    if value["relax_prediction"] is not False:
        raise ValueError("the qualified runtime requires relax_prediction=false")
    return input_id, sequence


def to_numpy(value: Any) -> Any:
    import torch

    if isinstance(value, torch.Tensor):
        return value.detach().cpu().numpy()
    if isinstance(value, dict):
        return {key: to_numpy(item) for key, item in value.items()}
    if isinstance(value, list):
        return [to_numpy(item) for item in value]
    if isinstance(value, tuple):
        return tuple(to_numpy(item) for item in value)
    return value


class Runtime:
    def __init__(self) -> None:
        self.ready = False
        self.error: str | None = None
        self.identity: dict[str, Any] = {}
        self.lock = threading.Lock()

    def load(self) -> None:
        started = time.monotonic()
        try:
            event("ARTIFACT_VERIFY_BEGIN", checkpoint=CHECKPOINT.name)
            if file_sha256(CHECKPOINT) != CHECKPOINT_SHA256:
                raise RuntimeError("OpenFold2 checkpoint digest mismatch")

            import torch
            from openfold.config import model_config
            from openfold.model.model import AlphaFold
            from openfold.utils.import_weights import import_openfold_weights_

            if not torch.cuda.is_available():
                raise RuntimeError("OpenFold2 requires a CUDA device")
            torch.set_grad_enabled(False)
            torch.set_float32_matmul_precision("high")
            event("MODEL_BUILD_BEGIN", config="model_3_ptm")
            self.config = model_config("model_3_ptm")
            model = AlphaFold(self.config).eval()
            state = torch.load(CHECKPOINT, map_location="cpu", weights_only=False)
            if isinstance(state, dict) and set(state) == {"ema"}:
                state = state["ema"]["params"]
            if not isinstance(state, dict):
                raise RuntimeError("OpenFold2 checkpoint is not a state dictionary")
            import_openfold_weights_(model=model, state_dict=state)
            self.model = model.to("cuda").eval()
            torch.cuda.synchronize()
            self.identity = {
                "runtime_origin": "upstream-source-not-nim",
                "source_repository": "github.com/aqlaboratory/openfold",
                "source_revision": SOURCE_REVISION,
                "model_repository": "nz/OpenFold",
                "model_revision": MODEL_REVISION,
                "checkpoint": CHECKPOINT.name,
                "checkpoint_bytes": CHECKPOINT.stat().st_size,
                "checkpoint_sha256": CHECKPOINT_SHA256,
                "config": "model_3_ptm",
                "input_features": "upstream-single-sequence-msa-no-template",
                "precision": "float32",
                "torch": torch.__version__,
                "cuda": torch.version.cuda,
                "gpu": torch.cuda.get_device_name(0),
            }
            self.ready = True
            event(
                "MODEL_READY",
                seconds=round(time.monotonic() - started, 6),
                **self.identity,
            )
        except Exception as exc:
            self.error = f"{type(exc).__name__}: {exc}"
            event("LOAD_FAILED", error=self.error)
            traceback.print_exc()

    def predict(self, input_id: str, sequence: str) -> dict[str, Any]:
        import numpy as np
        import torch
        from openfold.data import data_pipeline, feature_pipeline
        from openfold.np import protein, residue_constants

        with self.lock, torch.inference_mode():
            started = time.monotonic()
            np.random.seed(0)
            torch.manual_seed(1)
            raw_features: dict[str, Any] = {}
            raw_features.update(
                data_pipeline.make_sequence_features(
                    sequence=sequence,
                    description=input_id,
                    num_res=len(sequence),
                )
            )
            raw_features.update(data_pipeline.make_dummy_msa_feats(sequence))
            processed = feature_pipeline.FeaturePipeline(
                self.config.data
            ).process_features(
                raw_features,
                mode="predict",
                is_multimer=False,
            )
            batch = {
                key: torch.as_tensor(value, device="cuda")
                for key, value in processed.items()
            }
            event("INFERENCE_BEGIN", input_id=input_id, residues=len(sequence))
            output = self.model(batch)
            torch.cuda.synchronize()

            # OpenFold stores the recycling axis last in processed features.
            final_features = {
                key: to_numpy(value[..., -1]) for key, value in batch.items()
            }
            final_output = to_numpy(output)
            plddt = np.asarray(final_output["plddt"], dtype=np.float64)
            pae = np.asarray(final_output["predicted_aligned_error"], dtype=np.float64)
            ptm = float(np.asarray(final_output["ptm_score"]).reshape(()))
            if (
                plddt.shape != (len(sequence),)
                or pae.shape != (len(sequence), len(sequence))
                or not np.isfinite(plddt).all()
                or not np.isfinite(pae).all()
                or not math.isfinite(ptm)
            ):
                raise RuntimeError("OpenFold2 output confidence tensors are invalid")
            b_factors = np.repeat(
                plddt[..., None], residue_constants.atom_type_num, axis=-1
            )
            structure = protein.from_prediction(
                features=final_features,
                result=final_output,
                b_factors=b_factors,
                remove_leading_feature_dimension=False,
                remark="OpenFold2 model_3_ptm; no external MSA or template",
            )
            pdb = protein.to_pdb(structure)
            elapsed = time.monotonic() - started
            event("INFERENCE_COMPLETE", input_id=input_id, seconds=round(elapsed, 6))
            return {
                "input_id": input_id,
                "structures_in_ranked_order": [
                    {
                        "format": "pdb",
                        "structure": pdb,
                        "relaxed": False,
                        "model_param_set": 1,
                        "confidence": float(plddt.mean()),
                        "plddt": plddt.tolist(),
                        "predicted_aligned_error": pae.tolist(),
                        "ptm_score": ptm,
                        "runtime_origin": "upstream-source-not-nim",
                        "inference_seconds": elapsed,
                    }
                ],
            }


RUNTIME = Runtime()


class Handler(BaseHTTPRequestHandler):
    server_version = "fs2-openfold2-upstream/1"

    def send_json(self, status: int, value: object) -> None:
        body = json.dumps(value, sort_keys=True, allow_nan=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/v1/health/live":
            self.send_json(200, {"status": "live"})
        elif self.path == "/v1/health/ready":
            self.send_json(
                200 if RUNTIME.ready else 503,
                {
                    "status": "ready" if RUNTIME.ready else "loading",
                    "error": RUNTIME.error,
                },
            )
        elif self.path == "/v1/runtime" and RUNTIME.ready:
            self.send_json(200, RUNTIME.identity)
        else:
            self.send_json(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        if self.path != ENDPOINT:
            self.send_json(404, {"error": "not found"})
            return
        if not RUNTIME.ready:
            self.send_json(503, {"error": "model not ready"})
            return
        try:
            content_length = int(self.headers.get("Content-Length", "0"))
            if not 0 < content_length <= MAX_REQUEST_BYTES:
                raise ValueError("request body is empty or exceeds 64 KiB")
            input_id, sequence = parse_request(
                json.loads(self.rfile.read(content_length))
            )
            self.send_json(200, RUNTIME.predict(input_id, sequence))
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
            self.send_json(400, {"error": str(exc)})
        except Exception as exc:
            traceback.print_exc()
            self.send_json(500, {"error": f"{type(exc).__name__}: {exc}"})


if __name__ == "__main__":
    event("PROCESS_START")
    threading.Thread(target=RUNTIME.load, daemon=True).start()
    ThreadingHTTPServer(("0.0.0.0", 8000), Handler).serve_forever()
