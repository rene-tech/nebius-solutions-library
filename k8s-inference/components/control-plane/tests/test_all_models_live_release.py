from __future__ import annotations

import copy
import json
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from conftest import CATALOG_ROOT, CONTROL_ROOT, REPO_ROOT
from fs2_serve_catalog.consumer import SERVING_BINDINGS_SCHEMA
from fs2_serve_catalog.loader import Catalog, load_catalog
from jsonschema import Draft202012Validator, ValidationError

from fs2_serve.live_release import LiveReleaseError, render_live_release
from fs2_serve.qualification import (
    QualificationError,
    validate_qualification_projection,
)

INVENTORY = CONTROL_ROOT / "contracts/all-models-live-services.json"
PROJECTION_SCHEMA = CONTROL_ROOT / "contracts/model-qualification-projection.schema.json"
PROJECTION = CONTROL_ROOT / "contracts/model-qualification-projection.json"
TESTED_MODELS = {
    "boltz2",
    "diffdock",
    "evo2-40b",
    "genmol",
    "glm-5-2-fp8",
    "molmim",
    "msa-search-pdb70",
    "nv-reason-cxr-3b",
    "nv-segment-ct",
    "openfold2",
    "openfold3",
    "proteinmpnn",
    "qwen3-8b",
    "rfdiffusion",
    "sdxl",
}
EXPECTED_ACTIVE_VARIANTS = {
    "boltz2": "boltz2-hf-blackwell-sm103",
    "diffdock": "diffdock-upstream-blackwell-sm103",
    "evo2-40b": "evo2-40b-upstream-blackwell-sm103",
    "genmol": "nv-genmol-89m-v2-blackwell-sm103",
    "molmim": "molmim-exact-weights-blackwell-sm103",
    "nv-segment-ct": "nv-segment-ct-upstream-blackwell-sm103",
    "proteinmpnn": "proteinmpnn-upstream-blackwell-sm103",
    "rfdiffusion": "rfdiffusion-upstream-blackwell-sm103",
}


@pytest.fixture(scope="module")
def catalog() -> Catalog:
    return load_catalog(CATALOG_ROOT, repo_root=REPO_ROOT)


def inventory() -> dict[str, Any]:
    return json.loads(INVENTORY.read_text(encoding="utf-8"))


def write_inventory(path: Path, value: dict[str, Any]) -> None:
    path.write_text(json.dumps(value), encoding="utf-8")


def test_rendered_configmaps_and_helm_values_share_one_versioned_release(tmp_path: Path, catalog: Catalog) -> None:
    path = tmp_path / "inventory.json"
    write_inventory(path, inventory())

    release = render_live_release(catalog, path)

    assert len(release.routes) == 15
    assert release.bindings_config_map_name.endswith(release.release_id)
    assert release.routes_config_map_name.endswith(release.release_id)
    assert release.helm_values["catalog"] == {
        "bindingsConfigMapName": release.bindings_config_map_name,
        "rolloutDigest": f"sha256:{catalog.digest}",
        "leanRoutes": {
            "enabled": True,
            "configMapName": release.routes_config_map_name,
            "key": "lean-routes.json",
        },
    }
    bindings, routes = release.config_maps
    assert bindings["metadata"]["name"] == release.bindings_config_map_name
    assert routes["metadata"]["name"] == release.routes_config_map_name
    assert bindings["metadata"]["annotations"] == routes["metadata"]["annotations"]
    assert json.loads(bindings["data"]["serving-bindings.json"])["schema"] == SERVING_BINDINGS_SCHEMA
    route_document = json.loads(routes["data"]["lean-routes.json"])
    assert route_document["schema"] == "fs2-serve.nebius.ai/lean-routes/v3"
    assert {item["model_id"] for item in route_document["routes"]} == TESTED_MODELS
    assert route_document["qualification"] == release.qualification_projection


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (
            lambda value: value.update(schema="fs2-serve.nebius.ai/all-models-live-services/v2"),
            "schema is unsupported",
        ),
        (lambda value: value.pop("qualification"), "fields are invalid"),
    ],
)
def test_qualified_inventory_requires_v3_and_qualification(
    tmp_path: Path,
    catalog: Catalog,
    mutate: Callable[[dict[str, Any]], object],
    message: str,
) -> None:
    value = inventory()
    mutate(value)
    path = tmp_path / "inventory.json"
    write_inventory(path, value)

    with pytest.raises(LiveReleaseError, match=message):
        render_live_release(catalog, path)


def test_projection_is_exactly_15_schema_valid_and_excludes_literal_bf16(tmp_path: Path, catalog: Catalog) -> None:
    path = tmp_path / "inventory.json"
    write_inventory(path, inventory())
    release = render_live_release(catalog, path)
    schema = json.loads(PROJECTION_SCHEMA.read_text(encoding="utf-8"))

    Draft202012Validator.check_schema(schema)
    Draft202012Validator(schema, format_checker=Draft202012Validator.FORMAT_CHECKER).validate(
        release.qualification_projection
    )
    assert json.loads(PROJECTION.read_text(encoding="utf-8")) == release.qualification_projection
    rows = release.qualification_projection["rows"]
    assert [row["model_id"] for row in rows] == sorted(TESTED_MODELS)
    assert "glm-5-2-fp8" in TESTED_MODELS
    assert "glm-5-2" not in TESTED_MODELS
    assert set(catalog.tested_model_ids) == TESTED_MODELS


def test_projection_schema_and_runtime_reject_arbitrary_evidence_keys(tmp_path: Path, catalog: Catalog) -> None:
    path = tmp_path / "inventory.json"
    write_inventory(path, inventory())
    release = render_live_release(catalog, path)
    projection = copy.deepcopy(release.qualification_projection)
    projection["rows"][0]["evidence"] = {f"arbitrary_{index}": "0" * 64 for index in range(7)}
    schema = json.loads(PROJECTION_SCHEMA.read_text(encoding="utf-8"))

    with pytest.raises(ValidationError):
        Draft202012Validator(schema, format_checker=Draft202012Validator.FORMAT_CHECKER).validate(projection)
    with pytest.raises(QualificationError, match="qualification evidence fields are invalid"):
        validate_qualification_projection(catalog, release.routes, projection)


def test_projection_runtime_rejects_consistent_placeholder_evidence_across_all_rows(
    tmp_path: Path, catalog: Catalog
) -> None:
    path = tmp_path / "inventory.json"
    write_inventory(path, inventory())
    release = render_live_release(catalog, path)
    projection = copy.deepcopy(release.qualification_projection)
    placeholder_evidence = {key: "0" * 64 for key in projection["rows"][0]["evidence"]}
    for row in projection["rows"]:
        row["evidence"] = copy.deepcopy(placeholder_evidence)
    schema = json.loads(PROJECTION_SCHEMA.read_text(encoding="utf-8"))

    # The published shape validator intentionally checks digest syntax; runtime
    # applies the shared catalog strength rule and rejects placeholder content.
    Draft202012Validator(schema).validate(projection)
    with pytest.raises(QualificationError, match="strong SHA-256 digests"):
        validate_qualification_projection(catalog, release.routes, projection)


@pytest.mark.parametrize("field", ["registered", "route_active"])
def test_projection_schema_and_runtime_reject_inactive_projected_routes(
    tmp_path: Path, catalog: Catalog, field: str
) -> None:
    path = tmp_path / "inventory.json"
    write_inventory(path, inventory())
    release = render_live_release(catalog, path)
    projection = copy.deepcopy(release.qualification_projection)
    projection["rows"][0]["states"][field] = False
    schema = json.loads(PROJECTION_SCHEMA.read_text(encoding="utf-8"))

    with pytest.raises(ValidationError):
        Draft202012Validator(schema).validate(projection)
    with pytest.raises(QualificationError, match="must be registered and active"):
        validate_qualification_projection(catalog, release.routes, projection)


@pytest.mark.parametrize(
    "observed_at",
    ["2026-08-30T14:35:35.123Z", "2026-08-30T14:35:35+00:00"],
)
def test_projection_schema_and_runtime_require_exact_utc_second_observation_time(
    tmp_path: Path, catalog: Catalog, observed_at: str
) -> None:
    path = tmp_path / "inventory.json"
    write_inventory(path, inventory())
    release = render_live_release(catalog, path)
    projection = copy.deepcopy(release.qualification_projection)
    projection["observed_at"] = observed_at
    schema = json.loads(PROJECTION_SCHEMA.read_text(encoding="utf-8"))

    with pytest.raises(ValidationError):
        Draft202012Validator(schema).validate(projection)
    with pytest.raises(QualificationError, match="time is invalid"):
        validate_qualification_projection(catalog, release.routes, projection)


def test_projection_schema_and_runtime_reject_impossible_observation_time(tmp_path: Path, catalog: Catalog) -> None:
    path = tmp_path / "inventory.json"
    write_inventory(path, inventory())
    release = render_live_release(catalog, path)
    projection = copy.deepcopy(release.qualification_projection)
    projection["observed_at"] = "2026-99-99T99:99:99Z"
    schema = json.loads(PROJECTION_SCHEMA.read_text(encoding="utf-8"))

    with pytest.raises(ValidationError):
        Draft202012Validator(schema, format_checker=Draft202012Validator.FORMAT_CHECKER).validate(projection)
    with pytest.raises(QualificationError, match="time is invalid"):
        validate_qualification_projection(catalog, release.routes, projection)


def test_projection_names_eight_independent_variants_three_exact_nims_and_policy_restrictions(
    tmp_path: Path, catalog: Catalog
) -> None:
    path = tmp_path / "inventory.json"
    write_inventory(path, inventory())
    release = render_live_release(catalog, path)
    rows = {row["model_id"]: row for row in release.qualification_projection["rows"]}

    assert {
        model_id: row["variant_id"] for model_id, row in rows.items() if row["variant_id"] is not None
    } == EXPECTED_ACTIVE_VARIANTS
    assert {model_id for model_id, row in rows.items() if row["runtime_origin"]["kind"] == "nvidia-nim"} == {
        "msa-search-pdb70",
        "openfold2",
        "openfold3",
    }
    assert rows["nv-segment-ct"]["runtime_origin"]["repository"] == "nvidia/NV-Segment-CT"
    assert rows["nv-segment-ct"]["runtime_origin"]["nim_artifact_parity"] == "unverified"
    assert rows["nv-segment-ct"]["policy"]["non_clinical"] is True
    assert rows["nv-reason-cxr-3b"]["policy"] == {
        "license_id": "NVIDIA-OneWay-Noncommercial",
        "non_clinical": True,
        "commercial_use": "prohibited",
    }
    assert all(row["states"]["runtime_ready"] for row in rows.values())
    assert all(row["states"]["semantic_qualified"] for row in rows.values())
    assert all(row["states"]["http_mcp_qualified"] for row in rows.values())
    assert not any(row["states"]["elasticity_qualified"] for row in rows.values())


def test_release_selects_explicit_segment_sm103_variant(tmp_path: Path, catalog: Catalog) -> None:
    path = tmp_path / "inventory.json"
    write_inventory(path, inventory())

    release = render_live_release(catalog, path)

    segment = next(item for item in release.routes if item["model_id"] == "nv-segment-ct")
    assert segment["variant_id"] == "nv-segment-ct-upstream-blackwell-sm103"


def test_projection_accepts_an_exact_qualified_variant_on_another_accelerator_profile(
    tmp_path: Path, catalog: Catalog
) -> None:
    class AlternateAcceleratorCatalog:
        def __init__(self, delegate: Catalog) -> None:
            self.delegate = delegate

        def __getattr__(self, name: str) -> Any:
            return getattr(self.delegate, name)

        def model_variant(self, variant_id: str) -> Any:
            variant = self.delegate.model_variant(variant_id)
            if variant_id != "nv-segment-ct-upstream-blackwell-sm103":
                return variant
            value = variant.to_dict()
            value["runtime"]["architecture"] = "h200-sm90"
            value["runtime"]["device_capability"] = "sm90-qualified"
            return SimpleNamespace(
                base_model_id=variant.base_model_id,
                exposed_model_id=variant.exposed_model_id,
                relationship=variant.relationship,
                runtime_architecture="h200-sm90",
                to_dict=lambda: copy.deepcopy(value),
            )

        def fallback_for_variant(self, variant_id: str) -> tuple[Any, str]:
            fallback, profile = self.delegate.fallback_for_variant(variant_id)
            if variant_id == "nv-segment-ct-upstream-blackwell-sm103":
                profile = "h200-sm90"
            return fallback, profile

    path = tmp_path / "inventory.json"
    write_inventory(path, inventory())

    release = render_live_release(AlternateAcceleratorCatalog(catalog), path)  # type: ignore[arg-type]

    segment = next(row for row in release.qualification_projection["rows"] if row["model_id"] == "nv-segment-ct")
    assert segment["runtime_origin"]["kind"] == "independent-runtime"


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda value: value["routes"].pop("sdxl"), "tested catalog"),
        (lambda value: value["routes"]["qwen3-8b"].update(model_revision="0" * 40), "selected catalog identity"),
        (lambda value: value["routes"]["qwen3-8b"].update(runtime_image_digest="latest"), "digest-pinned"),
        (
            lambda value: value["routes"]["qwen3-8b"].update(runtime_image_digest="sha256:" + "0" * 64),
            "selected catalog identity",
        ),
        (lambda value: value["routes"]["sdxl"]["service"].update(name="unowned"), "unowned"),
        (lambda value: value["routes"]["sdxl"].update(protocols={"native": "/other"}), "protocols"),
        (lambda value: value["routes"]["sdxl"]["mcp"].update(tool_name="chat_completion"), "duplicate MCP"),
        (
            lambda value: value["routes"]["nv-segment-ct"].update(variant_id=None),
            "qualification projection is contradictory",
        ),
        (
            lambda value: value["qualification"].update(authority="static-variant-promotion"),
            "qualification projection is contradictory",
        ),
    ],
)
def test_release_rejects_live_catalog_tool_or_authority_drift(
    tmp_path: Path,
    catalog: Catalog,
    mutate: Callable[[dict[str, Any]], object],
    message: str,
) -> None:
    value = inventory()
    mutate(value)
    path = tmp_path / "inventory.json"
    write_inventory(path, value)

    with pytest.raises(LiveReleaseError, match=message):
        render_live_release(catalog, path)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda value: value["rows"].pop(),
        lambda value: value["rows"][-1].update(model_id="unexpected"),
        lambda value: value["rows"].__setitem__(-1, copy.deepcopy(value["rows"][0])),
        lambda value: value["rows"][0]["active_runtime"].update(model_revision="0" * 40),
        lambda value: value["rows"][0]["active_runtime"].update(runtime_image_digest="sha256:" + "0" * 64),
        lambda value: value["rows"][0]["active_runtime"]["service"].update(name="drifted"),
        lambda value: value["rows"][0]["runtime_origin"].update(repository="example.invalid/drift"),
        lambda value: value["rows"][0]["policy"].update(non_clinical=True),
        lambda value: value["rows"][0]["evidence"].update(audited_catalog_sha256="0" * 64),
        lambda value: value.update(activation_authority="static-variant-promotion"),
    ],
)
def test_projection_rejects_missing_extra_duplicate_runtime_policy_evidence_or_authority_drift(
    tmp_path: Path,
    catalog: Catalog,
    mutate: Callable[[dict[str, Any]], object],
) -> None:
    path = tmp_path / "inventory.json"
    write_inventory(path, inventory())
    release = render_live_release(catalog, path)
    projection = copy.deepcopy(release.qualification_projection)
    mutate(projection)

    with pytest.raises(QualificationError):
        validate_qualification_projection(catalog, release.routes, projection)


def test_release_rejects_duplicate_json_keys(tmp_path: Path, catalog: Catalog) -> None:
    path = tmp_path / "inventory.json"
    path.write_text(
        '{"schema":"x","schema":"y","namespace":"fs2-models","qualification":{},"routes":{}}',
        encoding="utf-8",
    )

    with pytest.raises(LiveReleaseError, match="duplicate key"):
        render_live_release(catalog, path)
