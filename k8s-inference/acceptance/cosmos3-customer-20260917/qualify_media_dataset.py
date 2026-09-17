#!/usr/bin/env python3
"""Real-GPU preview media and pinned-reader dataset proof, never public acceptance."""

from __future__ import annotations

import argparse
import base64
import hashlib
import importlib.metadata
import json
import shutil
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import httpx
from fs2_lerobot_augmentation.contracts import Selection
from fs2_lerobot_augmentation.cosmos import MODEL_REVISION, _normalize_transfer_video
from fs2_lerobot_augmentation.dataset import (
    decode_generated_video,
    encode_episode_reference,
    extract_uploaded_bundle,
    open_and_validate,
    package_dataset,
    rewrite_variant,
    sha256_file,
)


def record(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, sort_keys=True, indent=2) + "\n")


def qualify(*, base_url: str, output: Path, builder: Path, all_transfer: bool = False) -> None:
    if output.exists():
        raise ValueError("qualification output must be a new directory")
    output.mkdir(parents=True)
    assert importlib.metadata.version("lerobot") == "0.6.1"
    source = output / "source"
    # The caller supplies the tracked, read-only synthetic fixture builder.
    subprocess.run([sys.executable, str(builder), "--output", str(source)], check=True)  # noqa: S603
    selection = Selection(episodes="all", cameras="all")
    inspected = open_and_validate(source, repo_id="fs2/synthetic-cosmos3-lerobot", selection=selection)
    reference = output / "reference.mp4"
    camera = "observation.images.front"
    episode = inspected.episodes[0]
    _, height, width = inspected.dataset.features[camera]["shape"]
    fps = inspected.fps
    encode_episode_reference(inspected, episode, camera, reference)
    reference_uri = "data:video/mp4;base64," + base64.b64encode(reference.read_bytes()).decode()
    dimensions = [
        ("lighting", "transfer-video" if all_transfer else "video-to-video", "warm late-afternoon side lighting"),
        ("environment", "transfer-video", "blue storage fixtures in the background, keeping all foreground fruit"),
    ]
    receipts = []
    with httpx.Client(base_url=base_url, timeout=1800, follow_redirects=False, trust_env=False) as client:
        client.get("/readyz").raise_for_status()
        for index, (dimension, mode, instruction) in enumerate(dimensions):
            payload = {
                "mode": mode,
                "prompt": (
                    "A dual-arm robot with black grippers reaches over a wooden fruit display and shopping cart. "
                    "Preserve the robot, fruit, object positions, camera view and motion. "
                    f"Change only {dimension}: {instruction}."
                ),
                "negative_prompt": "changed motion, missing objects, temporal jitter, blur",
                "input_reference": reference_uri,
                "size": f"{width}x{height}",
                "num_frames": 16,
                "fps": fps,
                "seed": 20260917 + index,
                "num_inference_steps": 35,
                "guidance_scale": 6.0,
                "output_delivery": "artifact",
                "output_format": "mp4",
            }
            if mode == "video-to-video":
                payload.update(condition_frame_indexes_vision=[0, 1], condition_video_keep="first")
            else:
                payload.pop("size")
                payload.update(
                    resolution=min((256, 480, 704, 720), key=lambda value: abs(value - height)),
                    controls=[{"control_type": "edge", "control_weight": 1.0}],
                    num_video_frames_per_chunk=16,
                    num_conditional_frames=1,
                    num_first_chunk_conditional_frames=0,
                    share_vision_temporal_positions=True,
                    emphasize_control_in_prompt=True,
                )
            request_bytes = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
            record(output / f"{dimension}-request.json", payload)
            started = time.monotonic()
            response = client.post("/generate", json=payload)
            elapsed = time.monotonic() - started
            if response.status_code != 200:
                record(output / f"{dimension}-failure.json", {"status": response.status_code, "body": response.text})
                response.raise_for_status()
            assert response.headers["content-type"].startswith("video/mp4")
            raw = output / f"{dimension}-raw.mp4"
            raw.write_bytes(response.content)
            assert sha256_file(raw) == response.headers["x-fs2-output-sha256"]
            assert len(response.content) == int(response.headers["x-fs2-output-bytes"])
            replacement = output / f"{dimension}.mp4"
            shutil.copyfile(raw, replacement)
            if mode == "transfer-video":
                _normalize_transfer_video(replacement, width=width, height=height, frames=16, fps=fps)
            decoded = decode_generated_video(replacement, episode=episode, fps=fps)
            assert decoded.shape == (16, 3, height, width)
            variant = rewrite_variant(
                inspected,
                output_root=output / dimension,
                output_repo_id=f"fs2/cosmos3-preview-{dimension}",
                video_replacements={(0, camera): replacement},
                action_replacements={},
                provenance={
                    "scope": "direct-runtime-preview-not-public-admission",
                    "source": {"tree_sha256": inspected.tree_sha256},
                    "configuration": {"dimension": dimension, "seed": payload["seed"]},
                    "model": {"revision": MODEL_REVISION},
                },
            )
            for row in range(16):
                before, after = inspected.dataset.get_raw_item(row), variant.dataset.get_raw_item(row)
                fields = ["action", "timestamp", "frame_index"]
                fields.extend(key for key in before if key.startswith("observation.state"))
                for field in fields:
                    assert before[field].tolist() == after[field].tolist(), field
            archive = output / f"{dimension}.tar.zst"
            packaged = package_dataset(variant.root, archive)
            localized = output / f"{dimension}-relocalized"
            extract_uploaded_bundle(archive, localized, expected_sha256=packaged.sha256)
            reopened = open_and_validate(localized, repo_id=f"fs2/cosmos3-preview-{dimension}", selection=selection)
            assert reopened.frames == reopened.decoded_video_frames == 16
            receipt = {
                "dimension": dimension,
                "mode": mode,
                "request_sha256": hashlib.sha256(request_bytes).hexdigest(),
                "reference_sha256": sha256_file(reference),
                "raw_output_sha256": sha256_file(raw),
                "normalized_output_sha256": sha256_file(replacement),
                "artifact_sha256": packaged.sha256,
                "artifact_size_bytes": packaged.size_bytes,
                "frames": reopened.frames,
                "width": width,
                "height": height,
                "fps": fps,
                "decoded_video_frames": reopened.decoded_video_frames,
                "actions_states_timestamps_frame_indexes": "unchanged",
                "backend_id": response.headers["x-backend-id"],
                "elapsed_seconds": elapsed,
                "reader": "lerobot==0.6.1",
            }
            record(output / f"{dimension}-receipt.json", receipt)
            receipts.append(receipt)
            print(json.dumps(receipt), flush=True)
    assert len({item["raw_output_sha256"] for item in receipts}) == 2
    assert all(item["raw_output_sha256"] != item["reference_sha256"] for item in receipts)
    record(
        output / "receipt.json",
        {
            "recorded_at": datetime.now(UTC).isoformat(),
            "scope": "real-H100-direct-runtime-media-and-dataset-mechanics",
            "customer_ready": False,
            "public_admission_tested": False,
            "semantic_visual_review": "pending",
            "dimensions": receipts,
        },
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--fixture-builder", type=Path, required=True)
    parser.add_argument("--all-transfer", action="store_true")
    args = parser.parse_args()
    qualify(base_url=args.base_url, output=args.output, builder=args.fixture_builder, all_transfer=args.all_transfer)


if __name__ == "__main__":
    main()
