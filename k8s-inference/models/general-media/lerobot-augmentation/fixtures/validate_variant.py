"""Full-reader acceptance comparison; data integrity is not physical-motion qualification."""

from __future__ import annotations

from typing import Any

import numpy as np
from fs2_lerobot_augmentation.dataset import DatasetInspection


def compare_variant(
    source: DatasetInspection,
    output: DatasetInspection,
    *,
    replacements: set[tuple[int, str]],
    provenance: dict[str, Any] | None = None,
    max_unselected_mae: float = 6.0,
) -> dict[str, Any]:
    if (source.frames, source.fps, source.cameras, source.episodes) != (
        output.frames,
        output.fps,
        output.cameras,
        output.episodes,
    ):
        raise ValueError("output episode/camera/frame/task/FPS structure differs from source")
    camera_pairs = {(episode.index, camera) for episode in source.episodes for camera in source.cameras}
    if not replacements <= camera_pairs:
        raise ValueError("replacement selection is outside dataset")
    numeric_values = 0
    differences: dict[tuple[int, str], list[float]] = {pair: [] for pair in camera_pairs}
    for episode in source.episodes:
        for index in range(episode.start, episode.stop):
            left = source.dataset.get_raw_item(index)
            right = output.dataset.get_raw_item(index)
            keys = set(left) - set(source.cameras)
            if keys != set(right) - set(output.cameras):
                raise ValueError("output nonvideo feature keys differ")
            for key in keys:
                a, b = np.asarray(left[key]), np.asarray(right[key])
                if a.dtype != b.dtype or not np.array_equal(a, b):
                    raise ValueError(f"output nonvideo field {key} differs at frame {index}")
                numeric_values += a.size
            decoded_left, decoded_right = source.dataset[index], output.dataset[index]
            if decoded_left["task"] != decoded_right["task"]:
                raise ValueError("output task semantics differ")
            for camera in source.cameras:
                a = np.asarray(decoded_left[camera], dtype=np.float32)
                b = np.asarray(decoded_right[camera], dtype=np.float32)
                if a.shape != b.shape or not np.isfinite(b).all():
                    raise ValueError("output decoded camera shape/values differ")
                differences[(episode.index, camera)].append(float(np.abs(a - b).mean()))
    visual = []
    for (episode, camera), values in sorted(differences.items()):
        selected = (episode, camera) in replacements
        mae = float(np.mean(values))
        if not selected and max(values) > max_unselected_mae:
            raise ValueError(f"unselected episode {episode}/{camera} exceeded declared lossy-codec MAE tolerance")
        visual.append(
            {
                "episode_index": episode,
                "camera": camera,
                "selected": selected,
                "frames": len(values),
                "mean_absolute_pixel_difference": mae,
                "max_frame_mean_absolute_pixel_difference": max(values),
                "changed_frames": sum(value > 1.0 for value in values),
            }
        )
    if provenance is not None:
        if provenance.get("source", {}).get("tree_sha256") != source.tree_sha256:
            raise ValueError("provenance source tree identity differs")
        actual = {(item["episode_index"], item["camera"]) for item in provenance.get("operations", [])}
        if actual != replacements:
            raise ValueError("provenance generated-unit mapping differs")
    return {
        "reader": "lerobot==0.6.1",
        "status": "passed",
        "episodes": len(source.episodes),
        "frames": source.frames,
        "cameras": len(source.cameras),
        "decoded_frames_each": source.frames * len(source.cameras),
        "nonvideo_values_compared": numeric_values,
        "nonvideo_values_exact": True,
        "unselected_pixels_bit_exact": False,
        "unselected_max_frame_mae_limit": max_unselected_mae,
        "visual_comparison": visual,
        "physical_alignment_verified": False,
    }
