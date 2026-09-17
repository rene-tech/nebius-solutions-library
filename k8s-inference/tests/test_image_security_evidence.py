from __future__ import annotations

import hashlib
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
from security.oidc_attestation_broker import BrokerError, registry_auth
from security.release_image_closure import (
    _is_shell_entrypoint,
    _production_reference,
    _release_declaration_hashes,
    _script_helm_installs,
    _validate_terraform_resource_closure,
    validate_source_surfaces,
    verify_direct_invocation,
)
from security.semantic_yaml_images import (
    SemanticYamlError,
    independently_validated_image_scalars,
)
from security.yaml_image_references import YamlImageError, image_scalars


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
        lambda _path, _trust, **_kwargs: [
            {"digest_reference": first_party_reference}
        ],
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


@pytest.mark.parametrize(
    "rendered",
    [
        "containers:\n  - image : registry.example/image:1.0\n",
        "containers:\n  - 'image': 'registry.example/image:1.0'\n",
        'containers: [{"image" : "registry.example/image:1.0", name: api}]\n',
        "containers:\n  - {name: api, image: registry.example/image:1.0}\n",
        "? image\n: registry.example/image:1.0\n",
    ],
)
def test_yaml_image_lexer_closes_spacing_quoted_key_and_flow_mapping_bypasses(
    rendered: str,
) -> None:
    scalars = image_scalars(rendered)
    assert [scalar.reference for scalar in scalars] == [
        "registry.example/image:1.0"
    ]


def test_yaml_image_lexer_ignores_comments_and_block_scalar_payloads() -> None:
    rendered = (
        "note: |\n"
        "  image: registry.example/inside-text:1\n"
        "# image: registry.example/comment:1\n"
        "container: {image: registry.example/real:1}\n"
    )
    assert [scalar.reference for scalar in image_scalars(rendered)] == [
        "registry.example/real:1"
    ]


def test_yaml_image_lexer_rejects_composite_or_multiline_image_values() -> None:
    for rendered in (
        "container: {image: {repository: example.invalid/repo}}\n",
        "container:\n  image:\n    repository: example.invalid/repo\n",
        "container: {image: *runtime_image}\n",
        "defaults: &runtime\n  image: registry.example/image:1.0\n"
        "container:\n  <<: *runtime\n",
    ):
        with pytest.raises(YamlImageError):
            image_scalars(rendered)


@pytest.mark.parametrize(
    "rendered",
    [
        "defaults: &runtime\n  image: registry.example/image:1\ncontainer: *runtime\n",
        "defaults: &runtime\n  image: registry.example/image:1\ncontainer:\n  <<: *runtime\n",
        "container:\n  image: registry.example/one:1\n  image: registry.example/two:2\n",
    ],
)
def test_independent_yaml_parser_rejects_graph_and_lexer_disagreement(
    rendered: str,
) -> None:
    with pytest.raises(SemanticYamlError):
        independently_validated_image_scalars(rendered)


def test_direct_installer_rejects_values_absent_from_signed_closure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import security.release_image_closure as closure_module

    trust = tmp_path / "trust.json"
    surfaces = tmp_path / "surfaces.json"
    values = tmp_path / "values.yaml"
    closure = tmp_path / "closure.json"
    signature = tmp_path / "closure.json.sig"
    trust.write_text("trusted-source-policy")
    surfaces.write_text("governed-surfaces")
    values.write_text("replicaCount: 2\n")
    signature.write_text("detached-signature")
    chart = tmp_path / "chart"
    chart.mkdir()
    (chart / "Chart.yaml").write_text("name: modelexpress\nversion: 1.0.0\n")
    rendered = b"apiVersion: v1\nkind: ConfigMap\n"
    source_execution = {
        "kind": "direct-installer",
        "script_sha256": "a" * 64,
        "release_name_variable": "RELEASE_NAME",
        "namespace_variable": "NAMESPACE",
        "chart_source": "chart",
        "chart_source_sha256": "d" * 64,
        "commands_sha256_by_mode": {"install": "e" * 64, "upgrade": "f" * 64},
    }
    closure.write_text(
        json.dumps(
            {
                "schema": "fs2-serve.nebius.ai/release-image-closure/v2",
                "source": {"commit": "b" * 40, "tree": "c" * 40},
                "trust_policy_sha256": hashlib.sha256(trust.read_bytes()).hexdigest(),
                "surface_manifest_sha256": hashlib.sha256(
                    surfaces.read_bytes()
                ).hexdigest(),
                "surface_executions": {
                    "charts/addons/modelexpress": {
                        "source_execution": source_execution,
                        "ordered_values_sha256": [
                            hashlib.sha256(values.read_bytes()).hexdigest()
                        ],
                        "release_name": "modelexpress",
                        "namespace": "modelexpress",
                        "rendered_manifest_sha256": hashlib.sha256(rendered).hexdigest(),
                    }
                },
            }
        )
    )
    monkeypatch.setattr(
        closure_module, "validate_detached_signature", lambda *_args: None
    )
    monkeypatch.setattr(
        closure_module, "_source_tree_sha256", lambda _path: "d" * 64
    )
    monkeypatch.setattr(
        closure_module,
        "validate_attestation_identity",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        closure_module, "_git_identity", lambda _root: ("b" * 40, "c" * 40)
    )
    monkeypatch.setattr(
        closure_module,
        "validate_source_surfaces",
        lambda _root, _manifest: {
            "direct_render_surfaces": ["charts/addons/modelexpress"]
        },
    )
    monkeypatch.setattr(
        closure_module,
        "_direct_execution_bindings",
        lambda _root, _manifest, _path: {
            "charts/addons/modelexpress": source_execution
        },
    )

    verify_direct_invocation(
        root=tmp_path,
        manifest_path=surfaces,
        trust_path=trust,
        closure_path=closure,
        surface_id="charts/addons/modelexpress",
        values_paths=[values],
        release_name="modelexpress",
        namespace="modelexpress",
        mode="install",
        chart_path=chart,
        rendered_manifest=rendered,
    )
    values.write_text("replicaCount: 99\n")
    with pytest.raises(EvidenceError, match="selected values differ"):
        verify_direct_invocation(
            root=tmp_path,
            manifest_path=surfaces,
            trust_path=trust,
            closure_path=closure,
            surface_id="charts/addons/modelexpress",
            values_paths=[values],
            release_name="modelexpress",
            namespace="modelexpress",
            mode="install",
            chart_path=chart,
            rendered_manifest=rendered,
        )


def test_terraform_plan_closure_rejects_undeclared_helm_release() -> None:
    manifest = {
        "terraform_plan_owners": {
            "stages/foundation/releases.tf::keda": "stages/foundation"
        }
    }
    resources = {
        "stages/foundation": {
            "helm_release.keda": {},
            "helm_release.unreviewed": {},
        }
    }
    with pytest.raises(EvidenceError, match="ungoverned planned Helm resources"):
        _validate_terraform_resource_closure(manifest, resources)


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

    customer_override = f"customer.registry.example/runtime@{DIGEST}"
    assert _production_reference(
        customer_override,
        {customer_override: f"attacker.example/runtime@{DIGEST}"},
        {"registry.example.invalid"},
        used,
        "unit",
    ) == customer_override


def test_registry_auth_rejects_host_or_tag_scope_before_oidc_exchange(
    tmp_path: Path,
) -> None:
    trust = tmp_path / "trust.json"
    identity = tmp_path / "identity.json"
    trust.write_text(
        json.dumps(
            {
                "registry_authentication": {
                    "broker_url": "https://broker.invalid/token",
                    "audience": "release",
                    "maximum_ttl_seconds": 300,
                    "allowed_registries": ["registry.example"],
                }
            }
        )
    )
    identity.write_text("{}")
    for invalid in ("registry.example", "registry.example/team/image:latest"):
        with pytest.raises(BrokerError, match="digest-bound"):
            registry_auth(
                [invalid],
                "https://broker.invalid/token",
                "release",
                identity,
                trust,
                tmp_path / "docker-config.json",
            )


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
    assert "charts/addons/modelexpress/deploy.sh" in manifest[
        "direct_installer_scripts"
    ]
    assert "charts/addons/modelexpress" in manifest["direct_render_surfaces"]
    declaration_hashes = _release_declaration_hashes(ROOT, manifest)
    assert set(declaration_hashes) == set(manifest["terraform_helm_releases"]) | set(
        manifest["direct_render_surfaces"]
    )
    assert all(len(value) == 64 for value in declaration_hashes.values())
    assert {build["id"] for build in manifest["first_party_builds"]} == {
        "control-plane",
        "admin-console",
    }


def test_installer_discovery_accepts_extensionless_shell_and_arbitrary_helm_alias() -> None:
    source = '#!/usr/bin/env bash\nrelease_client=(helm --kube-context exact)\n"${release_client[@]}" upgrade --install demo chart\n'
    assert _is_shell_entrypoint("bin/release", source)
    assert _script_helm_installs(source) == [
        '"${release_client[@]}" upgrade --install demo chart'
    ]
