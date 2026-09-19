import importlib.util
from pathlib import Path

import numpy as np
import pytest

spec = importlib.util.spec_from_file_location(
    "motion", Path(__file__).with_name("compare_video_motion.py")
)
motion = importlib.util.module_from_spec(spec)
spec.loader.exec_module(motion)


def moving_square():
    frames = np.zeros((8, 64, 80), dtype=np.uint8)
    for index, frame in enumerate(frames):
        frame[20:40, 10 + 2 * index : 30 + 2 * index] = 255
    return frames


def test_same_motion_is_exact_in_proxy_not_physical_claim():
    source = moving_square()
    metrics = motion.compare_arrays(source, source.copy())
    assert metrics["frames"] == 8 and metrics["transitions"] == 7
    assert metrics["source_motion_pixels"] > 0
    assert metrics["flow_endpoint_error_mean_pixels"] == 0
    assert metrics["flow_direction_cosine_mean"] > 0.999


def test_static_output_detects_lost_source_motion():
    source = moving_square()
    metrics = motion.compare_arrays(source, np.repeat(source[:1], len(source), axis=0))
    assert metrics["flow_endpoint_error_mean_pixels"] > 0.75
    assert metrics["flow_direction_cosine_mean"] == 0
    assert metrics["temporal_activity_correlation"] is None


def test_empty_motion_is_unknown_not_fabricated_perfect():
    metrics = motion.compare_arrays(
        np.zeros((3, 64, 80), dtype=np.uint8), np.zeros((3, 64, 80), dtype=np.uint8)
    )
    assert metrics["source_motion_pixels"] == 0
    assert metrics["flow_endpoint_error_mean_pixels"] is None
    assert metrics["flow_direction_cosine_mean"] is None


def test_unequal_frame_counts_fail():
    with pytest.raises(ValueError, match="equal frame"):
        motion.compare_arrays(moving_square(), moving_square()[:-1])
