from __future__ import annotations

import json
from pathlib import Path

from test_admin_read import _client, _runtime
from test_scientific_admin import _readiness

from fs2_serve.model_inventory import build_model_inventory


def test_inventory_keeps_unconfigured_models(registry, cipher, hasher):
    with _client(_runtime(registry, cipher, hasher)) as client:
        response = client.get("/admin/api/v1/model-inventory")
        assert response.status_code == 200
        inventory = response.json()["data"]
        items = {item["model_id"]: item for item in inventory["items"]}
        assert set(items) == {model.id for model in registry.list()}
        assert items["qwen3-8b"]["availability"] == "hot"
        assert items["genmol"]["availability"] == "not-deployed"
        assert items["genmol"]["configured"] is False
        assert items["genmol"]["runtime_image_digest"] is None
        assert items["genmol"]["gpu_snapshot"] == "not-reported"
        assert inventory["not_deployed"] > 0


def test_inventory_requires_admin_session(registry, cipher, hasher):
    with _client(_runtime(registry, cipher, hasher), authenticated=False) as client:
        assert client.get("/admin/api/v1/model-inventory").status_code == 401


def test_failed_scientific_projection_does_not_claim_missing_model(registry):
    result = build_model_inventory(registry.list(), [], [], scientific_projection_available=False)
    assert result.not_deployed == 0
    assert all(item.availability == "unknown" for item in result.items)


def test_batch_profile_merges_legacy_id_without_claiming_hot_gpu(registry):
    profile = _readiness("rfdiffusion")
    profile = profile.model_copy(update={"readiness": "qualified"})
    result = build_model_inventory(registry.list(), [], [profile], scientific_projection_available=True)
    rows = [item for item in result.items if item.model_id == "rfdiffusion"]
    assert len(rows) == 1
    assert rows[0].availability == "batch-ready"
    assert rows[0].configured is True
    assert rows[0].serving_enabled is False
    assert rows[0].ready_replicas is None
    assert rows[0].gpu_snapshot == "unavailable"


def test_scientific_only_profile_is_not_lost(registry):
    result = build_model_inventory(registry.list(), [], [_readiness("boltzgen")], scientific_projection_available=True)
    row = next(item for item in result.items if item.model_id == "boltzgen")
    assert row.availability == "candidate"
    assert row.configured is True
    assert row.management_path == "/admin/scientific-runs"


def test_complete_h100_expectations_do_not_shrink_to_current_deployments():
    solution = Path(__file__).resolve().parents[3]
    expected = json.loads((solution / "acceptance/h100-fleet/expected-models.json").read_text())
    catalog = json.loads((solution / "catalog/runtime/catalog.json").read_text())
    serving = set(expected["serving_model_ids"])
    scientific = set(expected["scientific_model_ids"])
    assert not serving.intersection(scientific)
    assert len(serving | scientific) == 24
    assert set(catalog["tested_model_ids"]) <= serving | scientific | set(expected["excluded_models"])
    assert set(expected["excluded_models"]) == {"glm-5-2-fp8"}
