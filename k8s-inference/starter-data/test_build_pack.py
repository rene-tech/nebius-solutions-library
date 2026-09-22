"""Focused tests for deterministic microscopy starter-data preparation."""

import numpy as np

import build_pack


def test_bbbc039_selection_is_ten_distinct_stratified_fields():
    cases = build_pack.BBBC039_CASES

    assert len(cases) == 10
    assert len({slug for slug, _, _, _ in cases}) == 10
    assert len({source for _, source, _, _ in cases}) == 10
    assert {partition for _, _, partition, _ in cases} == {
        "training",
        "validation",
        "test",
    }
    counts = [count for _, _, _, count in cases]
    assert counts == sorted(counts)
    assert (counts[0], counts[-1]) == (6, 161)


def test_normalize_microscopy_image_is_deterministic_and_preserves_geometry():
    pixels = np.arange(16 * 24, dtype=np.uint16).reshape(16, 24) * 17

    first, first_low, first_high = build_pack.normalize_microscopy_image(pixels)
    second, second_low, second_high = build_pack.normalize_microscopy_image(pixels)

    assert np.array_equal(first, second)
    assert (first_low, first_high) == (second_low, second_high)
    assert first.shape == pixels.shape
    assert first.dtype == np.uint8
    assert first.min() == 0
    assert first.max() == 255


def test_decode_bbbc039_instances_uses_eight_connectivity_and_contiguous_labels():
    mask = np.zeros((8, 9, 3), dtype=np.uint8)
    # Diagonally touching foreground belongs to one nucleus under the
    # documented eight-connectivity rule.
    mask[1, 1, 0] = 255
    mask[2, 2, 0] = 255
    mask[5:7, 6:8, 0] = 255

    labels, count, areas = build_pack.decode_bbbc039_instances(mask)

    assert labels.dtype == np.uint16
    assert count == 2
    assert set(np.unique(labels)) == {0, 1, 2}
    assert areas == [2, 4]
    assert sum(areas) == np.count_nonzero(mask[:, :, 0])


def test_microscopy_helpers_reject_invalid_inputs():
    with np.testing.assert_raises_regex(ValueError, "two-dimensional integer"):
        build_pack.normalize_microscopy_image(np.ones((2, 2), dtype=np.float32))
    with np.testing.assert_raises_regex(ValueError, "no usable intensity range"):
        build_pack.normalize_microscopy_image(np.ones((2, 2), dtype=np.uint16))
    with np.testing.assert_raises_regex(ValueError, "invalid instance count"):
        build_pack.decode_bbbc039_instances(np.zeros((2, 2), dtype=np.uint8))
