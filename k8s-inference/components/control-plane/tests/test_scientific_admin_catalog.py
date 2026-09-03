from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from fs2_serve.registry import Registry
from fs2_serve.scientific_admin import ScientificAdminSourceUnavailableError
from fs2_serve.scientific_admin_catalog import ScientificCatalogFileAdapter

FIXED_NOW = datetime(2026, 9, 2, 21, 0, tzinfo=UTC)
RECEIPTS = Path(__file__).resolve().parents[3] / "catalog/runtime/contracts/scientific-source-candidate-receipts.json"


async def test_candidate_catalog_is_truthful_about_access_identity_and_cache_state(registry: Registry) -> None:
    snapshot = await ScientificCatalogFileAdapter(
        registry=registry,
        receipts_file=RECEIPTS,
        clock=lambda: FIXED_NOW,
    ).list_models()

    assert snapshot.observed_at == FIXED_NOW
    assert len(snapshot.data.items) == 9
    by_id = {item.model_id: item for item in snapshot.data.items}
    alpha_fold = by_id["alphafold3"]
    assert alpha_fold.readiness == "blocked"
    assert alpha_fold.access.profile == "academic"
    assert alpha_fold.access.state == "unverified"
    assert alpha_fold.access.credentials_exposed is False
    assert alpha_fold.access.alternative is not None
    assert alpha_fold.access.alternative.model_id == "openfold3"
    assert alpha_fold.backend.source_revision == "c0f97eda2f1f482fd94d3a38bece18c7069b4a5c"
    assert alpha_fold.backend.runtime_image_digest is None
    assert alpha_fold.caching.exact_tier == "not-observed"
    assert alpha_fold.caching.gpu_snapshot == "unavailable"

    rfdiffusion = by_id["rfdiffusion"]
    assert rfdiffusion.backend.backend_id == "rfdiffusion-upstream:native-upstream"
    assert "rfdiffusion-upstream" not in by_id

    esm_fast = by_id["esmfold2-fast"]
    assert esm_fast.execution_mode == "hybrid"
    assert esm_fast.interactive_supported is True
    assert [value.value for value in esm_fast.service_classes] == [
        "presentation",
        "interactive",
        "customer-batch",
        "bulk-backfill",
    ]


async def test_catalog_adapter_fails_closed_for_unknown_or_oversized_receipts(
    registry: Registry,
    tmp_path: Path,
) -> None:
    malformed = tmp_path / "receipts.json"
    malformed.write_text(json.dumps({"schema": "unknown", "receipts": []}), encoding="utf-8")
    adapter = ScientificCatalogFileAdapter(registry=registry, receipts_file=malformed)
    with pytest.raises(ScientificAdminSourceUnavailableError, match="schema is unsupported"):
        await adapter.list_models()

    malformed.write_text("x" * (1024 * 1024 + 1), encoding="utf-8")
    with pytest.raises(ScientificAdminSourceUnavailableError, match="too large"):
        await adapter.list_models()
