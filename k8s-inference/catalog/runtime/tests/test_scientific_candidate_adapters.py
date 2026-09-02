"""Coverage for candidate adapters that share the canonical controller contract."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from fs2_serve.scientific_batch import compile_adapter_run, profile_from_catalog
from fs2_serve.scientific_batch.adapters import bindcraft
from fs2_serve.scientific_batch.adapters.common import ScientificAdapterError, runtime_recipe_sha256

ROOT = Path(__file__).resolve().parents[3]
PROFILES = json.loads(
    (ROOT / "catalog/runtime/contracts/scientific-workload-profiles.json").read_text(encoding="utf-8")
)

PARAMETERS = {
    "freebindcraft": {
        "binder_length": 64,
        "scoring_mode": "open-source-bypass",
        "target_chains": ["A"],
        "trajectories": 2,
    },
    "mosaic": {"objective_profile_id": "minibinder", "iterations": 8, "seed": 7},
    "proteinmpnn": {"num_sequences": 4, "sampling_temperature": 0.2, "seed": 11},
    "rfdiffusion": {
        "contigs": [{"chain": "A", "start": 1, "end": 50}],
        "design_count": 3,
        "hotspot_residues": ["A10"],
        "seed": 13,
    },
}


def _profile(model_id: str):
    return profile_from_catalog(PROFILES, model_id)


def _request(model_id: str, parameters: dict[str, object]) -> dict[str, object]:
    profile = _profile(model_id)
    return {
        "schema": "fs2-serve.nebius.ai/scientific-run-request/v1",
        "operation": profile["interface"]["operations"][0],
        "service_class": profile["interface"]["service_classes"][0],
        "input_manifest": {
            "artifact_id": f"input.{model_id}",
            "sha256": "0" * 64,
            "size_bytes": 1024,
            "media_type": "application/vnd.fs2.scientific-manifest+json",
            "compression": "none",
        },
        "parameters": parameters,
    }


def test_every_registered_profile_binds_the_exact_controller_recipe() -> None:
    for profile in PROFILES["profiles"]:
        assert profile["execution_identity"]["runtime_recipe_sha256"] == runtime_recipe_sha256(
            ROOT,
            profile["model_id"],
        )


@pytest.mark.parametrize("model_id", tuple(PARAMETERS))
def test_candidate_adapters_bind_typed_parameters_and_immutable_mounts(model_id: str) -> None:
    profile = _profile(model_id)
    plan = compile_adapter_run(
        model_id,
        profile,
        _request(model_id, PARAMETERS[model_id]),
        operation_id=f"op-{model_id}-candidate",
        variant_id=profile["variant_id"],
    )
    assert profile["route_exposed"] is False
    assert plan.model_id == model_id
    assert plan.variant_id == profile["variant_id"]
    assert len(plan.invocations) == 1
    invocation = plan.invocations[0]
    assert invocation.materializations[0].artifact_id == f"input.{model_id}"
    assert invocation.runtime_mounts
    assert all(mount.read_only for mount in invocation.runtime_mounts)
    assert all(mount.ownership_policy == "pre-owned-no-recursive-chown" for mount in invocation.runtime_mounts)
    argv = "\0".join(invocation.argv)
    assert "s3://" not in argv and "file://" not in argv
    for value in PARAMETERS[model_id].values():
        if isinstance(value, str | int | float):
            assert str(value) in argv

    hostile = _request(model_id, {**PARAMETERS[model_id], "args": ["--online"]})
    with pytest.raises(ScientificAdapterError, match="parameters do not match"):
        compile_adapter_run(
            model_id,
            profile,
            hostile,
            operation_id=f"op-{model_id}-hostile",
            variant_id=profile["variant_id"],
        )


def test_bindcraft_projects_private_installed_tree_without_per_job_wheel_or_license_gate() -> None:
    model_id = "bindcraft"
    profile = _profile(model_id)
    request = _request(
        model_id,
        {"binder_length": 64, "target_chains": ["A"], "trajectories": 2},
    )
    plan = compile_adapter_run(
        model_id,
        profile,
        request,
        operation_id="op-bindcraft-candidate",
        variant_id=bindcraft.VARIANT_ID,
    )
    assert len(plan.invocations) == 3
    assert tuple(stage.stage_id for stage in plan.controller_plan.stages) == ("design", "relax-score")
    for invocation in plan.invocations:
        mount = next(
            item
            for item in invocation.runtime_mounts
            if item.artifact_id == bindcraft.PYROSETTA_RUNTIME_ARTIFACT
        )
        assert mount.mount_path == "/opt/fs2/academic/pyrosetta"
        assert mount.supplemental_groups == (65532,)
        assert mount.authorization_receipt_sha256 is None
        assert not any(argument.endswith(".whl") for argument in invocation.argv)
