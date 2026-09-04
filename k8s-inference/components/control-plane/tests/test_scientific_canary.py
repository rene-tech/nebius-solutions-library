from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from conftest import CATALOG_ROOT

from fs2_serve.scientific_batch.canary import CANARY_ID, run_internal_cpu_canary
from fs2_serve.scientific_batch.execution import (
    FileScientificManifestRenderer,
    ScientificExecutionMapError,
)
from fs2_serve.scientific_batch.profile_catalog import ScientificProfileCatalog, ScientificProfileError


def test_internal_cpu_canary_is_deterministic_and_never_a_discoverable_profile() -> None:
    catalog = ScientificProfileCatalog.load(CATALOG_ROOT)

    first = run_internal_cpu_canary(catalog)
    second = run_internal_cpu_canary(catalog)

    assert first == second
    assert first.canary_id == CANARY_ID
    assert first.input_sha256 == "1e20635aeb8036a584a2b6f69da8c707b12f1f44ed452e78a472c3e0f064928e"
    assert first.output_sha256 == "b18338dda9fda75dbca256a7982778de61d6f0a1317ae1b0d0c2adb98ca68457"
    assert first.profile_count == 10
    # All ten requested models are dispatchable through the active bridge. The canary
    # itself is still never one of them, which the discoverability assertions prove.
    assert first.runnable_profile_count == 10
    assert CANARY_ID not in {profile.model_id for profile in catalog.list(runnable_only=False)}


def test_empty_public_execution_map_is_refused_once_a_profile_is_runnable(tmp_path: Path) -> None:
    """An empty map may not be published while a profile claims to be servable.

    It was valid while nothing was runnable. Now that the ten-model fleet is dispatchable
    the map must cover every runnable stage, so an empty one is a configuration error.
    """
    catalog = ScientificProfileCatalog.load(CATALOG_ROOT)
    receipt = run_internal_cpu_canary(catalog)
    assert receipt.runnable_profile_count == 10

    execution_map = tmp_path / "execution-map.json"
    execution_map.write_text(
        json.dumps(
            {"schema": "fs2-serve.nebius.ai/scientific-execution-map/v3", "models": []},
            sort_keys=True,
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )

    with pytest.raises(ScientificExecutionMapError):
        FileScientificManifestRenderer(path=execution_map, profiles=catalog)

    # The committed map does cover it, and the canary is still not a discoverable profile.
    renderer = FileScientificManifestRenderer(
        path=CATALOG_ROOT / "contracts/scientific-execution-map.json", profiles=catalog
    )
    assert set(renderer.variants) == {
        "alphafold3",
        "bindcraft",
        "boltzgen",
        "esmfold2",
        "esmfold2-fast",
        "mosaic",
        "openfold3-openbind",
        "proteina-complexa",
        "protenix-v2",
        "rfdiffusion",
    }
    assert receipt.canary_id not in renderer.variants


def test_profile_catalog_refuses_a_stale_execution_identity(tmp_path: Path) -> None:
    catalog = tmp_path / "catalog"
    shutil.copytree(CATALOG_ROOT, catalog)
    profile_path = catalog / "contracts/scientific-workload-profiles.json"
    document = json.loads(profile_path.read_text())
    profile = next(item for item in document["profiles"] if item["model_id"] == "boltzgen")
    profile["execution_identity"]["workload_recipe_sha256"] = "a" * 64
    profile_path.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(ScientificProfileError, match="execution identity is stale"):
        ScientificProfileCatalog.load(catalog)


def test_profile_catalog_refuses_a_workload_only_mutation(tmp_path: Path) -> None:
    catalog = tmp_path / "catalog"
    shutil.copytree(CATALOG_ROOT, catalog)
    profile_path = catalog / "contracts/scientific-workload-profiles.json"
    document = json.loads(profile_path.read_text())
    profile = next(item for item in document["profiles"] if item["model_id"] == "boltzgen")
    profile["workload"]["stages"][0]["max_parallelism"] = 31
    profile_path.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(ScientificProfileError, match="workload recipe digest is stale"):
        ScientificProfileCatalog.load(catalog)
