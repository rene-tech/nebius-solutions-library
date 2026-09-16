from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from fs2_serve.mindguard_artifacts import MindGuardV2Bundle, MindGuardV2EventBinding, validate_private_bundle


def artifact(tmp_path: Path) -> dict:
    def file(name: str, contents: str) -> dict:
        data = contents.encode()
        (tmp_path / name).write_bytes(data)
        return {"path": name, "sha256": hashlib.sha256(data).hexdigest(), "size_bytes": len(data)}

    return {
        "artifact_id": "sword-mindguard-v2-test-fixture",
        "source_uri": "s3://approved-test-bucket/model",
        "source_revision": "a" * 40,
        "approval_record": file("approval.json", '{"fixture": true}'),
        "config": file("config.json", '{"max_position_embeddings": 32768}'),
        "tokenizer": file("tokenizer.json", "{}"),
        "tokenizer_config": file("tokenizer_config.json", "{}"),
        "chat_template": file("chat_template.jinja", "test fixture template"),
        "weights": [file("model.safetensors", "test fixture bytes, not model weights")],
        "serving": {
            "engine": "vllm",
            "image": "registry.invalid/model@sha256:" + "b" * 64,
            "tensor_parallel_size": 2,
            "known_good_configuration": file("serving.json", "{}"),
        },
    }


def test_valid_bundle_proves_integrity_but_never_fakes_gpu_qualification(tmp_path: Path) -> None:
    bundle = MindGuardV2Bundle.model_validate(artifact(tmp_path))
    result = validate_private_bundle(bundle, tmp_path)
    assert result["status"] == "artifact_integrity_verified"
    assert result["gpu_qualified"] is False
    assert result["mindeval_accepted"] is False
    assert len(result["manifest_sha256"]) == 64


def test_tampered_weights_and_external_symlink_are_rejected(tmp_path: Path) -> None:
    bundle = MindGuardV2Bundle.model_validate(artifact(tmp_path))
    weight = tmp_path / "model.safetensors"
    weight.write_text("changed")
    with pytest.raises(ValueError, match="size mismatch"):
        validate_private_bundle(bundle, tmp_path)
    weight.unlink()
    weight.symlink_to(tmp_path.parent / "elsewhere.safetensors")
    with pytest.raises((ValueError, FileNotFoundError)):
        validate_private_bundle(bundle, tmp_path)


@pytest.mark.parametrize(
    "source",
    [
        "https://huggingface.co/Qwen/Qwen3.6-35B-A3B",
        "https://token:secret@weights.invalid/model",
        "https://weights.invalid/model?signature=secret",
    ],
)
def test_public_base_or_embedded_credentials_rejected(tmp_path: Path, source: str) -> None:
    value = artifact(tmp_path)
    value["source_uri"] = source
    with pytest.raises(ValidationError):
        MindGuardV2Bundle.model_validate(value)


def test_insufficient_context_rejected(tmp_path: Path) -> None:
    value = artifact(tmp_path)
    data = json.dumps({"max_position_embeddings": 4096}).encode()
    (tmp_path / "config.json").write_bytes(data)
    value["config"]["sha256"] = hashlib.sha256(data).hexdigest()
    value["config"]["size_bytes"] = len(data)
    with pytest.raises(ValueError, match="32768"):
        validate_private_bundle(MindGuardV2Bundle.model_validate(value), tmp_path)


def test_event_binding_cannot_fallback_to_production_or_enable_fp8() -> None:
    value = {
        "event_id": "workshop-2026",
        "namespace": "fs2-models",
        "model_id": "mindguard-v2-event-workshop-2026",
        "endpoint": "http://mindguard-v2-event-workshop-2026.fs2-models.svc.cluster.local:8000/v1",
        "artifact_manifest_sha256": "a" * 64,
        "min_hot_replicas": 1,
    }
    assert MindGuardV2EventBinding.model_validate(value).event_only
    for change in [
        {"endpoint": "https://production.swordhealth.invalid/v1"},
        {"precision": "fp8"},
        {"production_fallback": True},
        {"min_hot_replicas": 0},
        {"model_id": "mindguard-8b"},
    ]:
        with pytest.raises(ValidationError):
            MindGuardV2EventBinding.model_validate({**value, **change})
