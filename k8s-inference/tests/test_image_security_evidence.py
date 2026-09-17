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
from security.helm_image_postrenderer import rewrite
from security.release_image_closure import (
    _production_reference,
    _release_declaration_hashes,
    validate_source_surfaces,
)


ROOT = Path(__file__).resolve().parents[1]
DIGEST = "sha256:" + "a" * 64


def test_checked_in_third_party_inventory_stays_fail_closed_until_resolved() -> None:
    with pytest.raises(EvidenceError, match="not bound to an exact sha256 digest"):
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


def test_inventory_requires_manifest_bytes_to_match_the_oci_digest(
    tmp_path: Path,
) -> None:
    digest_reference = f"registry.example/image@{DIGEST}"
    inventory = {
        "schema": "fs2-serve.nebius.ai/third-party-image-lock/v1",
        "images": [
            {
                "id": "example",
                "source_reference": "registry.example/image:1.0",
                "digest_reference": digest_reference,
                "consumers": ["unit"],
                "resolution_provenance": {
                    "source_reference": "registry.example/image:1.0",
                    "digest_reference": digest_reference,
                    "manifest_media_type": "application/vnd.oci.image.manifest.v1+json",
                    "resolved_at": "2026-09-17T00:00:00Z",
                    "resolver_identity": "unit",
                    "manifest_sha256": "b" * 64,
                    "resolution_receipt_path": "resolution.json",
                    "resolution_receipt_sha256": "c" * 64,
                },
            }
        ],
    }
    path = tmp_path / "inventory.json"
    path.write_text(json.dumps(inventory))
    with pytest.raises(EvidenceError, match="manifest hash differs from OCI digest"):
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
        "schema": "fs2-serve.nebius.ai/image-scan-receipt/v2",
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


def test_postrenderer_accepts_reviewed_first_party_and_rejects_unlisted_digest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    digest_reference = f"registry.example/image@{DIGEST}"
    import security.helm_image_postrenderer as postrenderer

    monkeypatch.setattr(
        postrenderer,
        "validate_inventory",
        lambda _path, _trust: [
            {
                "source_reference": "registry.example/image:1.0",
                "digest_reference": digest_reference,
            }
        ],
    )
    first_party_reference = f"registry.example/first-party@{'sha256:' + 'b' * 64}"
    monkeypatch.setattr(
        postrenderer,
        "validate_first_party_inventory",
        lambda _path, _trust: [{"digest_reference": first_party_reference}],
    )
    rendered, subjects = rewrite(
        "containers:\n"
        "  - image: registry.example/image:1.0\n"
        f"  - image: {first_party_reference}\n",
        tmp_path / "third-party.json",
        tmp_path / "first-party.json",
        tmp_path / "trust.json",
    )
    assert digest_reference in rendered
    assert subjects == {digest_reference, first_party_reference}
    with pytest.raises(EvidenceError, match="absent from the reviewed scan inventory"):
        rewrite(
            f"containers:\n  - image: registry.example/other@{DIGEST}\n",
            tmp_path / "third-party.json",
            tmp_path / "first-party.json",
            tmp_path / "trust.json",
        )


def test_placeholder_images_require_authoritative_production_mapping() -> None:
    source = f"registry.example.invalid/model@{DIGEST}"
    used: set[str] = set()
    with pytest.raises(EvidenceError, match="no production mapping"):
        _production_reference(
            source, {}, {"registry.example.invalid"}, used, "unit"
        )
    production = f"registry.production.example/model@{DIGEST}"
    assert (
        _production_reference(
            source,
            {source: production},
            {"registry.example.invalid"},
            used,
            "unit",
        )
        == production
    )
    assert used == {source}


def test_source_surface_manifest_is_derived_from_actual_helm_consumers() -> None:
    manifest = validate_source_surfaces(
        ROOT, ROOT / "security/release-image-surfaces.json"
    )
    assert "stages/foundation/releases.tf::cert_manager" in manifest[
        "terraform_helm_releases"
    ]
    assert "stages/infrastructure/bootstrap/bootstrap.sh" in manifest[
        "direct_installer_scripts"
    ]
    declaration_hashes = _release_declaration_hashes(ROOT, manifest)
    assert set(declaration_hashes) == set(manifest["terraform_helm_releases"]) | set(
        manifest["direct_render_surfaces"]
    )
    assert all(len(value) == 64 for value in declaration_hashes.values())
    assert {build["id"] for build in manifest["first_party_builds"]} == {
        "control-plane",
        "admin-console",
    }
