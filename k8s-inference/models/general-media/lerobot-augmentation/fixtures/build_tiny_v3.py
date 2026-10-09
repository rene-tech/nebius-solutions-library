#!/usr/bin/env python3
"""Build the deterministic 16-frame public LeRobot v3 acceptance fixture."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
from lerobot.datasets.lerobot_dataset import LeRobotDataset

RUNTIME = Path(__file__).resolve().parents[1] / "runtime" / "src"
sys.path.insert(0, str(RUNTIME))

from fs2_lerobot_augmentation.dataset import package_dataset, write_bundle_manifest  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.error("--output must not already exist")
    features = {
        "observation.images.front": {
            "dtype": "video",
            "shape": (3, 256, 256),
            "names": ["channels", "height", "width"],
        },
        "observation.state": {
            "dtype": "float32",
            "shape": (4,),
            "names": ["x", "y", "z", "gripper"],
        },
        "action": {
            "dtype": "float32",
            "shape": (4,),
            "names": ["dx", "dy", "dz", "gripper"],
        },
    }
    dataset = LeRobotDataset.create(
        repo_id="fs2/synthetic-cosmos3-lerobot",
        fps=8,
        features=features,
        root=args.output,
        robot_type="synthetic-tabletop-arm",
        use_videos=True,
        video_backend="pyav",
    )
    x = np.arange(256, dtype=np.uint8)[None, :]
    y = np.arange(256, dtype=np.uint8)[:, None]
    for index in range(16):
        image = np.empty((256, 256, 3), dtype=np.uint8)
        image[:, :, 0] = (x + index * 7) % 255
        image[:, :, 1] = (y + index * 11) % 255
        image[:, :, 2] = 64 + index * 8
        dataset.add_frame(
            {
                "observation.images.front": image,
                "observation.state": np.asarray(
                    [index / 15, 0.25, 0.5, index % 2], dtype=np.float32
                ),
                "action": np.asarray([0.01, 0.0, -0.01, index % 2], dtype=np.float32),
                "task": "move the red sample vial into the rack",
            }
        )
    dataset.save_episode()
    dataset.finalize()
    write_bundle_manifest(args.output)
    artifact = package_dataset(
        args.output, args.output.parent / f"{args.output.name}.tar.zst"
    )
    print(f"dataset={args.output}")
    print(f"artifact={args.output.parent / artifact.path}")
    print(f"sha256={artifact.sha256}")
    print(f"size_bytes={artifact.size_bytes}")


if __name__ == "__main__":
    main()
