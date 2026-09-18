from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "runtime/src"))

from fs2_lerobot_augmentation import worker  # noqa: E402
from fs2_lerobot_augmentation.contracts import MAX_BUNDLE_BYTES, AugmentationRequest, ContractError  # noqa: E402
from fs2_lerobot_augmentation.dataset import DatasetError, Episode, package_dataset  # noqa: E402


def inspection(root, *, frames=32, selected=1):
    return SimpleNamespace(
        root=root,
        frames=frames,
        cameras=("observation.images.front",),
        episodes=(Episode(0, 0, frames, "robot"),),
        selected_episodes=tuple(range(selected)),
        selected_cameras=("observation.images.front",),
        dataset=SimpleNamespace(features={"observation.images.front": {"shape": [3, 352, 640]}}),
        tree_sha256="a" * 64,
    )


def test_input_contract_and_published_schema_have_same_five_gib_bound():
    value = json.loads((ROOT / "fixtures/fixture-request.json").read_text())
    value["source"] = {
        "kind": "uploaded-bundle",
        "artifact_id": "dataset-test",
        "sha256": "a" * 64,
        "size_bytes": MAX_BUNDLE_BYTES,
        "media_type": "application/x-tar",
        "compression": "zstd",
    }
    assert AugmentationRequest.parse(value).source.size_bytes == MAX_BUNDLE_BYTES
    value["source"]["size_bytes"] += 1
    with pytest.raises(ContractError, match="size_bytes"):
        AugmentationRequest.parse(value)
    schema = json.loads((ROOT / "schema/request.schema.json").read_text())
    assert schema["$defs"]["uploaded_bundle_source"]["properties"]["size_bytes"]["maximum"] == MAX_BUNDLE_BYTES


def test_small_eight_variant_request_fits_but_large_generation_fanout_rejected(tmp_path):
    small = worker.workspace_plan(inspection(tmp_path), variants=8, workspace=tmp_path, source_artifact=None)
    assert 0 < small["estimated_peak_bytes"] < worker.WORKSPACE_BYTES
    with pytest.raises(DatasetError, match="estimated"):
        worker.workspace_plan(inspection(tmp_path, selected=32), variants=1, workspace=tmp_path, source_artifact=None)


def test_workspace_rejection_happens_before_any_cosmos_client_or_publication(tmp_path, monkeypatch):
    value = json.loads((ROOT / "fixtures/fixture-request.json").read_text())
    request = AugmentationRequest.parse(value)
    monkeypatch.setattr(worker.Cancellation, "install", lambda self: None)
    monkeypatch.setattr(worker, "_localize", lambda *args: tmp_path)
    monkeypatch.setattr(worker, "open_and_validate", lambda *args, **kwargs: inspection(tmp_path, frames=100000))

    def forbidden_client(*args, **kwargs):
        pytest.fail("Cosmos must not be contacted before workspace acceptance")

    monkeypatch.setattr(worker, "CosmosClient", forbidden_client)
    with pytest.raises(DatasetError, match="estimated"):
        worker.run(
            request,
            operation_id="00000000-0000-4000-8000-000000000001",
            workspace=tmp_path,
            platform_base_url="https://unused.invalid",
        )
    assert not (tmp_path / "result.json").exists()
    assert not (tmp_path / "progress.jsonl").exists()


def test_insufficient_disk_rejected_before_generation(tmp_path, monkeypatch):
    monkeypatch.setattr(worker.shutil, "disk_usage", lambda path: SimpleNamespace(free=0))
    with pytest.raises(DatasetError, match="insufficient free"):
        worker.workspace_plan(inspection(tmp_path), variants=1, workspace=tmp_path, source_artifact=None)


def test_oversized_output_is_not_published(tmp_path, monkeypatch):
    from fs2_lerobot_augmentation import dataset

    source = tmp_path / "source"
    source.mkdir()
    (source / "data").write_bytes(bytes(range(256)) * 100)
    monkeypatch.setattr(dataset, "MAX_BUNDLE_BYTES", 16)
    output = tmp_path / "result.tar.zst"
    with pytest.raises(DatasetError, match="compressed bound"):
        package_dataset(source, output)
    assert not output.exists()


def test_oversized_huggingface_source_rejected_before_download(tmp_path, monkeypatch):
    from fs2_lerobot_augmentation.contracts import HuggingFaceSource
    from fs2_lerobot_augmentation.localize import localize_huggingface

    monkeypatch.setattr(
        "huggingface_hub.HfApi.list_repo_tree", lambda *args, **kwargs: [SimpleNamespace(size=9 * 1024**3)]
    )
    monkeypatch.setattr(
        "huggingface_hub.snapshot_download", lambda **kwargs: pytest.fail("must reject before download")
    )
    with pytest.raises(DatasetError, match="8 GiB"):
        localize_huggingface(HuggingFaceSource("huggingface", "fs2/test", "a" * 40, None), tmp_path / "source")
