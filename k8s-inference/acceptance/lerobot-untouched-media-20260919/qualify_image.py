"""Offline exact-image dataset acceptance using a recorded source/retained generation.

Run in the immutable CPU coordinator image, without a source-code mount or
network. /input holds source/, generated/ and manifest.json; /output is writable.
This checks data/media preservation, not new inference or physical validity.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pyarrow.parquet as pq

from fs2_lerobot_augmentation import dataset as module
from fs2_lerobot_augmentation.contracts import Selection


def metadata(root):
    return {row["episode_index"]: row for path in root.glob("meta/episodes/**/*.parquet")
            for row in pq.read_table(path).to_pylist()}


def main():
    inputs, outputs = Path("/input"), Path("/output")
    manifest = json.loads((inputs / "manifest.json").read_bytes())
    code_sha = module.sha256_file(Path(module.__file__))
    if code_sha != manifest["dataset_source_sha256"]:
        raise ValueError("Published image does not contain the frozen dataset source")
    camera = "observation.images.cam_high"
    source = module.open_and_validate(inputs / "source", repo_id="fs2/recorded-source",
                                      selection=Selection((0,), (camera,)))
    generated = module.open_and_validate(inputs / "generated", repo_id="fs2/retained-public-generation",
                                         selection=Selection((0,), (camera,)))
    assert source.tree_sha256 == manifest["source_tree_sha256"]
    assert generated.tree_sha256 == manifest["generated_tree_sha256"]
    assert source.frames == generated.frames == 128
    reference = outputs / "selected-generated-reference.mp4"
    module.encode_episode_reference(generated, generated.episodes[0], camera, reference)
    before = {}
    preserve = module._preserve_untouched_media

    def capture(inspection, target, replacements, checkpoint):
        before["metadata"] = metadata(target.root)
        before["data"] = {str(path.relative_to(target.root)): module.sha256_file(path)
                          for path in target.root.glob("data/**/*.parquet")}
        before["selected_path"] = str(target.meta.get_video_file_path(0, camera))
        before["selected_sha256"] = module.sha256_file(target.root / before["selected_path"])
        preserve(inspection, target, replacements, checkpoint)

    # Observe the real function's input/output boundary; no generation or writer
    # behavior is substituted. The source module hash above binds its exact code.
    module._preserve_untouched_media = capture
    try:
        output = module.rewrite_variant(
            source, output_root=outputs / "variant-00", output_repo_id="fs2/preserved-result",
            video_replacements={(0, camera): reference}, action_replacements={}, provenance={},
        )
    finally:
        module._preserve_untouched_media = preserve
    artifact = module.package_dataset(output.root, outputs / "variant-00.tar.zst")
    module.extract_uploaded_bundle(outputs / artifact.path, outputs / "relocalized", expected_sha256=artifact.sha256)
    loaded = module.open_and_validate(outputs / "relocalized", repo_id="fs2/relocalized",
                                      selection=Selection("all", "all"), generation_bounds=False)
    assert loaded.decoded_video_frames == 256
    assert module.sha256_file(loaded.root / before["selected_path"]) == before["selected_sha256"]
    for path, digest in before["data"].items():
        assert module.sha256_file(loaded.root / path) == digest
    rows_before, rows_after = metadata(source.root), metadata(loaded.root)
    comparisons = []
    numeric_values = 0
    for episode in source.episodes:
        differences = {key: [] for key in source.cameras}
        for index in range(episode.start, episode.stop):
            left, right = module.source_row(source.dataset, index), module.source_row(loaded.dataset, index)
            for key in set(left) - set(source.cameras):
                a, b = np.asarray(left[key]), np.asarray(right[key])
                assert a.dtype == b.dtype and np.array_equal(a, b), (index, key)
                numeric_values += a.size
            left, right = source.dataset[index], loaded.dataset[index]
            for key in source.cameras:
                differences[key].append(int(np.abs(left[key].numpy().astype("int16") - right[key].numpy().astype("int16")).max()))
        for key in source.cameras:
            selected = (episode.index, key) == (0, camera)
            source_file = source.root / source.dataset.meta.get_video_file_path(episode.index, key)
            returned_file = loaded.root / loaded.dataset.meta.get_video_file_path(episode.index, key)
            source_sha, returned_sha = module.sha256_file(source_file), module.sha256_file(returned_file)
            if not selected:
                assert source_sha == returned_sha and max(differences[key]) == 0
                for suffix in ("from_timestamp", "to_timestamp"):
                    name = f"videos/{key}/{suffix}"
                    assert rows_before[episode.index][name] == rows_after[episode.index][name]
            else:
                assert all(differences[key])
                for name, value in before["metadata"][0].items():
                    if name.startswith((f"videos/{key}/", f"stats/{key}/")):
                        assert rows_after[0][name] == value
            comparisons.append({"episode_index": episode.index, "camera": key, "selected": selected,
                                "frames": episode.frames, "changed_frames": sum(value > 0 for value in differences[key]),
                                "maximum_absolute_pixel_difference": max(differences[key]),
                                "source_mp4_sha256": source_sha, "output_mp4_sha256": returned_sha})
    assert numeric_values == 6144
    receipt = {"status": "passed", "scope": "Published CPU coordinator plus retained public GPU generation; no fresh inference",
               "runtime_image": manifest["runtime_image"], "source_commit": manifest["source_commit"],
               "dataset_source_sha256": code_sha, "reader": "lerobot==0.6.1",
               "source_tree_sha256": source.tree_sha256, "generated_tree_sha256": generated.tree_sha256,
               "artifact_sha256": artifact.sha256, "artifact_size_bytes": artifact.size_bytes,
               "frames": 128, "decoded_frames_each": 256, "nonvideo_values_exact": numeric_values,
               "selected_reference_and_data_shard_bytes_unchanged": True, "media": comparisons,
               "public_end_to_end_qualified": False, "physical_alignment_verified": False,
               "container_finished_at": datetime.now(timezone.utc).isoformat()}
    (outputs / "receipt.json").write_text(json.dumps(receipt, indent=2) + "\n")
    print(json.dumps({"status": "passed", "receipt_sha256": hashlib.sha256((outputs / "receipt.json").read_bytes()).hexdigest()}))


if __name__ == "__main__":
    main()
