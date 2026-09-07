"""Benchmark-only loader hooks for Mosaic and BindCraft isolated Jobs.

The hooks do not change runtime arguments, model precision, iteration counts or
resource envelopes. They emit a timestamp at a model-specific native
initialization boundary. ``jax.effects_barrier`` drains ordered effects but is
not a ``block_until_ready`` for every pure model array; reports must therefore
describe these as initialization/dispatch boundaries, not fully synchronized
or shape-warmed GPU readiness.
"""

from __future__ import annotations

import builtins
import functools
import json
import os
import sys
import time
from datetime import datetime, timezone
from typing import Any, Callable


MODEL = os.environ.get("FS2_STARTUP_BENCHMARK_MODEL", "")
ORIGINAL_IMPORT = builtins.__import__
PROCESS_MONOTONIC = time.monotonic()
PROCESS_UTC = datetime.now(timezone.utc)
PATCHED: set[tuple[int, str]] = set()
EMITTED: set[str] = set()


def emit(phase: str, **extra: Any) -> None:
    print(
        "FS2_STARTUP "
        + json.dumps(
            {
                "schema": "fs2-startup-benchmark/v1",
                "model_id": MODEL,
                "phase": phase,
                "pid": os.getpid(),
                "monotonic_seconds": time.monotonic(),
                "python_process_seconds": time.monotonic() - PROCESS_MONOTONIC,
                "python_process_started_utc": PROCESS_UTC.isoformat(),
                "utc": datetime.now(timezone.utc).isoformat(),
                **extra,
            },
            sort_keys=True,
        ),
        flush=True,
    )


def synchronize_jax() -> dict[str, Any]:
    jax = sys.modules.get("jax")
    if jax is None:
        return {"placement": "jax-runtime", "device": "unavailable"}
    barrier = getattr(jax, "effects_barrier", None)
    if callable(barrier):
        barrier()
    devices = jax.devices()
    device = devices[0] if devices else None
    return {
        "placement": "jax-device",
        "device": getattr(device, "device_kind", str(device)),
        "jax_version": getattr(jax, "__version__", "unknown"),
        "synchronization_scope": (
            "jax.effects_barrier; pure arrays are not proven block_until_ready"
        ),
    }


def once(phase: str, boundary: str) -> Callable[[], None]:
    def measured() -> None:
        if phase in EMITTED:
            return
        EMITTED.add(phase)
        emit(
            phase,
            boundary=boundary,
            compilation="not-warmed; first request compilation remains outside readiness",
            **synchronize_jax(),
        )

    return measured


def wrap(owner: Any, name: str, *, before: Callable[[], None] | None = None,
         after: Callable[[], None] | None = None) -> None:
    key = (id(owner), name)
    if key in PATCHED or not hasattr(owner, name):
        return
    original = getattr(owner, name)

    @functools.wraps(original)
    def measured(*args: Any, **kwargs: Any) -> Any:
        if before is not None:
            before()
        result = original(*args, **kwargs)
        if after is not None:
            after()
        return result

    setattr(owner, name, measured)
    PATCHED.add(key)


def patch_loaded_modules() -> None:
    if MODEL == "mosaic":
        module = sys.modules.get("mosaic.optimizers")
        if module is not None:
            wrap(
                module,
                "simplex_APGM",
                before=once(
                    "model_ready",
                    "simplex_APGM entry after Boltz2 and ProteinMPNN checkpoint "
                    "load, feature construction, loss construction and initial JAX dispatch",
                ),
                after=once("first_compute_complete", "simplex_APGM return"),
            )
    elif MODEL == "bindcraft":
        module = sys.modules.get("functions.colabdesign_utils")
        if module is not None:
            wrap(
                module,
                "mk_afdesign_model",
                after=once(
                    "model_ready",
                    "first mk_afdesign_model return after PyRosetta initialization "
                    "and AlphaFold parameter loading",
                ),
            )
            wrap(
                module,
                "mk_mpnn_model",
                after=once(
                    "auxiliary_model_ready",
                    "first lazy mk_mpnn_model return after ProteinMPNN weight loading",
                ),
            )


def importing(
    name: str,
    globals: dict[str, Any] | None = None,
    locals: dict[str, Any] | None = None,
    fromlist: tuple[str, ...] = (),
    level: int = 0,
) -> Any:
    result = ORIGINAL_IMPORT(name, globals, locals, fromlist, level)
    if name.startswith(("mosaic.optimizers", "functions")):
        patch_loaded_modules()
    return result


if MODEL in {"mosaic", "bindcraft"}:
    emit("python_process_start")
    builtins.__import__ = importing
