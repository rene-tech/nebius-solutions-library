#!/usr/bin/env python3
"""Launch upstream RFdiffusion with B300-compatible eager SE(3) fallbacks."""

from __future__ import annotations

import runpy
from typing import Any

import warp


def _eager_fallback(
    meta_mod: Any,
    engine: Any,
    context: Any,
    inputs: tuple[Any, ...],
    num_outputs: int,
    dyn_axes: Any,
    **kwargs: Any,
) -> tuple[Any, Any, tuple[Any, ...], bool]:
    """Use the public PyTorch module when no packaged NIM TRT profile exists."""

    del num_outputs, dyn_axes, kwargs
    output = meta_mod()(*inputs)
    if not isinstance(output, tuple):
        output = (output,)
    return engine, context, output, False


def main() -> None:
    # The NVIDIA SE(3) package bundled in the CUDA/DGL dependency image expects
    # NIM to initialize Warp and attach TensorRT engine names.  The exact public
    # upstream model has no proprietary NIM profile, so initialize Warp directly
    # and retain the package's eager PyTorch path for those radial/projection MLPs.
    warp.init()
    from se3_transformer.model.layers import attention, convolution

    attention.trt_try_run = _eager_fallback
    convolution.trt_try_run = _eager_fallback
    runpy.run_path(
        "/opt/fs2/rfdiffusion/scripts/run_inference.py",
        run_name="__main__",
    )


if __name__ == "__main__":
    main()
