from __future__ import annotations

import importlib.metadata
import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "runtime" / "src"))

from fs2_lerobot_augmentation.contracts import (  # noqa: E402
    AugmentationRequest,
    Selection,
)
from fs2_lerobot_augmentation.cosmos import _validate_transfer_video_alignment  # noqa: E402
from fs2_lerobot_augmentation.dataset import (  # noqa: E402
    decode_generated_video,
    encode_episode_reference,
    extract_uploaded_bundle,
    open_and_validate,
    package_dataset,
    preflight_tree,
    rewrite_variant,
)
from fs2_lerobot_augmentation.worker import _localize  # noqa: E402


def _has_pinned_lerobot() -> bool:
    try:
        return importlib.metadata.version("lerobot") == "0.6.1"
    except importlib.metadata.PackageNotFoundError:
        return False


@pytest.mark.skipif(
    not _has_pinned_lerobot(), reason="requires pinned lerobot==0.6.1 runtime"
)
def test_public_fixture_builds_and_reloads_through_lerobot(tmp_path: Path) -> None:
    output = tmp_path / "tiny-v3"
    subprocess.run(
        [
            sys.executable,
            str(ROOT / "fixtures" / "build_tiny_v3.py"),
            "--output",
            str(output),
        ],
        check=True,
    )
    inspected = open_and_validate(
        output,
        repo_id="fs2/synthetic-cosmos3-lerobot",
        selection=Selection(episodes="all", cameras="all"),
    )
    assert len(inspected.episodes) == 1
    assert inspected.frames == 16
    assert inspected.decoded_video_frames == 16
    assert inspected.action_shape == (4,)


@pytest.mark.skipif(
    not _has_pinned_lerobot(), reason="requires pinned lerobot==0.6.1 runtime"
)
def test_complete_variant_is_finalized_reloaded_packaged_and_relocalized(
    tmp_path: Path,
) -> None:
    source = tmp_path / "tiny-v3"
    subprocess.run(
        [
            sys.executable,
            str(ROOT / "fixtures" / "build_tiny_v3.py"),
            "--output",
            str(source),
        ],
        check=True,
    )
    inspected = open_and_validate(
        source,
        repo_id="fs2/synthetic-cosmos3-lerobot",
        selection=Selection(episodes="all", cameras="all"),
    )
    reference = tmp_path / "episode-reference.mp4"
    encode_episode_reference(
        inspected, inspected.episodes[0], "observation.images.front", reference
    )
    normalized = tmp_path / "normalized-transfer.mp4"
    shutil.copyfile(reference, normalized)
    _validate_transfer_video_alignment(normalized, width=256, height=256, frames=16, fps=8)
    decoded = decode_generated_video(
        normalized, episode=inspected.episodes[0], fps=8
    )
    assert decoded.shape == (16, 3, 256, 256)
    variant = rewrite_variant(
        inspected,
        output_root=tmp_path / "variant-00",
        output_repo_id="fs2/synthetic-cosmos3-output",
        video_replacements={(0, "observation.images.front"): normalized},
        action_replacements={},
        provenance={
            "operation_id": "00000000-0000-4000-8000-000000000001",
            "source": {"tree_sha256": inspected.tree_sha256, "episode_index": 0},
            "configuration": {"dimension": "lighting", "seed": 20260915},
            "model": {"revision": "7a312c868bcce8e40b3eb40861300a9d0ba3fde1"},
        },
    )
    assert variant.frames == inspected.frames
    assert variant.action_shape == inspected.action_shape
    assert variant.decoded_video_frames == inspected.decoded_video_frames
    for row_index in range(inspected.frames):
        assert (
            variant.dataset.get_raw_item(row_index)["action"].tolist()
            == inspected.dataset.get_raw_item(row_index)["action"].tolist()
        )
    artifact_path = tmp_path / "variant-00.tar.zst"
    artifact = package_dataset(variant.root, artifact_path)
    extracted = tmp_path / "relocalized"
    extract_uploaded_bundle(artifact_path, extracted, expected_sha256=artifact.sha256)
    info, _ = preflight_tree(extracted)
    assert info["total_episodes"] == 1
    assert info["total_frames"] == 16


@pytest.mark.skipif(
    not _has_pinned_lerobot(), reason="requires pinned lerobot==0.6.1 runtime"
)
def test_uploaded_localization_retry_reuses_only_matching_receipt(
    tmp_path: Path,
) -> None:
    source = tmp_path / "tiny-v3"
    subprocess.run(
        [
            sys.executable,
            str(ROOT / "fixtures" / "build_tiny_v3.py"),
            "--output",
            str(source),
        ],
        check=True,
    )
    archive = tmp_path / "source.tar.zst"
    identity = package_dataset(source, archive)
    value = json.loads((ROOT / "fixtures" / "fixture-request.json").read_text())
    value["source"] = {
        "kind": "uploaded-bundle",
        "artifact_id": "00000000-0000-4000-8000-000000000501",
        "sha256": identity.sha256,
        "size_bytes": identity.size_bytes,
        "media_type": "application/x-tar",
        "compression": "zstd",
    }
    request = AugmentationRequest.parse(value)
    digest = hashlib.sha256(request.canonical_json().encode()).hexdigest()
    workspace = tmp_path / "work"
    first = _localize(request, workspace, archive, digest)
    second = _localize(request, workspace, archive, digest)
    assert first == second
    assert preflight_tree(second)[0]["total_frames"] == 16
