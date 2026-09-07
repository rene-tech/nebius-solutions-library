"""Inference-only portable implementation of OpenFold's in-place softmax ABI.

The upstream package imports its optional compiled CUDA extension
unconditionally. The operation's public contract is just an in-place softmax;
PyTorch dispatches the same operation to the active CUDA architecture. Training
is intentionally unsupported by this serving image.
"""

from __future__ import annotations

import torch


def forward_(values: torch.Tensor, rows: int, columns: int) -> None:
    if values.numel() != rows * columns or values.shape[-1] != columns:
        raise ValueError("OpenFold attention softmax shape differs from its ABI")
    values.copy_(torch.softmax(values, dim=-1))


def backward_(*_args: object) -> None:
    raise RuntimeError("the OpenFold2 serving runtime does not support training")
