#!/usr/bin/env python3
"""Two-episode/two-view fixture from pinned public generated robot media, not telemetry."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import av
import numpy as np
from lerobot.datasets.lerobot_dataset import LeRobotDataset

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "runtime" / "src"))
from fs2_lerobot_augmentation.dataset import package_dataset, sha256_file, write_bundle_manifest  # noqa: E402

VIDEO_SHA256 = "9b01266b6cd27478514133b00ada9c33db3f9444167f09942c11f880c629c8c0"
ACTIONS_SHA256 = "ba8408f727f9c77d4450b239069f81e9cdd9d099bf05da7f33f5bfb4cb2d55cd"
REPO_ID = "fs2/public-generated-robot-multiview"
FRONT = "observation.images.front"
WRIST = "observation.images.wrist"


def build_fixture(
    output: Path,
    *,
    video: Path,
    actions: Path,
    episode_frames: tuple[int, int] = (32, 32),
    small_unselected_camera: bool = False,
    timestamp_dtype: str = "float32",
    distinct_tasks: bool = False,
) -> Path:
    if output.exists():
        raise ValueError("output must not already exist")
    if sha256_file(video) != VIDEO_SHA256 or sha256_file(actions) != ACTIONS_SHA256:
        raise ValueError("public source does not match pinned media/actions hashes")
    if not all(1 <= count <= 32 for count in episode_frames) or sum(episode_frames) > 64:
        raise ValueError("fixture uses two contiguous episodes of 1..32 frames from the 64-frame source")
    value = json.loads(actions.read_text())
    chunks = np.asarray(value["action_chunks"], dtype=np.float32)
    if chunks.shape != (4, 16, 29) or value["fps"] != 10:
        raise ValueError("source action contract changed")
    with av.open(str(video)) as stream:
        frames = [frame.to_ndarray(format="rgb24") for frame in stream.decode(video=0)]
    if len(frames) != 64 or frames[0].shape != (720, 640, 3):
        raise ValueError("source stacked-camera video contract changed")
    wrist_shape = (3, 128, 128) if small_unselected_camera else (3, 352, 640)
    features = {
        FRONT: {"dtype": "video", "shape": (3, 352, 640), "names": ["channels", "height", "width"]},
        WRIST: {"dtype": "video", "shape": wrist_shape, "names": ["channels", "height", "width"]},
        "action": {"dtype": "float32", "shape": (29,), "names": [f"action_{i}" for i in range(29)]},
        "observation.state": {"dtype": "float32", "shape": (2,), "names": ["fixture_episode", "fixture_frame"]},
    }
    dataset = LeRobotDataset.create(
        REPO_ID,
        fps=10,
        features=features,
        root=output,
        robot_type="public-model-generated-agibotworld",
        use_videos=True,
        video_backend="pyav",
        batch_encoding_size=1,
    )
    if timestamp_dtype not in {"float32", "float64"}:
        raise ValueError("timestamp fixture dtype must be float32 or float64")
    dataset.meta.info.features["timestamp"]["dtype"] = timestamp_dtype
    for episode_index, count in enumerate(episode_frames):
        for frame_index in range(count):
            source_index = sum(episode_frames[:episode_index]) + frame_index
            stacked = frames[source_index]
            wrist = stacked[364:716]
            if small_unselected_camera:
                wrist = av.VideoFrame.from_ndarray(wrist, format="rgb24").reformat(128, 128).to_ndarray(format="rgb24")
            dataset.add_frame(
                {
                    FRONT: stacked[4:356],
                    WRIST: wrist,
                    "action": chunks[source_index // 16, source_index % 16],
                    "observation.state": np.asarray([episode_index, frame_index], dtype=np.float32),
                    "task": value["prompt"] + (f" (fixture episode {episode_index})" if distinct_tasks else ""),
                }
            )
        dataset.save_episode()
    dataset.finalize()
    (output / "source-provenance.json").write_text(
        json.dumps(
            {
                "source": "nvidia/Cosmos3-Nano@7a312c868bcce8e40b3eb40861300a9d0ba3fde1",
                "video_sha256": VIDEO_SHA256,
                "actions_sha256": ACTIONS_SHA256,
                "video": "assets/example_action_fd_agibotworld_4chunk_output.mp4",
                "actions": "assets/example_action_fd_agibotworld_action_chunks.json",
                "episode_frames": episode_frames,
                "timestamp_dtype": timestamp_dtype,
                "distinct_fixture_task_labels": distinct_tasks,
                "source_frame_ranges": [[0, episode_frames[0]], [episode_frames[0], sum(episode_frames)]],
                "selection": "contiguous source frames/action chunks in order; top/bottom views trimmed4rows",
                "classification": (
                    "public model-generated imagery/actions, not recorded telemetry or calibrated cameras"
                ),
                "state": "synthetic fixture episode/frame identifiers, not measured robot state",
                "physical_alignment_verified": False,
            },
            sort_keys=True,
        )
        + "\n"
    )
    write_bundle_manifest(output)
    package_dataset(output, output.parent / f"{output.name}.tar.zst")
    return output


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--video", type=Path, required=True)
    parser.add_argument("--actions", type=Path, required=True)
    args = parser.parse_args()
    print(build_fixture(args.output, video=args.video, actions=args.actions))


if __name__ == "__main__":
    main()
