import copy
from pathlib import Path

import pytest

from prepare_promotion import validate_evidence


def records():
    publication = {"runtime_image": "registry/model@sha256:" + "a" * 64,
                   "context_files": {"src/fs2_lerobot_augmentation/dataset.py": "b" * 64}}
    evidence = {"status": "passed", "runtime_image": publication["runtime_image"],
                "dataset_source_sha256": "b" * 64, "frames": 128, "decoded_frames_each": 256,
                "nonvideo_values_exact": 6144, "selected_reference_and_data_shard_bytes_unchanged": True,
                "thread_settings": {"torch_intraop": 4, "torch_interop": 64, "environment": {
                    "OMP_NUM_THREADS": "4", "MKL_NUM_THREADS": "4", "OPENBLAS_NUM_THREADS": "4"}},
                "public_end_to_end_qualified": False, "media": []}
    for episode in (0, 1):
        for camera in ("observation.images.cam_high", "observation.images.cam_right_wrist"):
            selected = (episode, camera) == (0, "observation.images.cam_high")
            evidence["media"].append({"episode_index": episode, "camera": camera, "selected": selected,
                                      "frames": 64, "changed_frames": 64 if selected else 0,
                                      "maximum_absolute_pixel_difference": 40 if selected else 0,
                                      "source_mp4_sha256": "c" * 64, "output_mp4_sha256": "d" * 64 if selected else "c" * 64})
    return publication, evidence


def test_exact_recorded_proof_is_accepted_without_mutating_evidence():
    publication, evidence = records()
    original = copy.deepcopy(evidence)
    validate_evidence(publication, evidence)
    assert evidence == original


@pytest.mark.parametrize("key,value", [("runtime_image", "other"), ("dataset_source_sha256", "x"),
                                     ("nonvideo_values_exact", 6143), ("decoded_frames_each", 128),
                                     ("public_end_to_end_qualified", True),
                                     ("selected_reference_and_data_shard_bytes_unchanged", False)])
def test_wrong_image_source_or_broadened_claim_is_rejected(key, value):
    publication, evidence = records()
    evidence[key] = value
    with pytest.raises(ValueError):
        validate_evidence(publication, evidence)


@pytest.mark.parametrize("key,value", [("changed_frames", 1), ("maximum_absolute_pixel_difference", 1),
                                     ("output_mp4_sha256", "x"), ("selected", True), ("frames", 63)])
def test_tolerance_pass_cannot_replace_exact_untouched_acceptance(key, value):
    publication, evidence = records()
    evidence["media"][1][key] = value
    with pytest.raises(ValueError):
        validate_evidence(publication, evidence)


def test_missing_camera_and_unchanged_selected_generation_are_rejected():
    publication, evidence = records()
    evidence["media"].pop()
    with pytest.raises(ValueError):
        validate_evidence(publication, evidence)
    publication, evidence = records()
    evidence["media"][0]["changed_frames"] = 0
    with pytest.raises(ValueError):
        validate_evidence(publication, evidence)


def test_image_defaults_bind_only_current_library_thread_limit():
    path = Path(__file__).resolve().parents[2] / "models/general-media/lerobot-augmentation/runtime/Containerfile.untouched-media"
    text = path.read_text()
    assert "ENV OMP_NUM_THREADS=4 MKL_NUM_THREADS=4 OPENBLAS_NUM_THREADS=4" in text
    assert "set_num_interop_threads" not in text


@pytest.mark.parametrize("change", ["torch_intraop", "OMP_NUM_THREADS", "MKL_NUM_THREADS", "OPENBLAS_NUM_THREADS"])
def test_unqualified_thread_configuration_is_rejected(change):
    publication, evidence = records()
    if change == "torch_intraop":
        evidence["thread_settings"][change] = 64
    else:
        evidence["thread_settings"]["environment"][change] = "64"
    with pytest.raises(ValueError):
        validate_evidence(publication, evidence)
