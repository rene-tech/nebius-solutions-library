from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "sai10_guard", ROOT / "scripts/secret_migration_guard.py"
)
assert SPEC is not None and SPEC.loader is not None
GUARD = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(GUARD)


def configured_plan(registry: dict, root: str) -> dict:
    addresses = [
        item["address"]
        for item in registry["terraform_resource_addresses"]
        if item["root"] == root
    ]
    return {
        "configuration": {
            "root_module": {
                "resources": [{"address": address} for address in addresses],
                "module_calls": {},
            }
        },
        "prior_state": {"values": {"root_module": {"resources": []}}},
        "resource_changes": [],
        "variables": {},
    }


def test_registry_normatively_covers_all_60_integrated_addresses() -> None:
    registry = GUARD.load_registry()
    assert len(registry["terraform_resource_addresses"]) == 60
    for item in registry["terraform_resource_addresses"]:
        assert GUARD.is_protected_address(
            item["address"], registry=registry, terraform_root=item["root"]
        ), item


def test_state_mv_rm_laundering_reproduction_is_rejected_before_receipt_counting() -> (
    None
):
    registry = GUARD.load_registry()
    plan = configured_plan(registry, "workloads")
    plan["resource_changes"] = [
        {
            "address": "kubernetes_secret_v1.admin",
            "change": {"actions": ["create"], "before": None, "after": {}},
        },
        {
            "address": "kubernetes_secret_v1.escaped_admin",
            "change": {"actions": ["delete"], "before": {}, "after": None},
        },
    ]
    with pytest.raises(GUARD.GuardError, match="fixed address create refused"):
        GUARD.inspect_plan(plan, registry=registry, terraform_root="workloads")

    escaped_delete = configured_plan(registry, "workloads")
    escaped_delete["resource_changes"] = [plan["resource_changes"][1]]
    with pytest.raises(GUARD.GuardError, match="unregistered credential"):
        GUARD.inspect_plan(
            escaped_delete,
            registry=registry,
            terraform_root="workloads",
        )


def test_declared_registry_address_cannot_be_omitted_or_moved() -> None:
    registry = GUARD.load_registry()
    plan = configured_plan(registry, "workloads")
    plan["configuration"]["root_module"]["resources"].pop()
    with pytest.raises(GUARD.GuardError, match="inventory differs"):
        GUARD.inspect_plan(plan, registry=registry, terraform_root="workloads")

    moved = configured_plan(registry, "workloads")
    moved["resource_changes"] = [
        {
            "address": 'kubernetes_secret_v1.admin_versioned["2"]',
            "previous_address": "kubernetes_secret_v1.admin",
            "change": {"actions": ["no-op"], "before": {}, "after": {}},
        }
    ]
    with pytest.raises(GUARD.GuardError, match="move"):
        GUARD.inspect_plan(moved, registry=registry, terraform_root="workloads")


def test_apply_gate_is_additive_and_authority_is_not_caller_selected() -> None:
    guard_source = (ROOT / "scripts/secret_migration_guard.py").read_text()
    wrapper_source = (ROOT / "inference-stack").read_text()
    for root in (
        ROOT / "stages/infrastructure",
        ROOT / "stages/foundation",
        ROOT / "stages/workloads",
        ROOT / "reference-data/terraform",
    ):
        source = (root / "credential_migration_gate.tf").read_text()
        assert 'resource "terraform_data" "credential_apply_gate_generation"' in source
        assert "prevent_destroy = true" in source
        assert "triggers_replace" not in source
        assert '"/usr/bin/python3"' in source
        assert '"/opt/fs2/k8s-inference/scripts/secret_migration_guard.py"' in source
        assert '${path.module}/../../scripts/secret_migration_guard.py' not in source
        assert '"--registry"' not in source
    assert 'PRODUCTION_TERRAFORM_COMMAND = "/snap/bin/terraform"' in guard_source
    assert "FS2_TERRAFORM_EXECUTABLE" not in guard_source
    assert "private_temporary_json" not in wrapper_source
    assert "live_secret_inventory(" not in wrapper_source


def test_authority_uses_canonical_backend_and_external_anchor() -> None:
    service = (ROOT / "scripts/credential_authority_service.py").read_text()
    provider = (ROOT / "scripts/credential_authority_provider.py").read_text()
    guard_source = (ROOT / "scripts/secret_migration_guard.py").read_text()
    evidence = (ROOT / "scripts/credential_evidence.py").read_text()
    unit = (ROOT / "deploy/systemd/fs2-credential-authority.service").read_text()
    assert '"state_paths"' not in service
    assert '"backend_type"' in service
    assert '"remote", "s3"' in service
    assert '"state", "pull"' in provider
    assert '"TF_DATA_DIR": root["terraform_data_dir"]' in provider
    assert 'f"-backend-config={root[\'backend_config_path\']}"' in provider
    assert '"registry_sha256": registry_sha256' in provider
    assert "local durable registry differs from root authority policy" in guard_source
    assert "credential-evidence-record/v1" in evidence
    assert "producer_signature" in evidence
    assert "external_anchor" in evidence
    assert "Restart=always" in unit
    assert "AF_UNIX AF_INET AF_INET6" in unit
    socket_unit = (ROOT / "deploy/systemd/fs2-credential-authority.socket").read_text()
    assert "RemoveOnStop=no" in socket_unit
    assert "FileDescriptorName=credential-authority" in socket_unit


def test_handoff_is_provider_derived_and_has_no_issuance_cli() -> None:
    source = (ROOT / "scripts/operator_handoff.py").read_text()
    parser = source[source.index("def parse_args") :]
    provider = (ROOT / "scripts/credential_authority_provider.py").read_text()
    assert 'subparsers.add_parser("issue")' not in parser
    assert '"operation": "viewer-handoff-inventory"' in source
    assert '"create_serviceaccount_tokens"' in provider
    assert '"create_tokenreviews"' in provider
    assert '"operator-handoff-viewer"' in provider
    assert '"viewer"' in provider


def test_evidence_and_rotation_have_no_disable_revoke_or_delete_transition() -> None:
    evidence = (ROOT / "scripts/append_only_evidence.py").read_text()
    rotation = (ROOT / "scripts/credential_rotation.py").read_text()
    parser = rotation[rotation.index("def parse_args") :]
    assert "os.O_EXCL" in evidence
    assert "previous_sha256" in evidence
    assert "_recover_complete_head_fragment" in evidence
    assert "conflicting complete head fragments" in evidence
    assert "os.replace" not in evidence
    assert ".unlink(" not in evidence
    for forbidden in ("disable-old", "delete-old", "revoke-old"):
        assert forbidden not in parser


def test_every_secret_consumer_rollout_hashes_exact_binding_and_readiness() -> None:
    templates = ROOT / "charts/control-plane/fs2-serve-control-plane/templates"
    for name in (
        "deployment.yaml",
        "model-controller-deployment.yaml",
        "migration-job.yaml",
        "maintenance-cronjob.yaml",
        "bootstrap-access-job.yaml",
        "bootstrap-scientific-access-job.yaml",
        "bootstrap-website-access-job.yaml",
    ):
        source = (templates / name).read_text()
        assert ".Values.secretRollout.bindingSha256" in source
        assert ".Values.secretRollout.readinessReceiptSha256" in source
        assert ".Values.secretRollout.rolloutStep" in source


def test_runtime_customer_storage_crypto_uses_one_aad_boundary() -> None:
    crypto = (ROOT / "components/control-plane/src/fs2_serve/crypto.py").read_text()
    cli = (ROOT / "components/control-plane/src/fs2_serve/cli.py").read_text()
    repository = (
        ROOT
        / "components/control-plane/src/fs2_serve/customer_storage_credentials.py"
    ).read_text()
    migration = (
        ROOT
        / "components/control-plane/migrations/0030_customer_storage_credentials.sql"
    ).read_text()
    deployment = (
        ROOT
        / "charts/control-plane/fs2-serve-control-plane/templates/deployment.yaml"
    ).read_text()
    tests = (
        ROOT / "components/control-plane/tests/test_keyring_rotation_compatibility.py"
    ).read_text()
    assert "class CustomerStorageCrypto" in crypto
    assert crypto.count("PayloadCipher.customer_storage_aad") >= 3
    assert "customer_storage_crypto = _customer_storage_crypto(settings)" in cli
    assert "PostgresCustomerStorageCredentialRepository" in cli
    assert "PayloadCipher.customer_storage_aad" in repository
    assert "INSERT INTO fs2_customer_storage_credential_generations" in repository
    assert "ORDER BY generation DESC LIMIT 1" in repository
    assert "DELETE FROM fs2_customer_storage" not in repository
    assert "PRIMARY KEY (tenant_id, principal_id, generation)" in migration
    assert "customer-storage-cipher" not in deployment
    assert "customer-storage-name" not in deployment
    for claim in ("rollback", "tenant", "principal", "migrate"):
        assert claim in tests.lower()


def test_integration_and_retirement_are_fail_closed() -> None:
    dependencies = json.loads(
        (ROOT / "security/sai-10-integration-dependencies.json").read_text()
    )
    assert dependencies["integration_authorized"] is False
    assert (
        dependencies["dependencies"]["SAI-05"]["status"] == "accepted-source-ancestor"
    )
    sai06 = dependencies["dependencies"]["SAI-06"]
    assert sai06["status"] == "static-source-go-integration-live-unaccepted"
    assert sai06["commit"] == "8b48f3467f49bc523146dd86da56c36ef29ca951"
    assert sai06["tree"] == "3081ba082ebb5ee2db8b92b3e8b01a79fa7dd30d"
    assert len(sai06["required_credential_addresses"]["infrastructure"]) == 4
    assert len(sai06["required_credential_addresses"]["workloads"]) == 4
    for ticket in ("SAI-08", "SAI-09"):
        assert dependencies["dependencies"][ticket]["commit"] is None
    sai08 = dependencies["dependencies"]["SAI-08"]
    assert any(
        "PayloadCipher.customer_storage_aad" in item
        for item in sai08["required_semantics"]
    )
    assert any("request-debug" in item for item in sai08["required_semantics"])

    guard_source = (ROOT / "scripts/secret_migration_guard.py").read_text()
    assert 'operation": "artifact-inventory"' in guard_source
    assert "caller-supplied live Secret inventories are forbidden" in guard_source
    assert 'DISPOSITION_ACTIONS = frozenset({"encrypted-rewrap"})' in guard_source


def test_every_deployable_terraform_root_forbids_local_state() -> None:
    roots = (
        ROOT,
        ROOT / "stages/foundation",
        ROOT / "stages/infrastructure",
        ROOT / "stages/workloads",
        ROOT / "reference-data/terraform",
        ROOT / "model-artifacts/terraform",
    )
    for root in roots:
        source = (root / "versions.tf").read_text()
        assert 'backend "s3" {}' in source
        assert 'backend "local"' not in source
    wrapper = (ROOT / "inference-stack").read_text()
    assert 'Path("/etc/fs2-serve/terraform-backends")' in wrapper
    assert 'f"-backend-config={backend_config}"' in wrapper
    assert 'f"-backend-config=path={legacy_backend}"' not in wrapper


def test_secret_commitments_hash_each_decoded_value_before_the_map() -> None:
    sources = "\n".join(
        path.read_text()
        for path in (
            ROOT / "stages/foundation/cluster_contract.tf",
            ROOT / "stages/workloads/bootstrap_access.tf",
            ROOT / "stages/workloads/database.tf",
            ROOT / "stages/workloads/modelexpress.tf",
            ROOT / "stages/workloads/scientific_artifacts.tf",
            ROOT / "stages/workloads/secrets.tf",
            ROOT / "reference-data/terraform/main.tf",
        )
    )
    assert sources.count('"fs2.nebius.ai/content-sha256"') == 19
    for block in sources.split('"fs2.nebius.ai/content-sha256"')[1:]:
        assert "sha256(jsonencode(" in block[:180]
        assert "sha256(" in block[block.index("sha256(jsonencode(") + 18 : 800]


def test_storage_runtime_is_request_wired_but_dependency_gated() -> None:
    helpers = (
        ROOT / "charts/control-plane/fs2-serve-control-plane/templates/_helpers.tpl"
    ).read_text()
    api = (ROOT / "components/control-plane/src/fs2_serve/api.py").read_text()
    debug = (
        ROOT / "components/control-plane/src/fs2_serve/request_debug.py"
    ).read_text()
    variables = (ROOT / "stages/workloads/variables.tf").read_text()
    assert 'value: {{ .Values.customerStorageCredentials.enabled | quote }}' in helpers
    assert '"/v1/customer-storage/credential"' in api
    assert "customer_storage_reconciler" in api
    assert "customer_storage_disclosure" in api
    assert 'path == "/v1/customer-storage/credential"' in debug
    assert "var.keyring_generations.storage.active > 1" in variables
    assert 'var.credential_consumer_rollout_step == "current-write"' in variables


def test_authority_requires_source_trust_and_exact_automation_identity() -> None:
    trust = json.loads(
        (ROOT / "security/credential-evidence-source-trust.json").read_text()
    )
    service = (ROOT / "scripts/credential_authority_service.py").read_text()
    provider = (ROOT / "scripts/credential_authority_provider.py").read_text()
    client = (ROOT / "scripts/credential_provider_adapter.py").read_text()
    assert trust["accepted_trust_bundles"] == []
    assert trust["minimum_witnesses"] == 2
    assert "not accepted by checked source" in client
    assert 'len(config["allowed_client_uids"]) != 1' in service
    assert "timedelta(hours=24)" in service
    assert "evidence_identity_proof" in provider
    assert "release_identity_proof" in provider
    assert '"credential-evidence-reader"' in provider
    assert '"credential-release-automation"' in provider
    assert '(policy["project_id"], "viewer")' in provider
    assert 'policy["profile"]' not in provider


def test_readiness_and_inventory_fail_closed_on_exact_live_bindings() -> None:
    service = (ROOT / "scripts/credential_authority_service.py").read_text()
    provider = (ROOT / "scripts/credential_authority_provider.py").read_text()
    guard_source = (ROOT / "scripts/secret_migration_guard.py").read_text()
    registry = json.loads(
        (ROOT / "security/durable-credential-registry.json").read_text()
    )
    contracts = json.loads(
        (ROOT / "security/credential-consumer-contracts.json").read_text()
    )
    assert '"credential_bindings"' in service
    assert 'canonical_sha256(bindings) != parameters["bindings_sha256"]' in service
    assert '"credential_bindings": bindings' in guard_source
    assert "reconcile_global_provider_inventory" in provider
    assert "all-cluster-secrets-and-all-project-iam" in provider
    assert registry["provider_inventory_exemptions"] == {
        "kubernetes_secrets": [],
        "nebius_iam": [],
    }
    assert registry["pending_credential_ids"] == [
        "postgresql-backup-s3",
        "postgresql-backup-s3-secret",
    ]
    assert contracts["pending_contract_ids"] == registry["pending_credential_ids"]
