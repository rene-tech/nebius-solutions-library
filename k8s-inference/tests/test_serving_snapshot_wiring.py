"""Qualified serving bundles preserve their measured source and storage seam."""

import hashlib
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("model", ["qwen3-8b", "cosmos3-nano", "genmol"])
def test_serving_snapshot_sources_are_the_exact_qualified_bytes(model):
    bundle = json.loads((ROOT / f"acceptance/h100-fleet/snapshots/{model}-bundle.json").read_text())
    sources = ROOT / "models/scientific-snapshot"
    for name, digest in bundle["source_sha256"].items():
        assert hashlib.sha256((sources / name).read_bytes()).hexdigest() == digest
    assert hashlib.sha256((sources / "serving_entrypoint.py").read_bytes()).hexdigest() == bundle["entrypoint_sha256"]


def test_snapshot_registry_and_dependencies_reach_the_existing_controller():
    controller = (ROOT / "stages/workloads/model_controller.tf").read_text()
    resources = (ROOT / "stages/workloads/serving_snapshots.tf").read_text()
    helm = (ROOT / "stages/workloads/control_plane.tf").read_text()
    assert "gpuSnapshotBundles = {" in controller
    assert "if bundle.model_ref == model_id" in controller
    assert "local.serving_snapshot_cache.manage_claim" in resources
    assert 'data "kubernetes_persistent_volume_claim_v1" "serving_snapshot_cache"' in resources
    assert "immutable = true" in resources
    assert "kubernetes_config_map_v1.serving_snapshot_sources" in helm
    assert "kubernetes_persistent_volume_claim_v1.serving_snapshots" in helm
    for hardcoded_gpu in ("h100", "b300", "sm90"):
        assert hardcoded_gpu not in resources.lower()
