from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, FormatChecker

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "runtime" / "src"))

from fs2_lerobot_augmentation.contracts import (  # noqa: E402
    AugmentationRequest,
    ContractError,
    HuggingFaceSource,
)
from fs2_lerobot_augmentation.dataset import (  # noqa: E402
    DatasetError,
    preflight_tree,
    verify_bundle_manifest,
    write_bundle_manifest,
)
from fs2_lerobot_augmentation.localize import (  # noqa: E402
    localize_huggingface,
    verify_source_reference,
)


def request() -> dict:
    return json.loads((ROOT / "fixtures" / "fixture-request.json").read_text())


def test_fixture_request_is_schema_valid_and_semantically_parseable() -> None:
    schema = json.loads((ROOT / "schema" / "request.schema.json").read_text())
    Draft202012Validator.check_schema(schema)
    errors = list(
        Draft202012Validator(schema, format_checker=FormatChecker()).iter_errors(
            request()
        )
    )
    assert errors == []
    parsed = AugmentationRequest.parse(request())
    assert parsed.variant_seeds == (20260915, 20260916)
    assert "lighting (strength=0.70):" in parsed.augmentation.prompt(
        task="pick", episode_index=0, camera="observation.images.front"
    )


@pytest.mark.parametrize(
    "source",
    [
        {"kind": "local", "path": "/tmp/customer.mp4"},
        {"kind": "huggingface", "repo_id": "org/data", "revision": "main"},
        {
            "kind": "object-store",
            "storage_binding": "data",
            "object_prefix": "../secret",
            "manifest_sha256": "a" * 64,
        },
        {
            "kind": "uploaded-bundle",
            "artifact_id": "a",
            "sha256": "a" * 64,
            "size_bytes": 1,
            "media_type": "application/zip",
            "compression": "zstd",
        },
    ],
)
def test_source_contract_is_immutable_and_rejects_client_paths(source: dict) -> None:
    value = request()
    value["source"] = source
    with pytest.raises(ContractError):
        AugmentationRequest.parse(value)


def test_transfer_requires_only_qualified_derived_controls() -> None:
    value = request()
    value["augmentation"]["mode"] = "transfer"
    with pytest.raises(ContractError, match="requires at least one"):
        AugmentationRequest.parse(value)
    value["augmentation"]["conditioning"]["controls"] = ["edge"]
    assert AugmentationRequest.parse(value).augmentation.conditioning.controls == (
        "edge",
    )
    value["augmentation"]["conditioning"]["controls"] = ["depth"]
    with pytest.raises(ContractError, match="unsupported"):
        AugmentationRequest.parse(value)


def test_forward_dynamics_is_rejected_until_runtime_is_qualified() -> None:
    value = request()
    value["augmentation"]["mode"] = "forward-dynamics"
    with pytest.raises(ContractError, match="not qualified"):
        AugmentationRequest.parse(value)


def test_inverse_dynamics_is_rejected_until_runtime_is_qualified() -> None:
    value = request()
    value["actions"] = {"mode": "inverse-dynamics"}
    with pytest.raises(ContractError, match="not qualified"):
        AugmentationRequest.parse(value)


def test_json_schema_rejects_the_same_invalid_action_and_transfer_combinations() -> (
    None
):
    schema = json.loads((ROOT / "schema" / "request.schema.json").read_text())
    validator = Draft202012Validator(schema, format_checker=FormatChecker())
    inverse = request()
    inverse["actions"] = {"mode": "inverse-dynamics"}
    assert list(validator.iter_errors(inverse))
    transfer = request()
    transfer["augmentation"]["mode"] = "transfer"
    assert list(validator.iter_errors(transfer))


def test_template_cannot_traverse_or_reference_unknown_fields() -> None:
    value = request()
    value["augmentation"]["prompt_template"] = "{task.__class__}"
    with pytest.raises(ContractError, match="unsupported placeholders|cannot traverse"):
        AugmentationRequest.parse(value)


def _minimal_tree(root: Path, *, version: str = "v3.0") -> None:
    (root / "meta" / "episodes" / "chunk-000").mkdir(parents=True)
    (root / "data" / "chunk-000").mkdir(parents=True)
    (root / "videos" / "observation.images.front" / "chunk-000").mkdir(parents=True)
    info = {
        "codebase_version": version,
        "fps": 8,
        "total_frames": 16,
        "total_episodes": 1,
        "total_tasks": 1,
        "features": {
            "observation.images.front": {"dtype": "video", "shape": [3, 256, 256]},
            "action": {"dtype": "float32", "shape": [4]},
            "timestamp": {"dtype": "float32", "shape": [1]},
            "frame_index": {"dtype": "int64", "shape": [1]},
            "episode_index": {"dtype": "int64", "shape": [1]},
            "index": {"dtype": "int64", "shape": [1]},
            "task_index": {"dtype": "int64", "shape": [1]},
        },
    }
    (root / "meta" / "info.json").write_text(json.dumps(info))
    (root / "data" / "chunk-000" / "file-000.parquet").write_bytes(b"PAR1dataPAR1")
    (root / "meta" / "episodes" / "chunk-000" / "file-000.parquet").write_bytes(
        b"PAR1episodesPAR1"
    )
    (root / "meta" / "tasks.parquet").write_bytes(b"PAR1tasksPAR1")
    (
        root / "videos" / "observation.images.front" / "chunk-000" / "file-000.mp4"
    ).write_bytes(b"\x00\x00\x00\x18ftypisomfixture")
    write_bundle_manifest(root)


def test_preflight_verifies_complete_inventory_and_container_magic(
    tmp_path: Path,
) -> None:
    root = tmp_path / "dataset"
    _minimal_tree(root)
    info, digest = preflight_tree(root)
    assert info["total_frames"] == 16
    assert len(digest) == 64
    video = root / "videos" / "observation.images.front" / "chunk-000" / "file-000.mp4"
    video.write_bytes(b"corrupt")
    with pytest.raises(DatasetError, match="identity mismatch"):
        preflight_tree(root)


def test_preflight_rejects_v21_before_gpu_work(tmp_path: Path) -> None:
    root = tmp_path / "dataset"
    _minimal_tree(root, version="v2.1")
    with pytest.raises(DatasetError, match="convert v2.1"):
        preflight_tree(root)


def test_huggingface_localization_materializes_the_common_integrity_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def snapshot_download(**kwargs: object) -> None:
        destination = Path(str(kwargs["local_dir"]))
        (destination / "meta").mkdir(parents=True)
        (destination / "meta" / "info.json").write_text("{}")

    monkeypatch.setattr("huggingface_hub.snapshot_download", snapshot_download)
    destination = tmp_path / "localized"
    localize_huggingface(
        HuggingFaceSource(
            kind="huggingface",
            repo_id="fs2/tiny",
            revision="a" * 40,
            credential_binding=None,
        ),
        destination,
    )
    assert (destination / "fs2-bundle-manifest.json").is_file()
    assert len(verify_bundle_manifest(destination)) == 1


def test_source_reference_is_exactly_bound_to_external_source(tmp_path: Path) -> None:
    source = HuggingFaceSource(
        kind="huggingface",
        repo_id="fs2/synthetic-cosmos3-lerobot",
        revision="0" * 40,
        credential_binding=None,
    )
    reference = tmp_path / "source-reference.json"
    reference.write_bytes((ROOT / "fixtures" / "source-reference.json").read_bytes())
    verify_source_reference(reference, source)
    value = json.loads(reference.read_text())
    value["source"]["revision"] = "1" * 40
    reference.write_text(json.dumps(value))
    with pytest.raises(DatasetError, match="differs"):
        verify_source_reference(reference, source)


def test_result_schema_rejects_unvalidated_artifact() -> None:
    schema = json.loads((ROOT / "schema" / "result.schema.json").read_text())
    Draft202012Validator.check_schema(schema)
    result = {
        "schema": "fs2-serve.nebius.ai/cosmos3-lerobot-augmentation-result/v1",
        "operation_id": "00000000-0000-4000-8000-000000000001",
        "status": "succeeded",
        "progress": {"completed_units": 1, "total_units": 1},
        "variants": [
            {
                "variant_index": 0,
                "seed": 1,
                "artifact": {
                    "artifact_id": "a",
                    "sha256": "a" * 64,
                    "size_bytes": 1,
                    "media_type": "application/x-tar",
                    "compression": "zstd",
                },
                "validation": {
                    "reader": "lerobot==0.6.1",
                    "episodes": 1,
                    "frames": 16,
                    "decoded_video_frames": 16,
                    "status": "passed",
                },
                "provenance_sha256": "b" * 64,
            }
        ],
        "failures": [],
    }
    validator = Draft202012Validator(schema, format_checker=FormatChecker())
    assert list(validator.iter_errors(result)) == []
    invalid = copy.deepcopy(result)
    invalid["variants"][0]["validation"]["status"] = "not-run"
    assert list(validator.iter_errors(invalid))
