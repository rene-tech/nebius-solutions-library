"""Exact unselected media preservation with the pinned reader and real recording.

Run with FS2_LEROBOT_RECORDED_ALOHA and FS2_LEROBOT_RETAINED_VARIANT pointing
at the frozen 128-row source and its retained actual public generated variant.
No inference is performed. Source/output decoded equality is not motion fidelity.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "runtime" / "src"))

from fs2_lerobot_augmentation import dataset as module  # noqa: E402
from fs2_lerobot_augmentation.contracts import Selection  # noqa: E402

HIGH = "observation.images.cam_high"
WRIST = "observation.images.cam_right_wrist"


def metadata(root):
    import pyarrow.parquet as pq

    return {row["episode_index"]: row for path in root.glob("meta/episodes/**/*.parquet")
            for row in pq.read_table(path).to_pylist()}


@pytest.fixture(scope="module")
def preserved(tmp_path_factory):
    source_path = os.environ.get("FS2_LEROBOT_RECORDED_ALOHA")
    retained_path = os.environ.get("FS2_LEROBOT_RETAINED_VARIANT")
    if not source_path or not retained_path:
        pytest.skip("requires frozen recorded128-row source and retained public generated variant")
    directory = tmp_path_factory.mktemp("untouched-recorded-media")
    source = module.open_and_validate(Path(source_path), repo_id="fs2/recorded-source",
                                      selection=Selection((0,), (HIGH,)))
    generated = module.open_and_validate(Path(retained_path), repo_id="fs2/retained-generation",
                                         selection=Selection((0,), (HIGH,)))
    assert source.frames == generated.frames == 128
    assert len(source.episodes) == 2 and len(source.cameras) == 2
    reference = directory / "retained-generated-episode0.mp4"
    module.encode_episode_reference(generated, generated.episodes[0], HIGH, reference)
    original_tree = {str(path.relative_to(source.root)): module.sha256_file(path)
                     for path in source.root.rglob("*") if path.is_file()}
    before = {}
    original = module._preserve_untouched_media

    def capture(inspection, target, replacements, checkpoint):
        before["metadata"] = metadata(target.root)
        before["data"] = {path.relative_to(target.root): module.sha256_file(path)
                          for path in target.root.glob("data/**/*.parquet")}
        before["selected_path"] = target.meta.get_video_file_path(0, HIGH)
        before["selected_sha256"] = module.sha256_file(target.root / before["selected_path"])
        before["stats"] = json.loads((target.root / "meta/stats.json").read_text())
        original(inspection, target, replacements, checkpoint)

    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr(module, "_preserve_untouched_media", capture)
        output = module.rewrite_variant(
            source, output_root=directory / "variant-00", output_repo_id="fs2/preserved-result",
            video_replacements={(0, HIGH): reference}, action_replacements={}, provenance={},
        )
    return source, output, before, original_tree


def assert_untouched(source, output):
    before, after = metadata(source.root), metadata(output.root)
    for episode in source.episodes:
        for camera in source.cameras:
            if (episode.index, camera) == (0, HIGH):
                continue
            original_file = source.root / source.dataset.meta.get_video_file_path(episode.index, camera)
            output_file = output.root / output.dataset.meta.get_video_file_path(episode.index, camera)
            assert module.sha256_file(original_file) == module.sha256_file(output_file)
            for suffix in ("from_timestamp", "to_timestamp"):
                key = f"videos/{camera}/{suffix}"
                assert before[episode.index][key] == after[episode.index][key]
            for key, value in before[episode.index].items():
                if key.startswith(f"stats/{camera}/"):
                    assert after[episode.index][key] == value
    for episode in source.episodes:
        for index in range(episode.start, episode.stop):
            left, right = source.dataset[index], output.dataset[index]
            for camera in source.cameras:
                if (episode.index, camera) != (0, HIGH):
                    assert np.array_equal(left[camera].numpy(), right[camera].numpy()), (index, camera)


def test_shared_chunk_preserves_source_bytes_offsets_and_every_untouched_frame(preserved):
    source, output, _, _ = preserved
    # The real source's episodes share one camera shard. The selected episode
    # must not be overwritten when preserving the other episode in that shard.
    assert source.dataset.meta.get_video_file_path(0, HIGH) == source.dataset.meta.get_video_file_path(1, HIGH)
    assert output.dataset.meta.get_video_file_path(0, HIGH) != output.dataset.meta.get_video_file_path(1, HIGH)
    assert output.dataset.meta.get_video_file_path(0, WRIST) == output.dataset.meta.get_video_file_path(1, WRIST)
    assert_untouched(source, output)


def test_selected_generated_reference_and_numeric_writer_bytes_remain_unchanged(preserved):
    source, output, before, original_tree = preserved
    assert output.dataset.meta.get_video_file_path(0, HIGH) == before["selected_path"]
    assert module.sha256_file(output.root / before["selected_path"]) == before["selected_sha256"]
    after = metadata(output.root)
    for key, value in before["metadata"][0].items():
        if key.startswith((f"videos/{HIGH}/", f"stats/{HIGH}/")):
            assert after[0][key] == value
    for path, digest in before["data"].items():
        assert module.sha256_file(output.root / path) == digest
    changed = 0
    for index in range(source.frames):
        left, right = module.source_row(source.dataset, index), module.source_row(output.dataset, index)
        for key in set(left) - set(source.cameras):
            a, b = np.asarray(left[key]), np.asarray(right[key])
            assert a.dtype == b.dtype and np.array_equal(a, b), (index, key)
        if index < source.episodes[0].stop:
            changed += not np.array_equal(source.dataset[index][HIGH].numpy(), output.dataset[index][HIGH].numpy())
    assert changed == 64  # Actual retained generation, not merely a renamed source.
    assert original_tree == {str(path.relative_to(source.root)): module.sha256_file(path)
                             for path in source.root.rglob("*") if path.is_file()}


def test_preserved_video_statistics_aggregate_and_unselected_codec_info(preserved):
    from lerobot.datasets.compute_stats import aggregate_stats

    source, output, before, _ = preserved
    after = metadata(output.root)
    expected = aggregate_stats([
        {camera: {key.removeprefix(f"stats/{camera}/"): np.asarray(value)
                  for key, value in row.items() if key.startswith(f"stats/{camera}/")}
         for camera in source.cameras}
        for row in after.values()
    ])
    observed = json.loads((output.root / "meta/stats.json").read_text())
    for camera in source.cameras:
        for name, value in expected[camera].items():
            assert np.array_equal(value, np.asarray(observed[camera][name]))
    for feature in set(observed) - set(source.cameras):
        assert observed[feature] == before["stats"][feature]
    assert output.dataset.features[WRIST] == source.dataset.features[WRIST]


def test_archive_relocalization_keeps_byte_and_full_decoded_equality(preserved, tmp_path):
    source, output, _, _ = preserved
    archive = tmp_path / "result.tar.zst"
    receipt = module.package_dataset(output.root, archive)
    module.extract_uploaded_bundle(archive, tmp_path / "relocalized", expected_sha256=receipt.sha256)
    loaded = module.open_and_validate(tmp_path / "relocalized", repo_id="fs2/reloaded",
                                      selection=Selection("all", "all"), generation_bounds=False)
    assert loaded.frames == 128 and loaded.decoded_video_frames == 256
    assert_untouched(source, loaded)


def test_copy_checkpoint_propagates_cancellation_without_source_mutation(preserved, tmp_path):
    import shutil
    from types import SimpleNamespace

    from fs2_lerobot_augmentation.worker import CancelledError

    source, output, _, original_tree = preserved
    destination = tmp_path / "variant-cancel"
    shutil.copytree(output.root, destination)
    target = SimpleNamespace(root=destination, meta=output.dataset.meta)
    count = 0

    def checkpoint():
        nonlocal count
        count += 1
        if count == 2:  # First copied file block, after metadata-loop checkpoint.
            raise CancelledError

    with pytest.raises(CancelledError):
        module._preserve_untouched_media(source, target, {(0, HIGH): Path("unused")}, checkpoint)
    assert count == 2
    assert original_tree == {str(path.relative_to(source.root)): module.sha256_file(path)
                             for path in source.root.rglob("*") if path.is_file()}
