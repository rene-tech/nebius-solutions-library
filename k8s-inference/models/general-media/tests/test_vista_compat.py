"""Point-transform math is unchanged; tensor dispatch cannot leak into NumPy."""
import importlib.util
from pathlib import Path

import numpy as np
import pytest

source = Path(__file__).parents[1] / "vista_compat.py"
spec = importlib.util.spec_from_file_location("vista_compat", source)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class TensorInput:
    def __init__(self, array):
        self.array = array

    def detach(self):
        return self

    def cpu(self):
        return self

    def numpy(self):
        return self.array

    def __array__(self, *args, **kwargs):
        raise AssertionError("Must detach/copy Tensor to CPU before NumPy conversion")


@pytest.mark.parametrize("tensor_points,tensor_affine", [(False, False), (True, False), (False, True), (True, True)])
def test_affine_rotation_scale_translation_matches_original_math(tensor_points, tensor_affine):
    points = np.random.default_rng(42).normal(size=(2, 7, 3))
    affine = np.array([[0, -2, 0, 11], [3, 0, 0, -9], [0, 0, 0.5, 4], [0, 0, 0, 1]])
    expected = np.empty_like(points)
    for batch in range(2):
        for index in range(7):
            expected[batch, index] = (affine @ np.r_[points[batch, index], 1])[:3]
    actual = module.transform_points_numpy(TensorInput(points) if tensor_points else points,
                                          TensorInput(affine) if tensor_affine else affine)
    assert type(actual) is np.ndarray
    assert actual.shape == (2, 7, 3)
    np.testing.assert_allclose(actual, expected, atol=1e-12)


def test_single_click_identity_keeps_original_voxel_coordinates():
    points = [[[138, 245, 18]]]
    actual = module.transform_points_numpy(points, np.eye(4))
    np.testing.assert_array_equal(actual, points)
