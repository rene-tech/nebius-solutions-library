#!/usr/bin/env python3
"""Import one pinned public synthetic Cosmos robot/action chunk as LeRobot v3."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

import av
import numpy as np
from fs2_lerobot_augmentation.dataset import package_dataset, write_bundle_manifest
from lerobot.datasets.lerobot_dataset import LeRobotDataset

VIDEO = Path("/fixtures/official-robot.mp4")
ACTIONS = Path("/fixtures/official-actions.json")
VIDEO_SHA256 = "9b01266b6cd27478514133b00ada9c33db3f9444167f09942c11f880c629c8c0"
ACTIONS_SHA256 = "ba8408f727f9c77d4450b239069f81e9cdd9d099bf05da7f33f5bfb4cb2d55cd"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.error("output must not already exist")
    assert hashlib.sha256(VIDEO.read_bytes()).hexdigest() == VIDEO_SHA256
    assert hashlib.sha256(ACTIONS.read_bytes()).hexdigest() == ACTIONS_SHA256
    value = json.loads(ACTIONS.read_text())
    actions = np.asarray(value["action_chunks"][0], dtype=np.float32)
    assert actions.shape == (16, 29) and value["fps"] == 10
    with av.open(str(VIDEO)) as source:
        frames = list(source.decode(video=0))[:16]
    assert len(frames) == 16 and frames[0].width == 640 and frames[0].height == 720
    dataset = LeRobotDataset.create(
        repo_id="fs2/public-synthetic-cosmos3-robot",
        fps=10,
        features={
            "observation.images.front": {
                "dtype": "video",
                "shape": (3, 352, 640),
                "names": ["channels", "height", "width"],
            },
            "action": {"dtype": "float32", "shape": (29,), "names": [f"action_{i}" for i in range(29)]},
        },
        root=args.output,
        robot_type="public-synthetic-agibotworld",
        use_videos=True,
        video_backend="pyav",
    )
    for frame, action in zip(frames, actions, strict=True):
        dataset.add_frame(
            {
                # Official output stacks two 640x360 views. Retain the upper
                # camera, trimming four rows per edge to meet the 16-pixel grid.
                "observation.images.front": frame.to_ndarray(format="rgb24")[4:356],
                "action": action,
                "task": value["prompt"],
            }
        )
    dataset.save_episode()
    dataset.finalize()
    (args.output / "source-provenance.json").write_text(
        json.dumps(
            {
                "source": "nvidia/Cosmos3-Nano@7a312c868bcce8e40b3eb40861300a9d0ba3fde1",
                "video": "assets/example_action_fd_agibotworld_4chunk_output.mp4",
                "video_sha256": VIDEO_SHA256,
                "actions": "assets/example_action_fd_agibotworld_action_chunks.json",
                "actions_sha256": ACTIONS_SHA256,
                "selection": "first16frames,firstactionchunk,uppercamera,rows4:356",
                "classification": "public-model-generated-example; not recorded robot telemetry",
            },
            sort_keys=True,
        )
    )
    write_bundle_manifest(args.output)
    package_dataset(args.output, args.output.parent / f"{args.output.name}.tar.zst")


if __name__ == "__main__":
    main()
