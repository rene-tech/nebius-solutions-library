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

    observability_lock = (ROOT / "observability/versions.lock.yaml").read_text()
    assert "deployment: blocked-pending-image-digests" in observability_lock
    assert "digestResolution: required-before-integration" in observability_lock
    installer = (ROOT / "observability/scripts/install.sh").read_text()
    assert ".imagePolicy.deployment" in installer
    assert "digest-pinned" in installer

    workflow = (
        ROOT.parent / ".github/workflows/k8s-inference-image-security.yml"
    ).read_text()
    assert "cron: '17 3 * * *'" in workflow
    assert workflow.count("ignore-unfixed: true") == 2
    assert workflow.count("severity: 'CRITICAL,HIGH'") == 2
    assert workflow.count("scanners: 'vuln,secret'") == 2
