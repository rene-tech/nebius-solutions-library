"""Real pinned-reader roundtrip; transformations here are LOCAL test doubles."""

import os
import subprocess
from pathlib import Path

import pytest

import dataset_io
from fs2_lerobot_augmentation.contracts import AugmentationRequest
from fs2_lerobot_augmentation.dataset import (
    encode_episode_reference,
    open_and_validate,
    package_dataset,
    rewrite_variant,
)


@pytest.fixture
def source():
    path = os.environ.get("FS2_LEROBOT_LOCAL_DATASET")
    if path is None:
        pytest.skip(
            "set FS2_LEROBOT_LOCAL_DATASET; run with pinned LeRobot 0.6.1 Python"
        )
    return Path(path)


def test_local_directory_to_reader_verified_output(source, tmp_path):
    template = Path(__file__).parent / "lighting.json"
    prepared = dataset_io.prepare(source, template, tmp_path, max_bytes=1024**3)
    assert prepared["source"]["frames"] == 64
    assert prepared["source"]["episodes"] == 2
    assert prepared["source"]["decoded_video_frames"] == 128
    request = AugmentationRequest.parse(prepared["parameters"])
    localized = tmp_path / "source"
    original = open_and_validate(
        localized, repo_id="fs2/local-test", selection=request.selection
    )
    reference, changed = tmp_path / "reference.mp4", tmp_path / "changed.mp4"
    encode_episode_reference(
        original, original.episodes[0], original.cameras[0], reference
    )
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-i",
            str(reference),
            "-vf",
            "eq=brightness=0.12",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            str(changed),
        ],
        check=True,
    )
    provenance = {
        "source": {"tree_sha256": original.tree_sha256},
        "operations": [{"episode_index": 0, "camera": original.cameras[0]}],
    }
    rewritten = rewrite_variant(
        original,
        output_root=tmp_path / "rewritten",
        output_repo_id="fs2/local-test-output",
        video_replacements={(0, original.cameras[0]): changed},
        action_replacements={},
        provenance=provenance,
    )
    archive = tmp_path / "output.tar.zst"
    packaged = package_dataset(rewritten.root, archive)
    variant = {
        "artifact": {"sha256": packaged.sha256, "size_bytes": packaged.size_bytes},
        "provenance_sha256": dataset_io.sha256_file(
            rewritten.root / "meta/fs2-augmentation-provenance.json"
        ),
    }
    dataset_io.write_json(tmp_path / "parameters.json", prepared["parameters"])
    dataset_io.write_json(tmp_path / "variant.json", variant)
    result = dataset_io.validate(
        localized,
        archive,
        tmp_path / "reloaded",
        tmp_path / "parameters.json",
        tmp_path / "variant.json",
        tmp_path / "validation.json",
    )
    assert result["validation"]["nonvideo_values_exact"] is True
    assert result["validation"]["decoded_frames_each"] == 128
    assert result["physical_alignment_verified"] is False
    assert (
        len(
            [
                row
                for row in result["validation"]["visual_comparison"]
                if not row["selected"]
            ]
        )
        == 3
    )


def test_all_episode_camera_request_preparation(source, tmp_path):
    result = dataset_io.prepare(
        source, Path(__file__).parent / "environment.json", tmp_path, max_bytes=1024**3
    )
    assert result["parameters"]["selection"] == {"episodes": "all", "cameras": "all"}
    assert (
        result["source"]["frames"] == 64
        and result["source"]["decoded_video_frames"] == 128
    )
