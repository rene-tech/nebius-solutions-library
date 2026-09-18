from __future__ import annotations

import gc
import json
import os
import sys
import weakref
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "runtime" / "src"), str(ROOT / "fixtures")]

from build_robot_multiview import FRONT, REPO_ID, build_fixture  # noqa: E402
from fs2_lerobot_augmentation import dataset as dataset_module  # noqa: E402
from fs2_lerobot_augmentation import worker  # noqa: E402
from fs2_lerobot_augmentation.contracts import AugmentationRequest, Selection  # noqa: E402
from fs2_lerobot_augmentation.cosmos import CosmosError, CosmosGeneration  # noqa: E402
from fs2_lerobot_augmentation.dataset import (  # noqa: E402
    DatasetError,
    encode_episode_reference,
    extract_uploaded_bundle,
    open_and_validate,
    package_dataset,
    rewrite_variant,
)
from validate_variant import compare_variant  # noqa: E402


@pytest.fixture(scope="module")
def source(tmp_path_factory):
    assets = os.environ.get("FS2_LEROBOT_FIXTURE_ASSETS")
    if assets is None:
        pytest.skip("set FS2_LEROBOT_FIXTURE_ASSETS to pinned public robot MP4/action fixtures")
    root = tmp_path_factory.mktemp("robot-multiepisode") / "source"
    build_fixture(
        root,
        video=Path(assets) / "official-robot.mp4",
        actions=Path(assets) / "official-actions.json",
        episode_frames=(16, 8),
        small_unselected_camera=True,
        distinct_tasks=True,
    )
    return open_and_validate(root, repo_id=REPO_ID, selection=Selection(episodes=(0,), cameras=(FRONT,)))


def test_subset_keeps_short_episode_small_camera_and_full_relocalized_reader(source, tmp_path):
    assert source.frames == 24 and source.decoded_video_frames == 48
    assert [episode.frames for episode in source.episodes] == [16, 8]
    assert len({episode.task for episode in source.episodes}) == 2
    with pytest.raises(DatasetError, match="shape is outside"):
        open_and_validate(source.root, repo_id=REPO_ID, selection=Selection(episodes="all", cameras="all"))
    reference = tmp_path / "reference.mp4"
    encode_episode_reference(source, source.episodes[0], FRONT, reference)
    provenance = {"source": {"tree_sha256": source.tree_sha256}, "operations": [{"episode_index": 0, "camera": FRONT}]}
    result = rewrite_variant(
        source,
        output_root=tmp_path / "variant-00",
        output_repo_id="fs2/multiepisode-result",
        video_replacements={(0, FRONT): reference},
        action_replacements={},
        provenance=provenance,
    )
    comparison = compare_variant(source, result, replacements={(0, FRONT)}, provenance=provenance)
    assert comparison["decoded_frames_each"] == 48
    assert comparison["nonvideo_values_exact"] and not comparison["physical_alignment_verified"]
    archive = tmp_path / "result.tar.zst"
    identity = package_dataset(result.root, archive)
    extract_uploaded_bundle(archive, tmp_path / "reloaded", expected_sha256=identity.sha256)
    loaded = open_and_validate(
        tmp_path / "reloaded",
        repo_id="fs2/multiepisode-result",
        selection=Selection(episodes="all", cameras="all"),
        generation_bounds=False,
    )
    assert compare_variant(source, loaded, replacements={(0, FRONT)})["status"] == "passed"


def test_real_preview_output_survives_complete_multiepisode_dataset(source, tmp_path):
    media = os.environ.get("FS2_LEROBOT_GENERATED_VIDEO")
    if media is None:
        pytest.skip("set FS2_LEROBOT_GENERATED_VIDEO to retained real GPU transfer output")
    result = rewrite_variant(
        source,
        output_root=tmp_path / "variant-00",
        output_repo_id="fs2/real-preview-result",
        video_replacements={(0, FRONT): Path(media)},
        action_replacements={},
        provenance={},
    )
    proof = compare_variant(source, result, replacements={(0, FRONT)})
    selected = [row for row in proof["visual_comparison"] if row["selected"]]
    assert selected[0]["changed_frames"] > 0
    assert selected[0]["mean_absolute_pixel_difference"] > 1
    assert not proof["physical_alignment_verified"]


def test_replacements_are_released_before_decoding_next_episode(source, tmp_path, monkeypatch):
    references = {}
    for episode in source.episodes:
        path = tmp_path / f"reference-{episode.index}.mp4"
        encode_episode_reference(source, episode, FRONT, path)
        references[(episode.index, FRONT)] = path
    decode = dataset_module.decode_generated_video
    prior = []

    def observed_decode(*args, **kwargs):
        gc.collect()
        assert all(item() is None for item in prior), "previous episode tensor is still retained"
        frames = decode(*args, **kwargs)
        prior.append(weakref.ref(frames))
        return frames

    monkeypatch.setattr(dataset_module, "decode_generated_video", observed_decode)
    output = rewrite_variant(
        source,
        output_root=tmp_path / "variant-00",
        output_repo_id="fs2/bounded-result",
        video_replacements=references,
        action_replacements={},
        provenance={},
    )
    assert output.frames == source.frames and len(prior) == 2


def test_rewrite_cancel_does_not_publish_provenance_or_manifest(source, tmp_path):
    checks = 0

    def checkpoint():
        nonlocal checks
        checks += 1
        if checks == 4:
            raise worker.CancelledError

    destination = tmp_path / "variant-00"
    with pytest.raises(worker.CancelledError):
        rewrite_variant(
            source,
            output_root=destination,
            output_repo_id="fs2/cancelled-result",
            video_replacements={},
            action_replacements={},
            provenance={},
            checkpoint=checkpoint,
        )
    assert not (destination / "fs2-bundle-manifest.json").exists()
    assert not (destination / "meta/fs2-augmentation-provenance.json").exists()


@pytest.mark.parametrize("failure", ["cancel", "error"])
def test_final_generation_cancel_or_error_does_not_publish_dataset(source, tmp_path, monkeypatch, failure):
    cancellation = worker.Cancellation()
    monkeypatch.setattr(worker.Cancellation, "install", lambda self: None)
    monkeypatch.setattr(worker, "Cancellation", lambda: cancellation)
    value = json.loads((ROOT / "fixtures/fixture-request.json").read_text())
    value["selection"] = {"episodes": [0], "cameras": [FRONT]}
    value["variants"] = {"count": 1, "seeds": [100]}
    value["failure_policy"] = {"mode": "fail-fast", "max_attempts": 1}
    request = AugmentationRequest.parse(value)
    monkeypatch.setattr(worker, "_localize", lambda *args: source.root)

    class Client:
        def __init__(self, *args, **kwargs):
            pass

        def generate(self, **kwargs):
            if failure == "error":
                raise CosmosError("MEDIA_FAILED", "generation failed", retryable=False)
            cancellation.requested = True
            return CosmosGeneration(kwargs["reference"], "00000000-0000-4000-8000-000000000002", "transfer-video")

    monkeypatch.setattr(worker, "CosmosClient", Client)
    workspace = tmp_path / "run"
    with pytest.raises(worker.CancelledError if failure == "cancel" else CosmosError):
        worker.run(
            request,
            operation_id="00000000-0000-4000-8000-000000000001",
            workspace=workspace,
            platform_base_url="https://inference.test.invalid",
        )
    assert not (workspace / "result.json").exists()
    assert not (workspace / "completion-receipt.json").exists()
    assert not (workspace / "artifacts").exists()


def test_canonical_float64_timestamps_survive_complete_rewrite(tmp_path):
    assets = os.environ.get("FS2_LEROBOT_FIXTURE_ASSETS")
    if assets is None:
        pytest.skip("requires pinned public robot fixtures")
    root = tmp_path / "source64"
    build_fixture(
        root,
        video=Path(assets) / "official-robot.mp4",
        actions=Path(assets) / "official-actions.json",
        episode_frames=(16, 8),
        small_unselected_camera=True,
        timestamp_dtype="float64",
        distinct_tasks=True,
    )
    source64 = open_and_validate(root, repo_id=REPO_ID, selection=Selection((0,), (FRONT,)))
    output = rewrite_variant(
        source64,
        output_root=tmp_path / "variant-64",
        output_repo_id="fs2/float64-result",
        video_replacements={},
        action_replacements={},
        provenance={},
    )
    assert str(dataset_module.source_row(output.dataset, 1)["timestamp"].dtype) == "float64"
    assert compare_variant(source64, output, replacements=set())["nonvideo_values_exact"]


@pytest.mark.parametrize("defect", ["timestamp", "task_index"])
def test_noncanonical_timing_or_task_order_rejected_before_child(source, tmp_path, monkeypatch, defect):
    original = dataset_module.source_row

    def changed(dataset, index, **kwargs):
        row = dict(original(dataset, index, **kwargs))
        if defect == "timestamp" and index == 1:
            row["timestamp"] = row["timestamp"] + 0.00001
        if defect == "task_index":
            row["task_index"] = 1 - row["task_index"]
        return row

    monkeypatch.setattr(dataset_module, "source_row", changed)
    monkeypatch.setattr("lerobot.datasets.lerobot_dataset.LeRobotDataset", lambda *args, **kwargs: source.dataset)
    monkeypatch.setattr(worker.Cancellation, "install", lambda self: None)
    monkeypatch.setattr(worker, "_localize", lambda *args: source.root)
    monkeypatch.setattr(
        worker, "CosmosClient", lambda *args, **kwargs: pytest.fail("no child before source validation")
    )
    value = json.loads((ROOT / "fixtures/fixture-request.json").read_text())
    value["selection"] = {"episodes": [0], "cameras": [FRONT]}
    with pytest.raises(DatasetError, match="unsupported noncanonical " + defect):
        worker.run(
            AugmentationRequest.parse(value),
            operation_id="00000000-0000-4000-8000-000000000001",
            workspace=tmp_path,
            platform_base_url="https://unused.invalid",
        )
    assert not (tmp_path / "result.json").exists()
