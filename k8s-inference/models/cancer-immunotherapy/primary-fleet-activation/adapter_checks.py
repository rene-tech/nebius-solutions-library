#!/usr/bin/env python3
"""Compile the model-owned requests through every merged primary adapter."""

from __future__ import annotations

import copy
import json
import sys
from pathlib import Path
from typing import Any
from uuid import UUID

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "components/control-plane/src"))
sys.path.insert(0, str(ROOT / "catalog/runtime"))

from fs2_serve.scientific_batch import ScientificInputArtifact  # noqa: E402
from fs2_serve.scientific_batch.adapters import bindcraft, proteina_complexa  # noqa: E402


def load(path: str) -> dict[str, Any]:
    value = json.loads((ROOT / path).read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def overlay(target: dict[str, Any], source: dict[str, Any]) -> None:
    for key, value in source.items():
        if isinstance(value, dict) and isinstance(target.get(key), dict):
            overlay(target[key], value)
        else:
            target[key] = copy.deepcopy(value)


def compile_fragment(fragment_path: str, compiler: Any, operation_id: str) -> None:
    fragment = load(fragment_path)
    profile = copy.deepcopy(fragment["profile_projection"]["profile"])
    request = load(fragment["public_fixtures"]["request"])
    execution = fragment["execution_projection"]
    overlay(profile, execution.get("adapter_profile_overlay", {}))
    compiler_arguments: dict[str, object] = {}
    if fragment["model_id"] == "proteina-complexa":
        manifest_declaration = next(
            item
            for item in fragment["public_fixtures"]["supporting_inputs"]
            if item["role"] == "request-input-manifest"
        )
        manifest = load(manifest_declaration["path"])
        compiler_arguments["input_artifacts"] = tuple(
            ScientificInputArtifact(
                logical_artifact_id=entry["name"],
                semantic_type=entry["semantic_type"],
                artifact_id=UUID(int=index),
                digest="sha256:" + entry["artifact"]["sha256"],
                size_bytes=entry["artifact"]["size_bytes"],
                media_type=entry["artifact"]["media_type"],
                compression=entry["artifact"].get("compression"),
            )
            for index, entry in enumerate(manifest["entries"], start=1)
        )
    plan = compiler(
        profile,
        request,
        operation_id=operation_id,
        **compiler_arguments,
    )

    expected_stages = {item["id"] for item in execution["stages"]}
    actual_stages = {item.stage_id for item in plan.invocations}
    if actual_stages != expected_stages:
        raise AssertionError(
            f"{fragment['model_id']} stage mismatch: {actual_stages} != {expected_stages}"
        )
    admitted_artifacts = {
        item["artifact_id"] for item in execution["runtime_artifacts"]
    }
    requested_artifacts = set(plan.required_model_artifacts)
    if not requested_artifacts <= admitted_artifacts:
        missing = sorted(requested_artifacts - admitted_artifacts)
        raise AssertionError(
            f"{fragment['model_id']} adapter requires unbound artifacts: {missing}"
        )


def main() -> int:
    compile_fragment(
        "models/cancer-immunotherapy/runtime-images/proteina-complexa/activation/fragment.json",
        proteina_complexa.compile_run,
        "op-primary-activation-proteina-complexa",
    )
    compile_fragment(
        "models/cancer-immunotherapy/images/bindcraft-native/activation/fragment.json",
        bindcraft.compile_run,
        "op-primary-activation-bindcraft",
    )
    print("merged adapter compilation: proteina-complexa, bindcraft: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
