"""Compatibility boundary for the pinned VISTA3D point-coordinate transform.

The upstream pipeline passes a PyTorch-derived affine through NumPy operations.
Modern array dispatch can return a Tensor, which its subsequent from_numpy call
rejects. Normalize both operands before the unchanged homogeneous transform.
Coordinates remain in the original image voxel space; no clipping or resampling.
"""
from __future__ import annotations

import numpy as np


def _numpy(value):
    if hasattr(value, "detach"):
        value = value.detach().cpu().numpy()
    return np.asarray(value)


def transform_points_numpy(point, affine):
    """Return [batch, points, 3] NumPy coordinates, including Tensor inputs."""
    points = _numpy(point)
    matrix = _numpy(affine)
    homogeneous = np.concatenate((points, np.ones((*points.shape[:2], 1))), axis=-1)
    return np.einsum("ij,bnj->bni", matrix, homogeneous)[..., :3]
