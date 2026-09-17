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
    assert "signed-registry-referrers-query-receipt-json" in policy["evidence"][
        "required_formats"
    ]
    assert "image_signature_subject_and_source_binding" in policy["evidence"][
        "required_bindings"
    ]

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
    assert "SAI24_EVIDENCE_SIGNING_KEY_PEM" not in workflow
    assert "environment: sai24-release-attestation" in workflow
    assert "id-token: write" in workflow
    assert "oidc_attestation_broker.py sign" in workflow
    assert "oidc_attestation_broker.py registry-auth" in workflow
    assert "NVCR subjects require protected short-lived authentication" in workflow
    assert "github.event_name != 'pull_request'" in workflow
    assert '--render-packet "$RENDER_PACKET"' in workflow
    assert "--render-packet k8s-inference/security/production-render-packet.json" not in workflow
    assert "--first-party-inventory" in workflow
    assert "--catalog-image-map" in workflow
    assert "--materials-authorization" in workflow
    assert "image-gate-requirements.lock" in workflow
    assert "--subject" in workflow
    assert "--registry\\n" not in workflow
    assert "--release-closure" in workflow
    assert "--resolution-receipt" not in workflow

    normal_workflow = (ROOT.parent / ".github/workflows/k8s-inference.yml").read_text()
    assert "uses: ./.github/workflows/k8s-inference-image-security.yml" in normal_workflow
    assert "needs: image-security" in normal_workflow
    assert "promotion-gate:" in normal_workflow
    assert "secrets: inherit" not in normal_workflow
    assert "protected-release-promotion-gate:" in normal_workflow

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

    first_party = json.loads(
        (ROOT / "security/first-party-images.lock.json").read_text()
    )
    assert first_party["inventory_state"] == (
        "blocked_pending_protected_build_attestation"
    )
    assert {image["id"] for image in first_party["images"]} == {
        "control-plane",
        "admin-console",
    }
    assert all(image["digest_reference"] is None for image in first_party["images"])
    assert all("build_attestation" in image for image in first_party["images"])
    assert all("build_receipt" not in image for image in first_party["images"])

    catalog_map = json.loads((ROOT / "security/catalog-images.lock.json").read_text())
    assert catalog_map["mapping_state"] == (
        "blocked_pending_authoritative_production_mappings"
    )
    assert catalog_map["mappings"] == []

    packet_template = json.loads(
        (ROOT / "security/production-render-packet.json").read_text()
    )
    assert packet_template["status"] == "template_only_not_release_evidence"
    assert packet_template["source"] == {"commit": None, "tree": None}
    assert packet_template["schema"].endswith("/v2")
    assert packet_template["materials_authorization_sha256"] is None
    assert set(packet_template["terraform_plans"]) == {
        "stages/foundation",
        "stages/workloads",
    }


def test_catalog_mapping_is_consumed_by_runtime_and_model_express_cannot_bypass_gate() -> None:
    root_locals = (ROOT / "locals.tf").read_text()
    root_variables = (ROOT / "variables.tf").read_text()
    root_main = (ROOT / "main.tf").read_text()
    assert 'variable "catalog_image_map_path"' in root_variables
    assert "var.catalog_image_map_path == null" in root_variables
    assert '"${path.module}/security/catalog-images.lock.json"' in root_locals
    assert "catalog_image_mappings" in root_locals
    assert "lookup(\n      local.catalog_image_mappings" in root_locals
    assert "selected_placeholder_model_images" in root_main
    assert "contains(local.catalog_placeholder_registries" in root_locals

    deploy = (ROOT / "charts/addons/modelexpress/deploy.sh").read_text()
    assert 'helm upgrade "${helm_args[@]}" "${image_gate[@]}"' in deploy
    assert 'helm install "${helm_args[@]}" "${image_gate[@]}"' in deploy
    assert "FS2_SAI24_RELEASE_CLOSURE" in deploy
    assert "--verify-direct-closure" in deploy
    assert "--rendered-manifest-stdin" in deploy
    assert '--release-name "$RELEASE_NAME"' in deploy
    assert '--namespace "$NAMESPACE"' in deploy
    assert '--chart-path "$CHART_DIR"' in deploy
    assert '--values-file "$VALUES_FILE"' in deploy
    assert "FS2_IMAGE_ATTESTATION_TRUST" not in deploy
    main_body = deploy.split("main() {", maxsplit=1)[1]
    assert main_body.index("verify_release_evidence") < main_body.index(
        "check_prerequisites"
    )
    postrenderer = (ROOT / "security/helm_image_postrenderer.py").read_text()
    assert "FS2_IMAGE_ATTESTATION_TRUST" not in postrenderer
    assert "validate_image_gate_authorization" in postrenderer

    semantic_gate = (ROOT / "security/semantic_yaml_images.py").read_text()
    assert "yaml.compose_all" in semantic_gate
    assert "Counter(lexical_values) != Counter(semantic)" in semantic_gate

    for stage in ("foundation", "workloads"):
        gate = (ROOT / f"stages/{stage}/release_image_gate.tf").read_text()
        assert "release_image_closure_gate" in gate
        assert "--verify-terraform-closure" in gate
        cluster_contract = (ROOT / f"stages/{stage}/cluster_contract.tf").read_text()
        assert "depends_on = [terraform_data.release_image_closure_gate]" in cluster_contract
    apply_wrapper = (ROOT / "security/apply_signed_terraform_plan.sh").read_text()
    assert 'terraform -chdir="$source_root/$root" show -json "$saved_plan"' in apply_wrapper
    assert 'exec terraform -chdir="$source_root/$root" apply "$saved_plan"' in apply_wrapper

    evidence_validator = (ROOT / "security/image_security_evidence.py").read_text()
    assert "registry-referrers-query-receipt/v1" in evidence_validator
    assert "/referrers/{subject_digest}" in evidence_validator
    assert "application/vnd.dev.cosign.simplesigning.v1+json" in evidence_validator
    assert "evidence-retention-provider-receipt/v1" in evidence_validator
    assert "zipfile.ZipFile" in evidence_validator
    assert '"https://in-toto.io/Statement/v1"' in evidence_validator
    assert "serialized_provenance" not in evidence_validator
    broker = (ROOT / "security/oidc_attestation_broker.py").read_text()
    assert '"actions": ["pull"]' in broker
    assert 'registry_command.add_argument("--subject"' in broker
    assert 'registry_command.add_argument("--registry"' not in broker
    surfaces = json.loads((ROOT / "security/release-image-surfaces.json").read_text())
    assert "charts/addons/modelexpress/deploy.sh" in surfaces[
        "direct_installer_scripts"
    ]
    assert surfaces["non_release_helm_scripts"]
    modelexpress_readme = (ROOT / "charts/addons/modelexpress/README.md").read_text()
    assert "helm install " not in modelexpress_readme
    assert "helm upgrade " not in modelexpress_readme
