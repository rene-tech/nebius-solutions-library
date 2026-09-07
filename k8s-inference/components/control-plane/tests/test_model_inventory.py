from __future__ import annotations

import json
from pathlib import Path

from test_admin_read import _client, _runtime
from test_scientific_admin import _readiness

from fs2_serve.model_inventory import build_model_inventory, load_snapshot_capabilities


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


def _snapshot_inputs():
    solution = Path(__file__).resolve().parents[3]
    directory = solution / "acceptance/h100-fleet/snapshots"
    capabilities = load_snapshot_capabilities(directory / "capabilities.json")
    bundle = json.loads((directory / "protenix-v2-bundle.json").read_bytes())
    profile = _readiness("protenix-v2")
    profile = profile.model_copy(
        update={
            "readiness": "qualified",
            "backend": profile.backend.model_copy(
                update={
                    "runtime_image_digest": bundle["runtime_image"].split("@")[-1],
                    "model_revision": bundle["profile_model_revision"],
                }
            ),
        }
    )
    return capabilities, bundle, profile


def test_isolated_snapshot_proof_is_not_a_configured_option(registry):
    capabilities, _, profile = _snapshot_inputs()
    result = build_model_inventory(
        registry.list(), [], [profile], scientific_projection_available=True, snapshot_capabilities=capabilities
    )
    item = next(item for item in result.items if item.model_id == "protenix-v2")
    assert item.gpu_snapshot == "candidate"
    assert item.snapshot_evidence_scope == "isolated-qualified"
    assert not item.snapshot_selectable
    assert item.snapshot_bundle_ids == []
    assert item.snapshot_restore_startup.n == 3
    assert item.snapshot_restore_startup.median_seconds == 3.757077
    assert "container-start" in item.snapshot_restore_startup.clock
    assert "not reserved RAM" in item.snapshot_restore_startup.cache


def test_matching_configured_snapshot_becomes_selectable(registry):
    capabilities, bundle, profile = _snapshot_inputs()
    result = build_model_inventory(
        registry.list(),
        [],
        [profile],
        scientific_projection_available=True,
        snapshot_capabilities=capabilities,
        snapshot_bundles={bundle["bundle_id"]: bundle},
    )
    item = next(item for item in result.items if item.model_id == "protenix-v2")
    assert item.gpu_snapshot == "verified"
    assert item.snapshot_evidence_scope == "configured-qualified"
    assert item.snapshot_selectable
    assert item.snapshot_bundle_ids == [bundle["bundle_id"]]
    assert "does not mean the current run used a snapshot" in item.snapshot_reason


def test_wrong_receipt_or_profile_revision_never_promotes_snapshot(registry):
    capabilities, bundle, profile = _snapshot_inputs()
    for field in ("qualification_receipt_sha256", "manifest_sha256", "profile_model_revision", "runtime_image"):
        changed = {**bundle, field: "different"}
        result = build_model_inventory(
            registry.list(),
            [],
            [profile],
            scientific_projection_available=True,
            snapshot_capabilities=capabilities,
            snapshot_bundles={bundle["bundle_id"]: changed},
        )
        item = next(item for item in result.items if item.model_id == "protenix-v2")
        assert item.gpu_snapshot == "candidate"
        assert not item.snapshot_selectable


def test_snapshot_timings_are_not_projected_onto_a_different_runtime(registry):
    capabilities, bundle, profile = _snapshot_inputs()
    profile = profile.model_copy(
        update={
            "backend": profile.backend.model_copy(
                update={
                    "runtime_image_digest": "sha256:" + "f" * 64,
                }
            )
        }
    )
    result = build_model_inventory(
        registry.list(),
        [],
        [profile],
        scientific_projection_available=True,
        snapshot_capabilities=capabilities,
        snapshot_bundles={bundle["bundle_id"]: bundle},
    )
    item = next(item for item in result.items if item.model_id == "protenix-v2")
    assert item.snapshot_evidence_scope == "runtime-mismatch"
    assert not item.snapshot_selectable
    assert item.snapshot_restore_startup is None
    assert item.snapshot_normal_startup is None


def test_experimental_serving_and_cpu_only_models_have_explicit_truth(registry):
    capabilities, _, _ = _snapshot_inputs()
    result = build_model_inventory(
        registry.list(),
        [],
        [_readiness("mosaic")],
        scientific_projection_available=True,
        snapshot_capabilities=capabilities,
    )
    items = {item.model_id: item for item in result.items}
    assert items["qwen3-8b"].gpu_snapshot == "candidate"
    assert not items["qwen3-8b"].snapshot_selectable
    assert items["cosmos3-nano"].gpu_snapshot == "candidate"
    assert items["mosaic"].snapshot_evidence_scope == "tested-incompatible"
    assert items["mosaic"].gpu_snapshot == "unsupported"
    assert items["msa-search-pdb70"].snapshot_evidence_scope == "not-applicable"
    assert "CPU-only" in items["msa-search-pdb70"].snapshot_reason


def test_unavailable_profile_projection_cannot_advertise_selectable_snapshot(registry):
    capabilities, bundle, profile = _snapshot_inputs()
    result = build_model_inventory(
        registry.list(),
        [],
        [profile],
        scientific_projection_available=False,
        snapshot_capabilities=capabilities,
        snapshot_bundles={bundle["bundle_id"]: bundle},
    )
    assert not next(item for item in result.items if item.model_id == "protenix-v2").snapshot_selectable


def test_absent_optional_capability_file_preserves_legacy_projection(tmp_path):
    assert load_snapshot_capabilities(tmp_path / "missing.json") == {}
