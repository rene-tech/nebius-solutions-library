from __future__ import annotations

import json
from pathlib import Path

from conftest import CATALOG_ROOT

from fs2_serve.scientific_batch.canary import CANARY_ID, run_internal_cpu_canary
from fs2_serve.scientific_batch.execution import FileScientificManifestRenderer
from fs2_serve.scientific_batch.profile_catalog import ScientificProfileCatalog


def test_internal_cpu_canary_is_deterministic_and_never_a_discoverable_profile() -> None:
    catalog = ScientificProfileCatalog.load(CATALOG_ROOT)

    first = run_internal_cpu_canary(catalog)
    second = run_internal_cpu_canary(catalog)

    assert first == second
    assert first.canary_id == CANARY_ID
    assert first.input_sha256 == "1e20635aeb8036a584a2b6f69da8c707b12f1f44ed452e78a472c3e0f064928e"
    assert first.output_sha256 == "b18338dda9fda75dbca256a7982778de61d6f0a1317ae1b0d0c2adb98ca68457"
    assert first.profile_count == 2
    assert first.runnable_profile_count == 0
    assert CANARY_ID not in {profile.model_id for profile in catalog.list(runnable_only=False)}


def test_empty_public_execution_map_is_valid_after_private_canary_passes(tmp_path: Path) -> None:
    catalog = ScientificProfileCatalog.load(CATALOG_ROOT)
    receipt = run_internal_cpu_canary(catalog)
    execution_map = tmp_path / "execution-map.json"
    execution_map.write_text(
        json.dumps(
            {"schema": "fs2-serve.nebius.ai/scientific-execution-map/v3", "models": []},
            sort_keys=True,
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )

    renderer = FileScientificManifestRenderer(path=execution_map, profiles=catalog)

    assert receipt.runnable_profile_count == 0
    assert renderer.variants == {}
