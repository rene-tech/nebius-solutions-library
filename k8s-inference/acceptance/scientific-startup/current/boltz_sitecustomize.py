"""Emit a source-time boundary after Lightning enters native GPU prediction.

Benchmark only: no model, arguments, weights, precision or batch size changes.
The prediction-start hook runs after Lightning's model/checkpoint/device setup.
Lazy compilation remains part of the first prediction, not this boundary.
"""

from __future__ import annotations

import builtins
from datetime import UTC, datetime
import functools
import hashlib
import inspect
import json
import os
import sys
import time


START = time.monotonic()
ORIGINAL_IMPORT = builtins.__import__
PATCHED: set[int] = set()
EMITTED = False


def patch_loaded() -> None:
    for name in ("lightning.pytorch.loops.prediction_loop", "pytorch_lightning.loops.prediction_loop"):
        module = sys.modules.get(name)
        loop = getattr(module, "_PredictionLoop", None)
        if loop is None or id(loop) in PATCHED:
            continue
        original = getattr(loop, "_on_predict_start", None)
        if original is None:
            continue
        source_hash = hashlib.sha256(inspect.getsource(original).encode()).hexdigest()

        @functools.wraps(original)
        def measured(self, *args, _original=original, _source_hash=source_hash, **kwargs):
            global EMITTED
            result = _original(self, *args, **kwargs)
            if not EMITTED:
                import torch

                if not torch.cuda.is_available():
                    raise RuntimeError("BoltzGen startup probe has no CUDA device")
                torch.cuda.synchronize()
                EMITTED = True
                print("FS2_STARTUP " + json.dumps({
                    "schema": "fs2-startup-benchmark/v1",
                    "model_id": "boltzgen",
                    "phase": "model_ready",
                    "pid": os.getpid(),
                    "utc": datetime.now(UTC).isoformat(),
                    "python_process_seconds": time.monotonic() - START,
                    "boundary": "_PredictionLoop._on_predict_start return after native setup/restore/device placement and CUDA synchronization",
                    "compilation": "not-warmed; first-shape compilation remains prediction work",
                    "device": torch.cuda.get_device_name(),
                    "compute_capability": list(torch.cuda.get_device_capability()),
                    "torch_version": torch.__version__,
                    "cuda_version": torch.version.cuda,
                    "native_hook_source_sha256": _source_hash,
                }, sort_keys=True), flush=True)
            return result

        loop._on_predict_start = measured
        PATCHED.add(id(loop))


def importing(name, globals=None, locals=None, fromlist=(), level=0):
    result = ORIGINAL_IMPORT(name, globals, locals, fromlist, level)
    if name.startswith(("lightning", "pytorch_lightning")):
        patch_loaded()
    return result


if os.environ.get("FS2_STARTUP_BENCHMARK_MODEL") == "boltzgen":
    builtins.__import__ = importing
