from __future__ import annotations

import json
from pathlib import Path

import pytest

from security.image_security_evidence import (
    EvidenceError,
    create_package_inventory,
    enforce_report,
    validate_inventory,
    validate_receipt,
)


ROOT = Path(__file__).resolve().parents[1]
DIGEST = "sha256:" + "a" * 64


def test_checked_in_third_party_inventory_stays_fail_closed_until_resolved() -> None:
    with pytest.raises(EvidenceError, match="rendered inventory is not complete"):
        validate_inventory(ROOT / "security/third-party-images.lock.json")


def test_inventory_rejects_tag_only_entries_after_completion(tmp_path: Path) -> None:
    inventory = {
        "schema": "fs2-serve.nebius.ai/third-party-image-lock/v1",
        "inventory_state": "digest-pinned",
        "rendered_inventory_complete": True,
        "images": [
            {
                "id": "example",
                "source_reference": "registry.example/image:1.0",
                "digest_reference": None,
                "consumers": ["values.yaml"],
            }
        ],
    }
    path = tmp_path / "inventory.json"
    path.write_text(json.dumps(inventory))
    with pytest.raises(EvidenceError, match="not bound to an exact sha256 digest"):
        validate_inventory(path)


def test_report_rejects_only_fixable_high_or_critical_and_all_secrets(
    tmp_path: Path,
) -> None:
    report = {
        "Results": [
            {
                "Vulnerabilities": [
                    {"Severity": "HIGH", "FixedVersion": "2.0"},
                    {"Severity": "CRITICAL", "FixedVersion": ""},
                ],
                "Secrets": [],
            }
        ]
    }
    path = tmp_path / "report.json"
    path.write_text(json.dumps(report))
    with pytest.raises(EvidenceError, match="1 fixable HIGH/CRITICAL"):
        enforce_report(path)

    report["Results"][0]["Vulnerabilities"] = []
    report["Results"][0]["Secrets"] = [{"RuleID": "example"}]
    path.write_text(json.dumps(report))
    with pytest.raises(EvidenceError, match="1 secret findings"):
        enforce_report(path)


def test_receipt_rejects_tag_subject_even_when_gate_claims_pass(tmp_path: Path) -> None:
    receipt = {
        "schema": "fs2-serve.nebius.ai/image-scan-receipt/v1",
        "source": {"commit": "b" * 40, "tree": "c" * 40},
        "subject": {"kind": "image", "identity": "registry.example/image:1.0"},
        "scanner": {"archive_sha256": "d" * 64},
        "artifacts": {"report": {"sha256": "e" * 64}},
        "gate": {"passed": True},
    }
    path = tmp_path / "receipt.json"
    path.write_text(json.dumps(receipt))
    with pytest.raises(EvidenceError, match="not digest-bound"):
        validate_receipt(path)


def test_package_inventory_requires_exact_versions_for_every_named_package(
    tmp_path: Path,
) -> None:
    sbom = tmp_path / "sbom.json"
    sbom.write_text(
        json.dumps({"packages": [{"name": "libssl3", "versionInfo": "3.5.8-r0"}]})
    )
    output = tmp_path / "packages.json"
    with pytest.raises(EvidenceError, match="libcrypto3"):
        create_package_inventory(sbom, ["libssl3", "libcrypto3"], output)
