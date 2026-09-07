"""Benchmark-only import hooks; mounted as sitecustomize.py in isolated jobs.

No global tracing/profiling and no model argument, precision or step changes.
Ready means loaded/initialized, before request-specific lazy compilation.
"""

import builtins
import functools
import json
import os
import sys
import time
from datetime import datetime, timezone

MODEL = os.environ.get("FS2_STARTUP_BENCHMARK_MODEL", "")
ORIGINAL_IMPORT = builtins.__import__
ORIGINAL_PRINT = builtins.print
PATCHED = set()
STARTED = time.monotonic()


def emit(phase, **extra):
    ORIGINAL_PRINT(
        "FS2_STARTUP "
        + json.dumps(
            {
                "schema": "fs2-startup-benchmark/v1",
                "model_id": MODEL,
                "phase": phase,
                "pid": os.getpid(),
                "monotonic_seconds": time.monotonic(),
                "utc": datetime.now(timezone.utc).isoformat(),
                "python_process_seconds": time.monotonic() - STARTED,
                **extra,
            },
            sort_keys=True,
        ),
        flush=True,
    )


def synchronized_ready(boundary, placement="cuda"):
    if placement == "cuda":
        torch = sys.modules["torch"]
        torch.cuda.synchronize()
        emit(
            "model_ready",
            boundary=boundary,
            placement=placement,
            compilation="not-warmed",
            device=torch.cuda.get_device_name(0),
            compute_capability=list(torch.cuda.get_device_capability(0)),
            torch_version=torch.__version__,
            cuda_version=torch.version.cuda,
        )
    else:
        emit(
            "model_ready",
            boundary=boundary,
            placement=placement,
            compilation="JAX compilation and device placement remain first-request work",
        )


def wrap(owner, name, *, before=None, after=None):
    key = (id(owner), name)
    if key in PATCHED:
        return
    original = getattr(owner, name)

    @functools.wraps(original)
    def measured(*args, **kwargs):
        if before:
            before()
        result = original(*args, **kwargs)
        if after:
            after()
        return result

    setattr(owner, name, measured)
    PATCHED.add(key)


def patch_loaded_modules():
    if MODEL.startswith("esmfold2"):
        module = sys.modules.get("esm.models.esmfold2")
        if module and hasattr(module, "ESMFold2InputBuilder"):
            wrap(
                module.ESMFold2InputBuilder,
                "fold",
                before=lambda: synchronized_ready(
                    "ESMFold2InputBuilder.fold entry after both checkpoints and precision setup"
                ),
                after=lambda: (
                    sys.modules["torch"].cuda.synchronize(),
                    emit("first_compute_complete"),
                ),
            )
    elif MODEL == "protenix-v2":
        module = sys.modules.get("runner.inference")
        if module and hasattr(module, "InferenceRunner"):
            wrap(
                module.InferenceRunner,
                "__init__",
                before=lambda: emit("model_initialization_start"),
                after=lambda: synchronized_ready(
                    "InferenceRunner.__init__ return after load_checkpoint and init_dumper"
                ),
            )
            wrap(
                module.InferenceRunner,
                "predict",
                before=lambda: emit("first_compute_start"),
                after=lambda: (
                    sys.modules["torch"].cuda.synchronize(),
                    emit("first_compute_complete"),
                ),
            )
    elif MODEL == "openfold3-openbind":
        module = sys.modules.get("pytorch_lightning.loops.prediction_loop")
        if module and hasattr(module, "_PredictionLoop"):
            wrap(
                module._PredictionLoop,
                "_predict_step",
                before=lambda: synchronized_ready(
                    "Lightning prediction step entry after setup and checkpoint restoration"
                ),
                after=lambda: (
                    sys.modules["torch"].cuda.synchronize(),
                    emit("first_compute_complete"),
                ),
            )
    elif MODEL == "alphafold3":
        module = sys.modules.get("alphafold3.model.params")
        if module and hasattr(module, "get_model_haiku_params"):
            wrap(
                module,
                "get_model_haiku_params",
                before=lambda: emit("model_initialization_start"),
                after=lambda: synchronized_ready(
                    "get_model_haiku_params return; native host parameter initialization",
                    "host-parameters",
                ),
            )


def importing(name, globals=None, locals=None, fromlist=(), level=0):
    result = ORIGINAL_IMPORT(name, globals, locals, fromlist, level)
    if name.startswith(
        (
            "esm.models.esmfold2",
            "runner.inference",
            "pytorch_lightning",
            "alphafold3.model",
        )
    ):
        patch_loaded_modules()
    return result


def af3_print(*args, **kwargs):
    ORIGINAL_PRINT(*args, **kwargs)
    message = " ".join(str(item) for item in args)
    if message.startswith("Running model inference with seed "):
        emit(
            "first_compute_complete" if " took " in message else "first_compute_start",
            clock="native synchronous inference; includes first-shape JAX compilation",
        )


if MODEL:
    emit("python_process_start")
    builtins.__import__ = importing
    if MODEL == "alphafold3":
        builtins.print = af3_print
