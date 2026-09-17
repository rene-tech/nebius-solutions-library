from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_catalog_cryptography_and_provider_versions_are_exactly_pinned() -> None:
    catalog_project = (ROOT / "catalog/runtime/pyproject.toml").read_text()
    catalog_lock = (ROOT / "catalog/runtime/uv.lock").read_text()
    assert 'dependencies = ["cryptography==50.0.1"]' in catalog_project
    assert 'name = "cryptography"\nversion = "50.0.1"' in catalog_lock
    assert 'version = "41.0.7"' not in catalog_lock

    for relative in (
        "stages/infrastructure/versions.tf",
        "stages/workloads/versions.tf",
        "reference-data/terraform/versions.tf",
    ):
        versions = (ROOT / relative).read_text()
        assert 'version = "= 0.5.232"' in versions
        assert 'version = ">= 0.5.232"' not in versions


def test_runtime_dockerfiles_repair_the_reported_os_packages() -> None:
    control = (ROOT / "components/control-plane/Dockerfile").read_text()
    assert "'libuuid=2.41.6-r0'" in control

    admin = (ROOT / "components/admin-console/Dockerfile").read_text()
    runtime = admin.split("FROM nginxinc/nginx-unprivileged:", maxsplit=1)[1]
    assert "apk add --no-cache --upgrade" in runtime
    for package in ("libcrypto3", "libssl3", "libexpat", "libuuid"):
        assert package in runtime
    assert "RUN apk upgrade" not in runtime
    for argument in (
        "FS2_LIBCRYPTO3_APK",
        "FS2_LIBSSL3_APK",
        "FS2_LIBEXPAT_APK",
        "FS2_LIBUUID_APK",
    ):
        assert argument in runtime
    package_lock = json.loads(
        (ROOT / "security/alpine-runtime-packages.lock.json").read_text()
    )
    assert package_lock["state"] == "blocked_pending_authorized_package_resolution"


def test_image_security_policy_is_fail_closed_and_time_bounded() -> None:
    policy = json.loads((ROOT / "security/image-security-policy.json").read_text())
    assert policy["schedule"]["minimum_frequency"] == "daily"
    assert policy["release_gate"]["block_fixable_severities"] == [
        "CRITICAL",
        "HIGH",
    ]
    assert policy["release_gate"]["maximum_secret_findings"] == 0
    assert policy["release_gate"]["require_digest_bound_result"] is True
    assert policy["remediation_sla"] == {
        "critical_hours": 24,
        "high_hours": 168,
        "clock_start": "scanner_result_created_at",
    }
    assert policy["evidence"]["retention_days"] == 90
    assert "spdx-json" in policy["evidence"]["required_formats"]

    observability_lock = (ROOT / "observability/versions.lock.yaml").read_text()
    assert "deployment: official-chart-tags" in observability_lock
    assert "promotion: blocked-pending-image-digests" in observability_lock
    assert "digestResolution: required-before-integration" in observability_lock
    installer = (ROOT / "observability/scripts/install.sh").read_text()
    assert ".imagePolicy.deployment" not in installer
    assert "observability deployment blocked" not in installer

    workflow = (
        ROOT.parent / ".github/workflows/k8s-inference-image-security.yml"
    ).read_text()
    assert "cron: '17 3 * * *'" in workflow
    assert "aquasecurity/trivy-action@" not in workflow
    assert "trivy_0.70.0_Linux-64bit.tar.gz" in workflow
    assert "8b4376d5d6befe5c24d503f10ff136d9e0c49f9127a4279fd110b727929a5aa9" in workflow
    assert "catalog/runtime" in workflow
    assert "third-party-images.lock.json" in workflow
    assert (
        "--package libcrypto3 --package libssl3 --package libexpat --package libuuid"
        in workflow
    )
    assert "retention-days: 90" in workflow
    assert "--provenance=mode=max" in workflow
    assert "release_image_closure.py" in workflow
    assert "SAI24_EVIDENCE_SIGNING_KEY_PEM" in workflow

    normal_workflow = (ROOT.parent / ".github/workflows/k8s-inference.yml").read_text()
    assert "uses: ./.github/workflows/k8s-inference-image-security.yml" in normal_workflow
    assert "needs: image-security" in normal_workflow
    assert "promotion-gate:" in normal_workflow

    inventory = json.loads((ROOT / "security/third-party-images.lock.json").read_text())
    assert "rendered_inventory_complete" not in inventory
    assert "exact Helm post-render output" in inventory["completeness_authority"]
    assert inventory["inventory_state"] == "blocked_pending_digest_resolution"
    by_id = {image["id"]: image for image in inventory["images"]}
    assert by_id["dcgm-exporter"]["digest_reference"] == (
        "nvcr.io/nvidia/k8s/dcgm-exporter@sha256:"
        "b4df763de9558e5b3f1f1d79bc65b772fcf65b8a9c3664ea7173e47153112b4a"
    )
    for image_id in (
        "cloudnative-pg-operator",
        "envoy-gateway-controller",
        "loki",
        "tempo",
        "otel-collector-contrib",
        "otel-collector-k8s",
    ):
        assert by_id[image_id]["digest_reference"] is None
