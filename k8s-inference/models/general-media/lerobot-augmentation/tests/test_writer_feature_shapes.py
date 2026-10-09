"""Recorded scalar auxiliary fields must survive the pinned reader/writer."""
from __future__ import annotations

import os
import sys
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "runtime" / "src"))

from fs2_lerobot_augmentation.dataset import DatasetError, _writer_feature  # noqa: E402


@pytest.mark.parametrize("dtype,value", [("bool", True), ("bool", False), ("float32", 0.25), ("int64", 17)])
def test_stored_scalar_is_losslessly_presented_as_declared_singleton(dtype, value):
    from lerobot.datasets.feature_utils import validate_feature_dtype_and_shape

    original = np.asarray(value, dtype=dtype)
    feature = {"dtype": dtype, "shape": (1,)}
    result = _writer_feature("auxiliary", original, feature)
    assert result.shape == (1,) and result.dtype == original.dtype
    assert result.tobytes() == original.tobytes()
    assert np.shares_memory(result, original)
    assert validate_feature_dtype_and_shape("auxiliary", feature, result) == ""


def test_existing_vector_has_no_copy_or_reordering():
    value = np.asarray([1.5, -2.0, 0.0], dtype="float32")
    assert _writer_feature("action", value, {"dtype": "float32", "shape": (3,)}) is value


@pytest.mark.parametrize("value,shape", [(True, (2,)), ([True, False], (1,)), ([[True]], (1,))])
def test_other_shape_mismatches_are_not_padded_or_flattened(value, shape):
    with pytest.raises(DatasetError, match="shape differs"):
        _writer_feature("next.done", np.asarray(value, dtype="bool"), {"dtype": "bool", "shape": shape})


def test_numeric_dtype_is_not_silently_coerced():
    with pytest.raises(DatasetError, match="dtype differs"):
        _writer_feature("next.done", np.asarray(1, dtype="int64"), {"dtype": "bool", "shape": (1,)})


def test_string_field_is_preserved():
    value = "recorded task"
    assert _writer_feature("description", value, {"dtype": "string", "shape": (1,)}) is value


def test_recorded_aloha_auxiliary_bool_survives_full_writer_and_reader(tmp_path):
    path = os.environ.get("FS2_LEROBOT_RECORDED_ALOHA")
    if path is None:
        pytest.skip("set FS2_LEROBOT_RECORDED_ALOHA to the pinned recorded two-episode input")
    from fs2_lerobot_augmentation.contracts import Selection
    from fs2_lerobot_augmentation.dataset import open_and_validate, rewrite_variant, source_row

    source = open_and_validate(
        Path(path), repo_id="fs2qualification/recorded-aloha",
        selection=Selection(episodes="all", cameras="all"),
    )
    assert source.frames == 128 and len(source.episodes) == 2
    assert source_row(source.dataset, 0)["next.done"].shape == ()
    result = rewrite_variant(
        source, output_root=tmp_path / "recorded-output", output_repo_id="fs2qualification/rewrite-aloha",
        video_replacements={}, action_replacements={}, provenance={},
    )
    assert result.frames == source.frames and result.episodes == source.episodes
    for index in range(source.frames):
        original, rewritten = source_row(source.dataset, index), source_row(result.dataset, index)
        for key in set(original) - set(source.cameras):
            left, right = np.asarray(original[key]), np.asarray(rewritten[key])
            assert left.dtype == right.dtype and np.array_equal(left, right), (index, key)


def test_auxiliary_shape_mismatch_is_detected_during_source_validation(monkeypatch):
    path = os.environ.get("FS2_LEROBOT_RECORDED_ALOHA")
    if path is None:
        pytest.skip("requires pinned recorded input")
    from fs2_lerobot_augmentation import dataset
    from fs2_lerobot_augmentation.contracts import Selection

    original = dataset.source_row

    def changed(*args, **kwargs):
        row = dict(original(*args, **kwargs))
        row["next.done"] = np.asarray([False, True], dtype="bool")
        return row

    monkeypatch.setattr(dataset, "source_row", changed)
    with pytest.raises(DatasetError, match="next.done shape differs"):
        dataset.open_and_validate(
            Path(path), repo_id="fs2qualification/recorded-aloha",
            selection=Selection(episodes="all", cameras="all"),
        )
