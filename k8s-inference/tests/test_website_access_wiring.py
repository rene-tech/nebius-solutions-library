from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BOOTSTRAP = (ROOT / "stages/workloads/bootstrap_access.tf").read_text(encoding="utf-8")
CONTROL_PLANE = (ROOT / "stages/workloads/control_plane.tf").read_text(encoding="utf-8")


def test_website_access_is_a_distinct_catalog_only_terraform_identity() -> None:
    assert 'website_access_secret_name = "fs2-serve-website-access"' in BOOTSTRAP
    assert 'website_access_principal   = "terraform-scientific-ai-website"' in BOOTSTRAP
    assert 'website_access_scopes      = ["catalog.read"]' in BOOTSTRAP
    assert "maxConcurrency = 1" in BOOTSTRAP
    assert 'resource "random_id" "website_access_token_id"' in BOOTSTRAP
    assert 'resource "random_password" "website_access_token_secret"' in BOOTSTRAP
    assert 'resource "kubernetes_secret_v1" "website_access"' in BOOTSTRAP
    assert (
        '"fs2.nebius.ai/credential-purpose" = "scientific-ai-website-catalog"'
        in BOOTSTRAP
    )


def test_control_plane_waits_for_the_website_secret_and_bootstraps_its_policy() -> None:
    assert "yamlencode(local.website_access_overrides)" in CONTROL_PLANE
    assert "kubernetes_secret_v1.website_access" in CONTROL_PLANE
