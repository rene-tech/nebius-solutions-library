from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from fs2_serve.registry import Registry
from fs2_serve.scientific_admin import ScientificAdminSourceUnavailableError
from fs2_serve.scientific_admin_catalog import ScientificCatalogFileAdapter, ScientificProfileDiscoveryAdapter
from fs2_serve.scientific_batch.service import ScientificProfileDiscovery

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


class DiscoveryService:
    def discovery_profiles(self, *, tenant_id, allowed_models, surface):
        assert allowed_models == frozenset({"*"})
        assert surface == "admin"
        if tenant_id != "tenant-a":
            return ()
        return (
            ScientificProfileDiscovery(
                model_id="protein-design",
                display_name="Protein Design",
                execution_mode="scientific-batch",
                operations=("design",),
                service_classes=("customer-batch",),
                parameter_schema="fs2-serve.nebius.ai/protein-design-parameters/v1",
                source_repository="example/protein-design",
                source_revision="a" * 40,
                variant_id="protein-design-h100",
                runtime_image_digest="sha256:" + "b" * 64,
                execution_identity_sha256="c" * 64,
                access_profile="standard",
                access_state="not-required",
                access_receipt_digest=None,
                h100_semantic_receipt_sha256="d" * 64,
                public_completion_receipt_sha256="e" * 64,
                scheduler_eligibility_receipt_sha256="f" * 64,
                execution_map_sha256="1" * 64,
                qualified_at="2026-09-03T08:00:00Z",
                mcp_tool_name="submit_protein_design",
                mcp_description="Submit a qualified protein design run.",
            ),
        )


async def test_admin_discovery_lists_only_tenant_submittable_profiles() -> None:
    adapter = ScientificProfileDiscoveryAdapter(
        scientific_batches=DiscoveryService(),  # type: ignore[arg-type]
        clock=lambda: FIXED_NOW,
    )

    assert (await adapter.list_models()).data.items == []
    snapshot = await adapter.list_models(tenant_id="tenant-a")

    assert snapshot.observed_at == FIXED_NOW
    assert [item.model_id for item in snapshot.data.items] == ["protein-design"]
    model = snapshot.data.items[0]
    assert model.readiness == "qualified"
    assert model.backend.runtime_image_digest == "sha256:" + "b" * 64
    assert model.backend.execution_identity_digest == "c" * 64
