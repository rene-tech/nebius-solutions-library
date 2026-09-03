from __future__ import annotations

import json
import pytest
from pathlib import Path

from conftest import CATALOG_ROOT

from fs2_serve.scientific_batch.canary import CANARY_ID, run_internal_cpu_canary
from fs2_serve.scientific_batch.execution import (
    FileScientificManifestRenderer,
    ScientificExecutionMapError,
)
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
    # BoltzGen is dispatchable, so exactly one profile is runnable. The canary itself is
    # still never one of them, which the discoverability assertions below prove.
    assert first.runnable_profile_count == 1
    assert CANARY_ID not in {profile.model_id for profile in catalog.list(runnable_only=False)}


def test_empty_public_execution_map_is_refused_once_a_profile_is_runnable(tmp_path: Path) -> None:
    """An empty map may not be published while a profile claims to be servable.

    It was valid while nothing was runnable. Now that BoltzGen is dispatchable the map must
    cover every runnable stage, so an empty one is a configuration error rather than a
    harmless default: it would leave a routed profile with nothing to execute.
    """
    catalog = ScientificProfileCatalog.load(CATALOG_ROOT)
    receipt = run_internal_cpu_canary(catalog)
    assert receipt.runnable_profile_count == 1

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
    assert set(renderer.variants) == {"boltzgen"}
    assert receipt.canary_id not in renderer.variants
