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
    assert "protected_scanner_executable_toolchain_binding" in policy["evidence"][
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
    assert '"${reviewed_helm[@]}" upgrade "${helm_args[@]}" "${image_gate[@]}"' in deploy
    assert '"${reviewed_helm[@]}" install "${helm_args[@]}" "${image_gate[@]}"' in deploy
    assert "FS2_SAI24_RELEASE_CLOSURE" in deploy
    assert 'FS2_EXTERNAL_CAPSULE_ACTIVE:-}" != "1"' in deploy
    assert "register-workload-registry-refresh" in deploy
    assert "--surface-id" in deploy
    assert '--release-name "$RELEASE_NAME"' in deploy
    assert '--namespace "$NAMESPACE"' in deploy
    assert '--chart-path "$CHART_DIR"' in deploy
    assert '--values-file "$VALUES_FILE"' in deploy
    assert "FS2_IMAGE_ATTESTATION_TRUST" not in deploy
    main_body = deploy.split("main() {", maxsplit=1)[1]
    assert main_body.index("acquire_pull_authorization") < main_body.index(
        "deploy_chart"
    )
    deploy_body = deploy.split("deploy_chart() {", maxsplit=1)[1].split(
        "# Function to show deployment status", maxsplit=1
    )[0]
    assert deploy_body.index('template "$RELEASE_NAME"') < deploy_body.index(
        "activate_pull_authorization"
    )
    assert deploy_body.index("activate_pull_authorization") < deploy_body.index(
        '"${reviewed_helm[@]}" upgrade'
    )
    activation_body = deploy.split(
        "activate_pull_authorization() {", maxsplit=1
    )[1].split("configure_reviewed_tools() {", maxsplit=1)[0]
    assert activation_body.index("register-workload-registry-refresh") < (
        activation_body.index("apply-registry-secret")
    )
    assert "--refresh-registration" in activation_body
    assert "--broker-fresh-credential-at-secret-admission" in activation_body
    assert "--admission-contract" in activation_body
    assert "--maximum-readiness-age-seconds 60" in activation_body
    assert '--docker-config "$PULL_DOCKER_CONFIG"' not in activation_body
    postrenderer = (ROOT / "security/helm_image_postrenderer.py").read_text()
    assert "FS2_IMAGE_ATTESTATION_TRUST" not in postrenderer
    assert "validate_image_gate_authorization" in postrenderer

    semantic_gate = (ROOT / "security/semantic_yaml_images.py").read_text()
    assert "yaml.compose_all" in semantic_gate
    assert "Counter(lexical_values) != Counter(semantic)" in semantic_gate

    for stage in ("foundation", "workloads"):
        gate = (ROOT / f"stages/{stage}/release_image_gate.tf").read_text()
        assert "release_image_closure_gate" in gate
        assert "terraform_apply_gate_entrypoint.py" in gate
        assert "external_trust_path" in gate
        cluster_contract = (ROOT / f"stages/{stage}/cluster_contract.tf").read_text()
        assert "depends_on = [terraform_data.release_image_closure_gate]" in cluster_contract
    apply_wrapper = (ROOT / "security/apply_signed_terraform_plan.sh").read_text()
    assert 'exec "$FS2_IMAGE_GATE_BOOTSTRAP" signed-terraform-apply' in apply_wrapper
    assert "stages/infrastructure|stages/foundation|stages/workloads" in apply_wrapper
    assert "--registry-refresh-registration" in apply_wrapper
    assert "terraform -chdir=" not in apply_wrapper

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
    lease_surfaces = {
        row["key"]: row for row in surfaces["workload_registry_secret_leases"]
    }
    assert set(lease_surfaces) == {"models", "observability", "modelexpress"}
    assert lease_surfaces["models"]["subject_sources"] == ["catalog_image_sources"]
    assert lease_surfaces["observability"]["subject_consumers"] == [
        "stages/workloads/values/dcgm-exporter.yaml"
    ]
    assert lease_surfaces["modelexpress"]["resource_address"] == (
        "kubernetes_secret_v1.modelexpress_nvcrio[0]"
    )
    modelexpress_readme = (ROOT / "charts/addons/modelexpress/README.md").read_text()
    assert "helm install " not in modelexpress_readme
    assert "helm upgrade " not in modelexpress_readme


def test_external_capsule_is_the_only_release_execution_authority() -> None:
    external = json.loads(
        (ROOT / "security/external-capsule-trust.example.json").read_text()
    )
    assert external["state"] == "blocked_example_not_an_authority"
    assert external["source_root"]["transport"] is None
    assert external["capsule_contract"]["deny_path_reopen"] is True
    assert external["capsule_contract"]["deny_path_lookup"] is True
    assert {
        "terraform-init",
        "terraform-validate-root",
        "signed-terraform-plan",
        "signed-terraform-apply",
        "tool-sha256",
        "acquire-release-registry-credential",
        "prepare-workload-registry-secret-leases",
    }.issubset(external["capsule_contract"]["bootstrap_commands"])
    assert external["capsule_contract"]["protected_entrypoint_environment"] == {
        "FS2_EXTERNAL_CAPSULE_ACTIVE": "1",
        "FS2_CAPSULE_SOURCE_ROOT": "externally-bound-read-only-tree",
        "FS2_CAPSULE_TOOL_DIR": "externally-bound-read-only-tools",
    }
    protected_entry = external["capsule_contract"]["inference_stack_entry"]
    assert protected_entry["capability_transport"] == "inherited-unix-sock-seqpacket"
    assert protected_entry["required_peer_uid"] == 0
    assert protected_entry["caller_selectable_bindings"] is False
    provider_admission = external["capsule_contract"][
        "workload_secret_provider_rpc_admission"
    ]
    assert provider_admission["deny_planning_credential_forwarding"] is True
    assert provider_admission["deny_credential_reuse_across_resource_rpcs"] is True
    assert provider_admission["mutate_write_only_secret_data_only"] is True
    assert provider_admission["preserve_planned_metadata_and_data_wo_revision"] is True
    assert (
        provider_admission["require_complete_signed_external_handoff_before_apply_success"]
        is True
    )
    assert provider_admission["handoff_signer_id"] is None
    assert provider_admission["handoff_public_key_sha256"] is None
    assert provider_admission["handoff_verifier_sha256"] is None
    registry_operations = external["capsule_contract"][
        "registry_operation_authorization"
    ]
    assert registry_operations["flat_subject_action_cross_product_forbidden"] is True
    assert registry_operations["docker_auth_host_set_must_equal_grant_partitions"] is True

    toolchain = json.loads(
        (ROOT / "security/execution-toolchain.lock.json").read_text()
    )
    assert toolchain["state"] == "blocked_pending_independent_toolchain_capture"
    assert set(toolchain["terraform_roots"]) == {
        ".",
        "stages/infrastructure",
        "stages/foundation",
        "stages/workloads",
    }
    provider_proxy = toolchain["terraform_execution"][
        "workload_secret_provider_rpc_proxy"
    ]
    assert provider_proxy["path"] is None
    assert provider_proxy["credential_reuse_across_resource_rpcs"] is False
    assert provider_proxy["planning_credential_forwarded_to_apply"] is False
    assert toolchain["terraform_execution"]["bind_init_plan_show_apply_to_same_capsule"] is True
    assert toolchain["terraform_execution"]["network_provider_installation"] is False
    assert all(
        not root["provider_packages"]
        for root in toolchain["terraform_roots"].values()
    )

    validator = (ROOT / "security/execution_toolchain.py").read_text()
    assert "def _load_fd(" in validator
    assert "external = _load(external_path)" not in validator
    assert "trust = _load(trust_path)" not in validator
    assert "lock = _load(protected_path)" not in validator

    stack = (ROOT / "inference-stack").read_text()
    assert "receive_capsule_bindings" in stack
    assert "SO_PEERCRED" in stack
    assert "SOCK_SEQPACKET" in stack
    assert "direct inference-stack execution is disabled" in stack
    parser_source = stack.split("def parse_args", 1)[1].split("def main", 1)[0]
    assert "--image-gate-bootstrap" not in parser_source
    assert "--external-capsule-trust" not in parser_source
    assert "--registry-docker-config" not in parser_source
    assert "FS2_CAPSULE_TOOL_DIR" in stack
    assert "signed-terraform-apply" in stack
    assert "signed-terraform-plan" in stack
    assert 'terraform_capsule_command("terraform-init"' in stack
    assert "external Terraform capsule did not emit its signed binary-plan receipt" in stack
    assert 'f"{plan_path}.capsule.json"' in stack
    assert 'DEPLOY_ROOT: "."' in stack
    assert 'INFRA_ROOT: "stages/infrastructure"' in stack
    assert "FS2_NVCR_DOCKERCONFIGJSON" not in stack
    assert 'purpose="workload-secret"' in stack
    assert "MINIMUM_WORKLOAD_CREDENTIAL_TTL_SECONDS = 600" in stack

    jobset = (ROOT / "modules/jobset-controller/main.tf").read_text()
    assert 'binary_path = var.release_image_contract.bootstrap_path' in jobset
    assert 'binary_path = "/usr/bin/env"' not in jobset

    workflow = (
        ROOT.parent / ".github/workflows/k8s-inference-image-security.yml"
    ).read_text()
    protected = workflow.split("scan-release-image-closure:", maxsplit=1)[1]
    assert 'export PATH="$FS2_CAPSULE_TOOL_DIR"' in protected
    assert "Record externally bound Trivy identity" in protected
    assert "releases/download" not in protected
    assert "--scanner-executable-sha256" in protected


def test_private_pull_refresh_contract_stays_fail_closed_until_attested() -> None:
    trust = json.loads((ROOT / "security/image-attestation-trust.json").read_text())
    policy = trust["workload_registry_authentication"]
    assert policy["maximum_ttl_seconds"] == 900
    assert policy["maximum_refresh_interval_seconds"] == 300
    assert policy["maximum_refresh_readiness_age_seconds"] == 60
    assert policy["authorized_refresh_owner_ids"] == []
    assert policy["refresh_controller_contract_sha256"] is None
    assert policy["secret_admission_contract_sha256"] is None
    assert policy["authorized_secret_admission_proxy_ids"] == []
    assert policy["authorized_secret_admission_handoff_signer_ids"] == []

    refresh = json.loads(
        (ROOT / "security/workload-registry-refresh-contract.json").read_text()
    )
    assert refresh["state"] == (
        "blocked_pending_security_owner_and_runtime_attestation"
    )
    assert refresh["retire_superseded_without_delete"] is True
    assert refresh["maximum_readiness_receipt_age_seconds"] == 60
    assert refresh["secret_admission_contract_sha256"] is None
    assert refresh["failure_policy"]["delete_secret"] is False
    assert refresh["runtime"]["image"] is None
    assert refresh["runtime"]["sbom_sha256"] is None
    assert refresh["runtime"]["provenance_sha256"] is None

    admission = json.loads(
        (ROOT / "security/workload-registry-secret-admission-contract.json").read_text()
    )
    assert admission["schema"] == (
        "fs2-serve.nebius.ai/workload-registry-secret-admission-contract/v2"
    )
    assert admission["state"] == (
        "blocked_pending_security_owner_and_runtime_attestation"
    )
    assert admission["provider_rpc_mode"] == (
        "broker-immediately-before-secret-create-or-update"
    )
    assert admission["planning_credential_is_never_forwarded_to_apply"] is True
    assert admission["provider_proxy_mutates_planned_metadata"] is False
    assert admission["provider_proxy_mutates_data_wo_revision"] is False
    assert admission["provider_proxy_mutates_only_write_only_secret_data"] is True
    assert admission["token_receipt_persisted_in_terraform_state"] is False
    assert admission["token_revision_persisted_in_terraform_state"] is False
    assert (
        admission["authoritative_handoff"][
            "terraform_apply_success_requires_verified_handoff"
        ]
        is True
    )
    assert admission["fail_closed"]["credential_reuse_across_resource_rpcs_allowed"] is False
    assert admission["runtime"]["proxy_id"] is None
    assert admission["runtime"]["handoff_signer_id"] is None
    assert admission["runtime"]["handoff_public_key_sha256"] is None
    assert admission["runtime"]["handoff_verifier_sha256"] is None

    variables = (ROOT / "variables.tf").read_text()
    assert "static NVCR Docker config input is forbidden" in variables
    workload_variables = (ROOT / "stages/workloads/variables.tf").read_text()
    for field in (
        "lease_id",
        "lease_generation",
        "subject_scope_sha256",
        "refresh_owner_id",
        "refresh_registration_sha256",
        "retire_superseded_without_delete",
        "secret_admission_proxy_id",
        "secret_admission_contract_sha256",
        "state_ownership",
        "token_receipt_destination",
    ):
        assert field in workload_variables
    assert 'variable "nvcrio_dockerconfigjson"' not in workload_variables
    assert 'variable "nvcrio_credential_authorization"' not in workload_variables
