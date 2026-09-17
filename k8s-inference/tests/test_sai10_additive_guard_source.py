from __future__ import annotations

import ast
import importlib.util
import json
import re
from pathlib import Path

import pytest

from scripts.credential_authority_provider import (
    OPERATION_PURPOSES,
    READ_ONLY_OPERATIONS,
    ProviderError,
    base_address,
    classes_for_address,
    credential_presence_sets,
    generation_from_address,
    legacy_v1_adoption_classes,
    state_addresses,
    terraform_resource_type as provider_resource_type,
)


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
    ] + [
        item["address"]
        for item in registry["credential_activation_resource_addresses"]
        if item["root"] == root
    ]
    root_addresses = [
        address for address in addresses if not address.startswith("module.")
    ]
    module_addresses = [
        address
        for address in addresses
        if address.startswith("module.reference_data.")
    ]
    assert len(root_addresses) + len(module_addresses) == len(addresses)
    module_calls = {}
    if module_addresses:
        module_calls["reference_data"] = {
            "module": {
                "resources": [
                    {"address": address, "mode": "managed"}
                    for address in module_addresses
                ],
                "module_calls": {},
            }
        }
    return {
        "configuration": {
            "root_module": {
                "resources": [
                    {"address": address, "mode": "managed"}
                    for address in root_addresses
                ],
                "module_calls": module_calls,
            }
        },
        "prior_state": {"values": {"root_module": {"resources": []}}},
        "resource_changes": [],
        "variables": {},
    }


def test_registry_normatively_covers_all_68_current_and_sai06_addresses() -> None:
    registry = GUARD.load_registry()
    assert len(registry["terraform_resource_addresses"]) == 68
    for item in registry["terraform_resource_addresses"]:
        assert GUARD.is_protected_address(
            item["address"], registry=registry, terraform_root=item["root"]
        ), item


def test_reference_data_secret_uses_exact_workloads_module_addresses() -> None:
    registry = GUARD.load_registry()
    credential = next(
        item
        for item in registry["credentials"]
        if item["id"] == "reference-data-s3-secret"
    )
    fixed = "module.reference_data.kubernetes_secret_v1.object_storage"
    versioned = (
        "module.reference_data.kubernetes_secret_v1.object_storage_versioned"
    )
    raw_fixed = "module.reference_data[0].kubernetes_secret_v1.object_storage"
    raw_versioned = (
        'module.reference_data[0].kubernetes_secret_v1.object_storage_versioned["2"]'
    )
    assert credential["terraform_root"] == "workloads"
    assert any(
        re.fullmatch(pattern, fixed) for pattern in credential["address_regexes"]
    )
    assert any(
        re.fullmatch(pattern, raw_versioned)
        for pattern in credential["address_regexes"]
    )
    assert not any(
        re.fullmatch(
            pattern,
            "module.reference_data[1].kubernetes_secret_v1.object_storage",
        )
        for pattern in credential["address_regexes"]
    )
    assert base_address(raw_fixed) == fixed
    assert base_address(raw_versioned) == versioned
    assert provider_resource_type(raw_fixed) == "kubernetes_secret_v1"
    assert GUARD.credential_resource_type(raw_fixed) == "kubernetes_secret_v1"
    assert {
        item["credential_class"]
        for item in classes_for_address(registry, "workloads", raw_fixed)
    } == {"reference-data-s3-secret"}
    assert not classes_for_address(
        registry, "workloads", "kubernetes_secret_v1.object_storage"
    )
    assert not classes_for_address(
        registry, "reference-data", "kubernetes_secret_v1.object_storage"
    )
    assert legacy_v1_adoption_classes(
        registry, terraform_root="workloads", address=raw_fixed
    ) == frozenset({"reference-data-s3-secret"})
    assert not legacy_v1_adoption_classes(
        registry, terraform_root="workloads", address=raw_versioned
    )
    assert not legacy_v1_adoption_classes(
        registry,
        terraform_root="workloads",
        address="module.reference_data[1].kubernetes_secret_v1.object_storage",
    )

    raw_state = {
        "resources": [
            {
                "mode": "managed",
                "module": "module.reference_data[0]",
                "type": "kubernetes_secret_v1",
                "name": "object_storage",
                "instances": [
                    {
                        "attributes": {
                            "id": "fs2-reference-data/object-storage-v1",
                            "immutable": True,
                            "metadata": [
                                {
                                    "name": "object-storage-v1",
                                    "namespace": "fs2-reference-data",
                                    "uid": "uid-v1",
                                    "resource_version": "1",
                                    "annotations": {},
                                }
                            ],
                        }
                    }
                ],
            },
            {
                "mode": "managed",
                "module": "module.reference_data[0]",
                "type": "kubernetes_secret_v1",
                "name": "object_storage_versioned",
                "instances": [
                    {
                        "index_key": "2",
                        "attributes": {
                            "id": "fs2-reference-data/object-storage-v2",
                            "immutable": True,
                            "metadata": [
                                {
                                    "name": "object-storage-v2",
                                    "namespace": "fs2-reference-data",
                                    "uid": "uid-v2",
                                    "resource_version": "2",
                                    "annotations": {
                                        "fs2.nebius.ai/credential-class": "reference-data-s3-secret",
                                        "fs2.nebius.ai/credential-generation": "2",
                                        "fs2.nebius.ai/content-sha256": "a" * 64,
                                    },
                                }
                            ],
                        },
                    }
                ],
            },
        ]
    }
    projected = state_addresses(raw_state)
    assert [item["address"] for item in projected] == [raw_fixed, raw_versioned]
    required = registry["credential_presence"]["feature_gated"][
        "reference-data-s3-secret"
    ]["groups"]["reference-data"]["required_addresses"]
    assert {(item["root"], item["address"]) for item in required} == {
        ("workloads", base_address(projected[0]["address"]))
    }
    for path in (
        ROOT / "scripts/credential_authority_provider.py",
        ROOT / "scripts/credential_authority_service.py",
        ROOT / "scripts/secret_migration_guard.py",
    ):
        source = path.read_text()
        assert 'address.startswith("kubernetes_secret_v1.")' not in source


def test_nested_module_plan_is_required_and_local_alias_is_rejected() -> None:
    registry = GUARD.load_registry()
    deployment_contract = json.loads(
        (ROOT / "security/credential-authority-deployment-contract.json").read_text()
    )
    inventory_contract = deployment_contract["inventory"]
    assert inventory_contract["nested_module_credential_addresses"] == (
        "exact-owning-root-and-complete-module-path"
    )
    assert inventory_contract["root-level-or-separate-state-alias_allowed"] is False
    plan = configured_plan(registry, "workloads")
    configured = GUARD.configuration_resource_addresses(plan["configuration"])
    fixed = "module.reference_data.kubernetes_secret_v1.object_storage"
    versioned = (
        "module.reference_data.kubernetes_secret_v1.object_storage_versioned"
    )
    assert {fixed, versioned} <= configured
    assert fixed not in {
        item["address"]
        for item in plan["configuration"]["root_module"]["resources"]
    }
    assert GUARD.enforce_registry_resource_inventory(
        plan, registry=registry, terraform_root="workloads"
    ) == GUARD.registry_resource_addresses(registry, terraform_root="workloads")

    aliased = json.loads(json.dumps(plan))
    nested = aliased["configuration"]["root_module"]["module_calls"][
        "reference_data"
    ]["module"]["resources"]
    nested[0]["address"] = "kubernetes_secret_v1.object_storage"
    with pytest.raises(
        GUARD.GuardError,
        match="configuration module path",
    ):
        GUARD.enforce_registry_resource_inventory(
            aliased, registry=registry, terraform_root="workloads"
        )

    flattened = json.loads(json.dumps(plan))
    nested = flattened["configuration"]["root_module"]["module_calls"].pop(
        "reference_data"
    )["module"]["resources"]
    flattened["configuration"]["root_module"]["resources"].extend(nested)
    with pytest.raises(GUARD.GuardError, match="configuration module path"):
        GUARD.enforce_registry_resource_inventory(
            flattened, registry=registry, terraform_root="workloads"
        )


def test_real_shaped_secret_data_source_is_not_a_managed_credential(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = GUARD.load_registry()
    assert 'data "kubernetes_secret_v1" "database_ca"' in (
        ROOT / "stages/workloads/database.tf"
    ).read_text()
    plan = configured_plan(registry, "workloads")
    data_address = "data.kubernetes_secret_v1.database_ca"
    plan["configuration"]["root_module"]["resources"].append(
        {
            "address": data_address,
            "mode": "data",
            "type": "kubernetes_secret_v1",
            "name": "database_ca",
            "provider_config_key": "kubernetes",
            "expressions": {
                "metadata": {
                    "constant_value": {
                        "name": "database-ca",
                        "namespace": "fs2-system",
                    }
                }
            },
            "schema_version": 0,
        }
    )
    plan["resource_changes"] = [
        {
            "address": data_address,
            "mode": "data",
            "type": "kubernetes_secret_v1",
            "name": "database_ca",
            "provider_name": "registry.terraform.io/hashicorp/kubernetes",
            "change": {
                "actions": ["read"],
                "before": None,
                "after": {"metadata": [{"name": "database-ca"}]},
                "after_unknown": {},
            },
        }
    ]
    assert GUARD.credential_resource_type(data_address) == "kubernetes_secret_v1"
    assert data_address not in GUARD.configuration_resource_addresses(
        plan["configuration"]
    )
    assert GUARD.enforce_registry_resource_inventory(
        plan, registry=registry, terraform_root="workloads"
    ) == GUARD.registry_resource_addresses(registry, terraform_root="workloads")
    monkeypatch.setattr(GUARD, "require_staged_secret_plan", lambda *_a, **_k: None)
    monkeypatch.setattr(
        GUARD, "require_consumer_rollout_binding", lambda *_a, **_k: None
    )
    monkeypatch.setattr(
        GUARD, "require_additive_apply_gate_generation", lambda *_a, **_k: None
    )
    result = GUARD.inspect_plan(
        plan, registry=registry, terraform_root="workloads"
    )
    assert result["protected_changes"] == 0

    forged = json.loads(json.dumps(plan))
    forged["configuration"]["root_module"]["resources"][-1]["mode"] = "managed"
    with pytest.raises(GUARD.GuardError, match="mode differs from its address"):
        GUARD.configuration_resource_addresses(forged["configuration"])

    forged = json.loads(json.dumps(plan))
    forged["resource_changes"][0]["mode"] = "managed"
    with pytest.raises(GUARD.GuardError, match="mode differs from its address"):
        GUARD.inspect_plan(forged, registry=registry, terraform_root="workloads")


def test_greenfield_fixed_v1_create_requires_distinct_empty_bootstrap_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = GUARD.load_registry()
    plan = configured_plan(registry, "workloads")
    fixed = "random_password.bootstrap_access_token_secret"
    plan["resource_changes"] = [
        {
            "address": fixed,
            "mode": "managed",
            "type": "random_password",
            "name": "bootstrap_access_token_secret",
            "change": {"actions": ["create"], "before": None, "after": {}},
        }
    ]
    monkeypatch.setattr(GUARD, "require_staged_secret_plan", lambda *_a, **_k: None)
    monkeypatch.setattr(
        GUARD, "require_consumer_rollout_binding", lambda *_a, **_k: None
    )
    monkeypatch.setattr(
        GUARD, "require_additive_apply_gate_generation", lambda *_a, **_k: None
    )
    with pytest.raises(GUARD.GuardError, match="fixed address create refused"):
        GUARD.inspect_plan(plan, registry=registry, terraform_root="workloads")
    result = GUARD.inspect_plan(
        plan,
        registry=registry,
        terraform_root="workloads",
        greenfield_bootstrap=True,
    )
    assert result["protected_changes"] == 1

    occupied = json.loads(json.dumps(plan))
    occupied["prior_state"]["values"]["root_module"]["resources"] = [
        {
            "address": "terraform_data.preexisting",
            "mode": "managed",
            "type": "terraform_data",
            "name": "preexisting",
            "values": {"id": "already-present"},
        }
    ]
    with pytest.raises(GUARD.GuardError, match="empty managed Terraform prior state"):
        GUARD.inspect_plan(
            occupied,
            registry=registry,
            terraform_root="workloads",
            greenfield_bootstrap=True,
        )


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
        ROOT,
        ROOT / "stages/infrastructure",
        ROOT / "stages/foundation",
        ROOT / "stages/workloads",
        ROOT / "reference-data/terraform",
    ):
        source = (root / "credential_migration_gate.tf").read_text()
        if root == ROOT / "reference-data/terraform":
            assert (
                'resource "terraform_data" "credential_apply_gate_generation"'
                not in source
            )
        else:
            assert (
                'resource "terraform_data" "credential_apply_gate_generation"'
                in source
            )
        assert "prevent_destroy = true" in source
        assert "triggers_replace" not in source
        assert '"/usr/bin/python3"' in source
        assert '"/opt/fs2/k8s-inference/scripts/secret_migration_guard.py"' in source
        assert '${path.module}/../../scripts/secret_migration_guard.py' not in source
        assert '"--registry"' not in source
        expected_history_guards = (
            1 if root == ROOT / "reference-data/terraform" else 2
        )
        assert source.count(
            "!contains(var.credential_migration_gate_history, "
            "var.credential_migration_gate_receipt_sha256)"
        ) == expected_history_guards
    assert 'PRODUCTION_TERRAFORM_COMMAND = "/snap/bin/terraform"' in guard_source
    assert "FS2_TERRAFORM_EXECUTABLE" not in guard_source
    assert "private_temporary_json" not in wrapper_source
    assert "live_secret_inventory(" not in wrapper_source


def test_embedded_reference_data_uses_the_workloads_native_gate() -> None:
    wrapper = (ROOT / "inference-stack").read_text()
    guard = (ROOT / "scripts/secret_migration_guard.py").read_text()
    workloads = (ROOT / "stages/workloads/reference_data.tf").read_text()
    child = (ROOT / "reference-data/terraform/credential_migration_gate.tf").read_text()
    contract = json.loads(
        (ROOT / "security/credential-authority-deployment-contract.json").read_text()
    )["saved_plan_execution"]["embedded_module_gate_owner"]

    assert "REFERENCE_DATA_ROOT" not in wrapper
    assert 'REFERENCE_DATA_ROOT.resolve(): "reference-data"' not in wrapper
    assert "credential_migration_gate_managed_by_parent = true" in workloads
    assert (
        "credential_migration_gate_parent_token       = "
        "terraform_data.credential_migration_gate.id"
    ) in workloads
    for name in (
        "credential_migration_gate_receipt_path",
        "credential_migration_gate_source_commit",
        "credential_migration_gate_receipt_sha256",
        "credential_migration_gate_history",
        "credential_migration_phase",
    ):
        assert f"{name}" in workloads
        assert f"var.{name}" in workloads
    module_source = workloads.split(
        'module "reference_data"', 1
    )[1].split('resource "terraform_data" "reference_data_contract"', 1)[0]
    assert "depends_on = [terraform_data.credential_migration_gate]" not in module_source

    assert "condition     = var.credential_migration_gate_managed_by_parent" in child
    assert '"forwarded-native-gate"' in child
    assert "credential_migration_gate_parent_token" in child
    data_source = child.split(
        'data "external" "credential_migration_gate"', 1
    )[1].split('resource "terraform_data" "credential_migration_gate"', 1)[0]
    assert "credential_migration_gate_parent_token" not in data_source
    marker_source = child.split(
        'resource "terraform_data" "credential_migration_gate"', 1
    )[1]
    assert "parent_gate_token = var.credential_migration_gate_parent_token" in marker_source
    assert 'terraform_root          = "workloads"' in child
    assert "count =" not in child
    assert (
        'resource "terraform_data" "credential_apply_gate_generation"'
        not in child
    )
    assert "apply-saved-plan-gate" not in child
    assert "terraform_configuration = path.root" in child
    assert "path.root != path.module" in child
    assert "terraform_configuration = path.module" not in child
    forwarded_verifier = guard.split(
        "def validate_forwarded_native_gate(", 1
    )[1].split("def validate_native_gate(", 1)[0]
    assert "command_json(" not in forwarded_verifier
    assert "authority_json(" not in forwarded_verifier
    assert "greenfield_bootstrap_identity(" not in forwarded_verifier
    forwarded_cli = guard.split(
        'if args.command == "forwarded-native-gate":', 1
    )[1].split('elif args.command == "native-gate":', 1)[0]
    assert "validate_forwarded_native_gate(" in forwarded_cli
    assert "PRODUCTION_TERRAFORM_COMMAND" not in forwarded_cli
    assert "command_json(" not in forwarded_cli

    assert contract == {
        "module": "module.reference_data",
        "owning_root": "workloads",
        "child_read_only_receipt_verification_enabled": True,
        "child_receipt_verification_root": "workloads",
        "child_receipt_verification_phase": "plan",
        "child_backend_or_provider_read_enabled": False,
        "child_apply_generation_enabled": False,
        "embedded_mode_requires": "exact-registered-workloads-path-root-and-receipt",
        "parent_gate_inputs_forwarded": [
            "credential_migration_gate_parent_token",
            "credential_migration_gate_receipt_path",
            "credential_migration_gate_source_commit",
            "credential_migration_gate_receipt_sha256",
            "credential_migration_gate_history",
            "credential_migration_phase",
        ],
        "parent_gate_token": "terraform_data.credential_migration_gate.id",
        "parent_gate_token_consumers": [
            "module.reference_data.terraform_data.credential_migration_gate",
            "module.reference_data.kubernetes_secret_v1.object_storage",
            "module.reference_data.kubernetes_secret_v1.object_storage_versioned",
        ],
        "state_freshness_owner": "workloads-saved-plan-apply-gate",
        "standalone_supported": False,
        "standalone_rejection": "not-present-in-wrapper-authority-or-guard-root-registries",
    }


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
        ROOT / "model-artifacts/terraform",
    )
    for root in roots:
        source = (root / "versions.tf").read_text()
        assert 'backend "s3" {}' in source
        assert 'backend "local"' not in source
    child = (ROOT / "reference-data/terraform/versions.tf").read_text()
    assert 'backend "' not in child
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
    assert "def authorize_operation_caller" in service
    assert 'process_cgroup(pid) != caller["cgroup_path"]' in service
    assert 'set(callers) != CALLER_PURPOSES' in service
    assert "authority operation-to-purpose map is not exact" in service
    assert "timedelta(hours=24)" in service
    assert "evidence_identity_proof" in provider
    assert "release_identity_proof" in provider
    assert '"credential-evidence-reader"' in provider
    assert '"credential-release-automation"' in provider
    assert '(policy["project_id"], "viewer")' in provider
    assert 'policy["profile"]' not in provider


def test_feature_activation_gen1_and_composite_generations_fail_closed() -> None:
    provider = (ROOT / "scripts/credential_authority_provider.py").read_text()
    guard_source = (ROOT / "scripts/secret_migration_guard.py").read_text()
    assert 'key.partition(":")[0]' in provider
    assert '"uninstantiated_declared_addresses"' in provider
    assert '"availability": "disabled-absent"' in provider
    assert "def credential_presence_sets(" in provider
    assert "optional credential class is only partially enabled" in provider
    assert "enabled credential feature is only partially present" in provider
    assert "disabled credential feature retains managed resources" in provider
    assert "authoritative credential feature marker set is incomplete" in provider
    assert "def legacy_v1_adoption_classes(" in provider
    assert "def legacy_v1_adoption_classes(" in guard_source
    assert "not legacy_adoption and binding.get(\"immutable\") is not True" in provider
    assert "not legacy_adoption and not live_is_immutable" in guard_source
    assert "mutable legacy predecessor cannot be selected current-write" in provider
    assert "Historical fixed-v1 Secrets predate the annotations" in provider
    assert "if missing_declared:" not in provider
    assert generation_from_address('kubernetes_secret_v1.database_versioned["2:owner"]') == 2
    assert generation_from_address('kubernetes_secret_v1.database_versioned["2:consumer"]') == 2
    assert generation_from_address("kubernetes_secret_v1.database") == 1


def test_exact_mutable_legacy_predecessor_is_adopted_without_weakening_successor() -> None:
    registry = GUARD.load_registry()
    content_sha256 = "a" * 64
    observed_at = "2026-09-17T00:00:00Z"

    def state(address: str, *, annotations: dict[str, str], immutable: bool) -> dict:
        return {
            "values": {
                "root_module": {
                    "resources": [
                        {
                            "address": address,
                            "values": {
                                "metadata": [
                                    {
                                        "namespace": "fs2-system",
                                        "name": "fs2-admin-v1",
                                        "uid": "uid-v1",
                                        "resource_version": "41",
                                        "annotations": annotations,
                                    }
                                ],
                                "immutable": immutable,
                            },
                        }
                    ]
                }
            }
        }

    def live(*, annotations: dict[str, str], immutable: bool) -> dict:
        return {
            "items": [
                {
                    "metadata": {
                        "namespace": "fs2-system",
                        "name": "fs2-admin-v1",
                        "uid": "uid-v1",
                        "resourceVersion": "41",
                        "annotations": annotations,
                    },
                    "immutable": immutable,
                    "authorityContentSha256": content_sha256,
                    "authorityEvidenceId": "provider-observation-v1",
                    "authorityObservedAt": observed_at,
                }
            ]
        }

    adopted = GUARD.live_secret_bindings(
        state("kubernetes_secret_v1.admin", annotations={}, immutable=False),
        live(annotations={}, immutable=False),
        registry=registry,
        terraform_root="workloads",
    )
    assert adopted["kubernetes_secret_v1.admin"]["credential_class"] == "admin-token"
    assert adopted["kubernetes_secret_v1.admin"]["generation"] == "1"
    assert adopted["kubernetes_secret_v1.admin"]["immutable"] == "false"

    successor_annotations = {
        "fs2.nebius.ai/credential-class": "admin-token",
        "fs2.nebius.ai/credential-generation": "2",
        "fs2.nebius.ai/content-sha256": content_sha256,
    }
    with pytest.raises(GUARD.GuardError, match="immutable binding differs"):
        GUARD.live_secret_bindings(
            state(
                'kubernetes_secret_v1.admin_versioned["2"]',
                annotations=successor_annotations,
                immutable=False,
            ),
            live(annotations=successor_annotations, immutable=False),
            registry=registry,
            terraform_root="workloads",
        )


def test_presence_policy_is_exhaustive_and_feature_activation_is_authoritative() -> None:
    registry = GUARD.load_registry()
    identifiers = {item["id"] for item in registry["credentials"]}
    presence = registry["credential_presence"]
    assert set(presence["required"]).isdisjoint(presence["optional"])
    assert set(presence["required"]).isdisjoint(presence["feature_gated"])
    assert set(presence["feature_gated"]).isdisjoint(presence["optional"])
    assert (
        set(presence["required"])
        | set(presence["feature_gated"])
        | set(presence["optional"])
        == identifiers
    )
    assert set(presence["required"]) == {
        "operator-handoff",
        "database-logins",
        "pat-bootstrap",
        "admin-token",
        "payload-keyring",
        "ledger-keyring",
        "customer-storage-cipher-keyring",
        "customer-storage-name-keyring",
        "pat-pepper-keyring",
        "route-attestors",
        "grafana-admin",
        "grafana-datasource",
    }
    assert set(presence["feature_gated"]) == {
        "pat-scientific",
        "pat-website",
        "registry-credentials",
        "reference-data-s3",
        "reference-data-s3-secret",
        "scientific-artifact-s3",
        "scientific-artifact-s3-secret",
    }
    assert set(presence["optional"]) == {
        "postgresql-backup-s3",
        "postgresql-backup-s3-secret",
    }
    assert {
        credential_class: set(policy["groups"])
        for credential_class, policy in presence["feature_gated"].items()
    } == {
        "pat-scientific": {"academic-assets"},
        "pat-website": {"academic-assets"},
        "registry-credentials": {
            "ngc-api-key",
            "model-nvcr",
            "dcgm-nvcr",
            "modelexpress-nvcr",
        },
        "reference-data-s3": {"reference-data"},
        "reference-data-s3-secret": {"reference-data"},
        "scientific-artifact-s3": {"scientific-artifacts"},
        "scientific-artifact-s3-secret": {"scientific-artifacts"},
    }
    for policy in presence["optional"].values():
        assert policy["activation"] == "all-authoritative-addresses-observed"
        assert policy["required_addresses"]
    for policy in presence["feature_gated"].values():
        assert policy["activation"] == "authoritative-state-marker-groups"
        assert policy["groups"]
        for group in policy["groups"].values():
            assert group["source"] in registry[
                "credential_activation_resource_addresses"
            ]
            assert {
                (item["root"], item["address"])
                for item in group["required_addresses"]
            } <= {
                (item["root"], item["address"])
                for item in group["managed_addresses"]
            }

    infrastructure = (
        ROOT / "stages/infrastructure/credential_migration_gate.tf"
    ).read_text()
    workloads = (ROOT / "stages/workloads/credential_migration_gate.tf").read_text()
    for source in (infrastructure, workloads):
        assert 'resource "terraform_data" "credential_feature_activation"' in source
        assert "prevent_destroy = true" in source
        assert "credential-feature-activation/v1" in source


def test_feature_gated_presence_requires_exact_authoritative_marker() -> None:
    registry = GUARD.load_registry()
    policies = {item["id"]: item for item in registry["credentials"]}
    declared = registry["terraform_resource_addresses"]

    grouped = {}
    for credential_class in registry["credential_presence"]["required"]:
        policy = policies[credential_class]
        address = next(
            item
            for item in declared
            if item["root"] == policy["terraform_root"]
            and any(
                re.fullmatch(pattern, item["address"])
                for pattern in policy["address_regexes"]
            )
        )
        grouped[(credential_class, 1)] = [
            {
                "terraform_root": address["root"],
                "terraform_address": address["address"],
            }
        ]

    marker_activations = {
        (item["root"], item["address"]): {}
        for item in registry["credential_activation_resource_addresses"]
    }
    for credential_class, policy in registry["credential_presence"][
        "feature_gated"
    ].items():
        for group_name, group in policy["groups"].items():
            source = (group["source"]["root"], group["source"]["address"])
            marker_activations[source].setdefault(credential_class, {})[
                group_name
            ] = False

    def states() -> list[dict]:
        return [
            {
                "root": root,
                "resources": [
                    {
                        "address": address,
                        "credential_activation": {
                            "schema": "fs2-serve.nebius.ai/credential-feature-activation/v1",
                            "activations": activations,
                        },
                    }
                ],
            }
            for (root, address), activations in marker_activations.items()
        ]

    required, enabled, absent_feature, absent_optional = credential_presence_sets(
        registry, grouped, states()
    )
    assert required == frozenset(registry["credential_presence"]["required"])
    assert enabled == required
    assert absent_feature == frozenset(
        registry["credential_presence"]["feature_gated"]
    )
    assert absent_optional == frozenset(registry["credential_presence"]["optional"])

    for credential_class, feature in registry["credential_presence"][
        "feature_gated"
    ].items():
        for group_name, group in feature["groups"].items():
            source = group["source"]
            activation = marker_activations[(source["root"], source["address"])][
                credential_class
            ]
            activation[group_name] = True
            with pytest.raises(ProviderError, match="only partially present"):
                credential_presence_sets(registry, grouped, states())

            candidate = dict(grouped)
            candidate[(credential_class, 1)] = [
                {
                    "terraform_root": item["root"],
                    "terraform_address": item["address"],
                }
                for item in group["required_addresses"]
            ]
            (
                _required,
                enabled,
                absent_feature,
                _absent_optional,
            ) = credential_presence_sets(registry, candidate, states())
            assert credential_class in enabled
            assert credential_class not in absent_feature

            activation[group_name] = False
            with pytest.raises(
                ProviderError, match="disabled credential feature retains"
            ):
                credential_presence_sets(registry, candidate, states())

    with pytest.raises(ProviderError, match="marker set is incomplete"):
        credential_presence_sets(registry, grouped, states()[1:])

    adoptions = registry["legacy_v1_secret_adoptions"]
    assert all("_versioned" not in item["address"] for item in adoptions)
    assert len(adoptions) == len(
        {
            (item["root"], item["address"], item["credential_class"])
            for item in adoptions
        }
    )


def test_expiry_is_future_valid_not_merely_present() -> None:
    rotation = (ROOT / "scripts/credential_rotation.py").read_text()
    assert "provider credential expiry is not future-valid" in rotation
    assert rotation.count("parsed_expiry.astimezone(UTC) <= datetime.now(UTC)") >= 2


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
    assert '"credential_bindings": class_bindings' in guard_source
    assert "reconcile_global_provider_inventory" in provider
    assert "all-cluster-secrets-and-all-project-iam" in provider
    assert registry["provider_inventory_exemptions"] == {
        "kubernetes_secrets": [],
        "nebius_iam": [],
    }
    assert registry["pending_credential_ids"] == []
    assert contracts["pending_contract_ids"] == registry["pending_credential_ids"]


def test_all_credential_contracts_use_current_schema_and_checked_source_trust() -> None:
    contracts = json.loads(
        (ROOT / "security/credential-consumer-contracts.json").read_text()
    )
    guard = (ROOT / "scripts/secret_migration_guard.py").read_text()
    rotation = (ROOT / "scripts/credential_rotation.py").read_text()
    assert contracts["pending_contract_ids"] == []
    assert len(contracts["contracts"]) == 21
    for contract in contracts["contracts"].values():
        assert set(contract) == {
            "adapter",
            "authority",
            "consumers",
            "readiness",
            "required_operations",
        }
        assert {"consumer-readiness", "rotation-readiness"} <= set(
            contract["required_operations"]
        )
    assert guard.count('source_trust=policy["source_trust"]') >= 1
    assert rotation.count('source_trust=policy["source_trust"]') >= 1
    assert '"source_trust": source_trust' in guard
    assert '"credential_bindings": class_bindings' in guard
    assert "consumer_readiness_observation" in rotation


def test_class_adapters_receive_only_exact_class_source_material() -> None:
    provider = (ROOT / "scripts/credential_authority_provider.py").read_text()
    dispatch = provider[
        provider.index("def class_adapter_result(") : provider.index(
            "\ndef backend_custody_result", provider.index("def class_adapter_result(")
        )
    ]
    assert '"credential_sources": class_sources' in dispatch
    assert '"terraform_states": states' not in dispatch
    assert '"kubernetes_secrets": secrets' not in dispatch
    assert '"nebius_inventory": provider_inventory' not in dispatch
    assert 'parameters.get("source_trust") != source_trust' in dispatch
    assert "parameters, expected_bindings, secrets, registry" in dispatch


def test_authorized_reader_files_are_narrow_and_global_inventory_is_root_private() -> None:
    service = (ROOT / "scripts/credential_authority_service.py").read_text()
    provider = (ROOT / "scripts/credential_authority_provider.py").read_text()
    wrapper = (ROOT / "inference-stack").read_text()
    schema = json.loads(
        (ROOT / "security/credential-authority-config.schema.json").read_text()
    )
    assert "caller_identities" in schema["required"]
    assert "operation_callers" in schema["required"]
    policy = schema["$defs"]["policy"]
    for field in (
        "backend_access_identities",
        "backend_access_identity_adapter",
        "kubeconfig_sha256",
        "cluster_inventory_identity",
        "cluster_authorization_adapter",
    ):
        assert field in policy["required"]
    assert "def root_reader_file(" in service
    assert "stat.S_IMODE(metadata.st_mode) != 0o640" in service
    assert 'root_private_file(global_kubeconfig, label="global Secret inventory kubeconfig")' in service
    assert "cluster_inventory_identity_proof" in provider
    assert 'expected_sha256=policy["kubeconfig_sha256"]' in provider
    assert 'metadata.st_gid != reader_gid' in wrapper
    assert 'os.geteuid() != reader_uid' in wrapper


def test_configured_addresses_and_observed_secret_identity_are_mandatory() -> None:
    guard = (ROOT / "scripts/secret_migration_guard.py").read_text()
    provider = (ROOT / "scripts/credential_authority_provider.py").read_text()
    assert "state legitimately omits disabled optional" in guard
    assert '"uninstantiated_declared_addresses"' in provider
    assert "Kubernetes Secrets and " in provider
    assert "Nebius IAM objects lack exact custody" in provider
    assert 'binding.get("immutable") is not True' in provider
    assert 'binding.get("uid") != item["metadata"]["uid"]' in provider
    assert 'live_annotations["class"] not in matching_classes' in provider
    assert "fixed_v1_without_complete_annotations" in provider
    assert 'values.get("immutable") is not True' in guard
    assert "fixed_v1_without_complete_annotations" in guard
    assert "expected_generation = str(generation_from_address(address))" in guard


def test_every_remote_init_requires_verified_additive_state_copy() -> None:
    wrapper = (ROOT / "inference-stack").read_text()
    init = wrapper[
        wrapper.index("def terraform_init(") : wrapper.index(
            "\ndef validate_stage_roots", wrapper.index("def terraform_init(")
        )
    ]
    migration_gate = init.index('"state-migration-readiness"')
    bootstrap_gate = init.index('"greenfield-bootstrap-readiness"')
    terraform_init = init.index('"init",')
    assert migration_gate < terraform_init
    assert bootstrap_gate < terraform_init
    assert 'migration.get("status") != "copy-verified-source-retained"' in init
    assert 'migration.get("source_retained") is not True' in init
    assert 'migration.get("overwrite_performed") is not False' in init
    assert 'migration.get("destination_canonical_state_sha256")' in init


def test_greenfield_bootstrap_is_distinct_provider_attested_and_reobserved() -> None:
    wrapper = (ROOT / "inference-stack").read_text()
    guard = (ROOT / "scripts/secret_migration_guard.py").read_text()
    provider = (ROOT / "scripts/credential_authority_provider.py").read_text()
    service = (ROOT / "scripts/credential_authority_service.py").read_text()
    client = (ROOT / "scripts/credential_provider_adapter.py").read_text()
    anchor = (ROOT / "scripts/credential_external_anchor_client.py").read_text()
    schema = json.loads(
        (ROOT / "security/credential-authority-config.schema.json").read_text()
    )
    contract = json.loads(
        (ROOT / "security/credential-authority-deployment-contract.json").read_text()
    )
    assert "greenfield-bootstrap-readiness" in schema["properties"]["operations"][
        "required"
    ]
    assert "greenfield_bootstrap_adapter" in schema["$defs"]["policy"][
        "required"
    ]
    root_schema = schema["$defs"]["terraform_root"]
    assert "initialization_mode" in root_schema["required"]
    assert set(root_schema["properties"]["initialization_mode"]["enum"]) == {
        "legacy-copy",
        "greenfield-empty",
        "remote-established",
    }
    assert "def greenfield_bootstrap_readiness_result(" in provider
    assert 'root["legacy_state_source"] is not None' in provider
    assert 'response.get("backend_object_present") is not False' in provider
    assert 'response.get("kubernetes_secret_identities") != []' in provider
    assert 'response.get("nebius_credential_identities") != []' in provider
    assert '"greenfield-bootstrap-readiness": {"release-automation"}' in provider
    assert '"greenfield-bootstrap-readiness"' in service
    assert '"greenfield-bootstrap-readiness"' in client
    assert '"greenfield-bootstrap-readiness"' in anchor
    assert "def greenfield_bootstrap_identity(" in guard
    assert 'authority_json(\n        {\n            "operation": "greenfield-bootstrap-readiness"' in guard
    assert "require_empty_greenfield_state" in guard
    assert "greenfield_bootstrap=greenfield_bootstrap" in guard
    assert "if greenfield" in wrapper
    assert "else json.loads(authoritative_state_json" in wrapper
    assert contract["greenfield_bootstrap"] == {
        "initialization_mode": "greenfield-empty",
        "legacy_state_or_adoption_evidence_allowed": False,
        "backend_object_and_versions_must_be_absent": True,
        "backend_lock_must_be_absent": True,
        "provider_attested_all_project_iam_inventory_required": True,
        "provider_attested_all_cluster_secret_inventory_required": True,
        "managed_prior_state_must_be_empty": True,
        "fixed_generation_one_create_requires_this_contract": True,
        "fresh_reobservation_required_at_plan_and_apply": True,
        "post_first_apply_mode": "remote-established",
        "post_first_apply_transition": "provider-observed-plan-and-apply-receipt-bound-lineage-version-state-and-credential-genesis",
        "post_first_apply_identity_receipt": "automatic-provider-observed-write-once-before-policy-promotion",
        "post_apply_crash_recovery": "reconcile-immutable-saved-plan-gate-receipts-with-provider-observed-applied-plan",
        "policy_promotion_required_before_next_root_command": True,
        "execution_authorized": False,
    }


def test_wrapper_uses_registered_roots_bootstrap_mode_and_every_identity_receipt() -> None:
    wrapper = (ROOT / "inference-stack").read_text()
    plan = wrapper[
        wrapper.index("def plan_json(") : wrapper.index(
            "\ndef authoritative_state_json", wrapper.index("def plan_json(")
        )
    ]
    assert "def terraform_root_name(" in wrapper
    assert 'DEPLOY_ROOT.resolve(): "configuration"' in wrapper
    assert "terraform_root=root.name" not in plan
    assert "terraform_root_name(root)" in plan
    assert "durable_identity_receipt(plan_path.parent, root)" in plan
    assert "greenfield_bootstrap=greenfield" in plan
    assert 'arguments.append("--greenfield-bootstrap")' in plan
    assert 'if root_name == "workloads"' in wrapper
    assert 'f"{root_name}-durable-identity.receipt.json"' in wrapper


def test_configuration_root_has_an_explicit_zero_credential_plan_guard() -> None:
    guard = (ROOT / "scripts/secret_migration_guard.py").read_text()
    gate = (ROOT / "credential_migration_gate.tf").read_text()
    main = (ROOT / "main.tf").read_text()
    assert 'if terraform_root == "configuration":' in guard
    assert "def inspect_configuration_plan(" in guard
    assert "require_additive_apply_gate_generation(document)" in guard
    assert "configuration root contains a durable credential resource" in guard
    assert "configuration plan may not move a resource address" in guard
    assert "configuration data source has a mutating action" in guard
    assert '"configuration",\n            "infrastructure"' in guard
    assert 'terraform_root          = "configuration"' in gate
    assert 'resource "terraform_data" "credential_apply_gate_generation"' in gate
    assert "apply-saved-plan-gate" in gate
    assert "depends_on = [terraform_data.credential_migration_gate]" in main


def test_saved_plan_validation_and_apply_use_one_sealed_immutable_snapshot() -> None:
    wrapper = (ROOT / "inference-stack").read_text()
    guard = (ROOT / "scripts/secret_migration_guard.py").read_text()
    contract = json.loads(
        (ROOT / "security/credential-authority-deployment-contract.json").read_text()
    )["saved_plan_execution"]
    apply_source = wrapper[
        wrapper.index("def apply_plan(") : wrapper.index(
            "\ndef workload_endpoint_outputs", wrapper.index("def apply_plan(")
        )
    ]
    assert "os.open(" in apply_source
    assert "O_NOFOLLOW" in apply_source
    assert 'Path(f"/proc/self/fd/{snapshot_descriptor}")' in apply_source
    assert "pass_fds=(snapshot_descriptor,)" in apply_source
    assert "str(runtime_plan_path)" in apply_source
    assert "str(plan_path)" not in apply_source
    assert "sealed_plan_snapshot(" in apply_source
    assert "assert_sealed_plan_descriptor(" in apply_source
    assert apply_source.count("assert_descriptor_stable(") >= 4
    assert "FS2_TERRAFORM_SAVED_PLAN_ORIGINAL_PATH" in wrapper
    assert "os.memfd_create(" in wrapper
    assert "fcntl.F_ADD_SEALS" in wrapper
    for seal in ("F_SEAL_WRITE", "F_SEAL_GROW", "F_SEAL_SHRINK", "F_SEAL_SEAL"):
        assert seal in wrapper
    assert 're.fullmatch(r"/proc/self/fd/([0-9]+)"' in guard
    assert "os.fstat(descriptor)" in guard
    assert "descriptor_sha256(descriptor)" in guard
    assert "require_sealed_saved_plan(saved_plan)" in guard
    assert contract["runtime_object"] == "memfd"
    assert set(contract["required_runtime_seals"]) == {
        "write",
        "grow",
        "shrink",
        "seal",
    }
    assert contract["native_gate_plan_authority"].endswith(
        "not-environment-path"
    )


def test_native_saved_plan_gate_binds_the_actual_terraform_ancestor_plan() -> None:
    guard = (ROOT / "scripts/secret_migration_guard.py").read_text()
    native = guard[
        guard.index("def actual_terraform_apply_plan(") : guard.index(
            "\ndef validate_saved_plan_gate_from_environment(",
            guard.index("def actual_terraform_apply_plan("),
        )
    ]
    validation = guard[
        guard.index("def validate_saved_plan_gate_from_environment(") : guard.index(
            "\ndef load_registry(",
            guard.index("def validate_saved_plan_gate_from_environment("),
        )
    ]
    execution_validation = guard[
        guard.index("def validate_saved_plan_gate(") : guard.index(
            "\ndef command_json(", guard.index("def validate_saved_plan_gate(")
        )
    ]
    assert 'Path(f"/proc/{pid}")' in native
    assert '(process_path / "cmdline").read_bytes()' in native
    assert 'f"-chdir={expected_configuration}"' in native
    assert '"apply",' in native
    assert '"-input=false",' in native
    assert 'process_path / "fd" / str(descriptor_number)' in native
    assert "process_start_time(pid) != started" in native
    assert "require_sealed_saved_plan(" in native
    assert "os.environ" not in native
    assert "actual_terraform_apply_plan(" in validation
    assert 'saved_plan = Path(f"/proc/self/fd/{plan_descriptor}")' in validation
    assert "saved_plan = Path(plan_value)" in validation
    assert "sealed_runtime_snapshot=" in validation
    assert 'plan_sha256 = execution_plan_identity["sha256"]' in execution_validation
    assert 'saved_plan_identity(saved_plan)["sha256"]' not in execution_validation


def test_feature_activation_markers_are_ordered_after_native_gate() -> None:
    for relative in (
        "stages/infrastructure/credential_migration_gate.tf",
        "stages/workloads/credential_migration_gate.tf",
    ):
        source = (ROOT / relative).read_text()
        marker = source[
            source.index('resource "terraform_data" "credential_feature_activation"') :
        ]
        assert "depends_on = [terraform_data.credential_migration_gate]" in marker


def test_greenfield_transition_auto_seals_durable_identity_before_promotion() -> None:
    wrapper = (ROOT / "inference-stack").read_text()
    capture = wrapper[
        wrapper.index("def capture_greenfield_durable_identity(") : wrapper.index(
            "\ndef terraform_root_name(",
            wrapper.index("def capture_greenfield_durable_identity("),
        )
    ]
    initialize = wrapper[
        wrapper.index("def terraform_init(") : wrapper.index(
            "\ndef greenfield_transition_run_root", wrapper.index("def terraform_init(")
        )
    ]
    apply_source = wrapper[
        wrapper.index("def apply_plan(") : wrapper.index(
            "\ndef workload_endpoint_outputs", wrapper.index("def apply_plan(")
        )
    ]
    assert '"capture-state"' in capture
    assert "authoritative_state_json(terraform, root, environment)" in capture
    assert 'raw_state.get("lineage") != expected_transition.get("lineage")' in capture
    assert "canonical_sha256(raw_state)" in capture
    assert "capture_greenfield_durable_identity(" in initialize
    assert "capture_greenfield_durable_identity(" in apply_source
    assert initialize.index("capture_greenfield_durable_identity(") < initialize.index(
        "publish an additive"
    )


def test_backend_sessions_require_exact_provider_permission_closure() -> None:
    wrapper = (ROOT / "inference-stack").read_text()
    provider = (ROOT / "scripts/credential_authority_provider.py").read_text()
    schema = json.loads(
        (ROOT / "security/credential-authority-config.schema.json").read_text()
    )
    assert "backend_authorization_adapter" in schema["$defs"]["policy"]["required"]
    assert "def backend_authorization_closure_proof(" in provider
    assert '"scope") != "project-and-bound-backend-objects"' in provider
    assert 'response.get("direct_grants") != []' in provider
    assert 'response.get("cross_project_grants") != []' in provider
    assert 'response.get("impersonation_grants") != []' in provider
    assert 'response.get("unscoped_actions") != []' in provider
    assert "backend authorization closure is stale" in provider
    assert 'purpose != "release-automation"' in wrapper
    assert '"s3:DeleteObject", "s3:PutObject"' in wrapper


def test_iam_inventory_is_projected_before_access_key_serialization() -> None:
    provider = (ROOT / "scripts/credential_authority_provider.py").read_text()
    inventory = provider[
        provider.index("IAM_PROJECTED_FIELDS =") : provider.index(
            "\ndef authorization_closure_proof", provider.index("IAM_PROJECTED_FIELDS =")
        )
    ]
    assert '"projection_boundary": "provider-before-serialization"' in inventory
    assert 'response.get("secret_fields_observed") != 0' in inventory
    assert 'response.get("data_fields_returned") != 0' in inventory
    assert "Nebius IAM metadata projection is stale" in inventory
    assert '"iam", "access-key", "list"' not in provider
    assert '"status", "secret"' not in inventory


def test_greenfield_transition_is_write_once_recoverable_and_policy_bound() -> None:
    wrapper = (ROOT / "inference-stack").read_text()
    provider = (ROOT / "scripts/credential_authority_provider.py").read_text()
    service = (ROOT / "scripts/credential_authority_service.py").read_text()
    schema = json.loads(
        (ROOT / "security/credential-authority-config.schema.json").read_text()
    )
    assert "greenfield-lineage-transition-readiness" in schema["properties"][
        "operations"
    ]["required"]
    assert "greenfield_transition_adapter" in schema["$defs"]["policy"]["required"]
    root = schema["$defs"]["terraform_root"]
    assert "lineage_origin" in root["required"]
    assert "greenfield_transition" in root["required"]
    assert "def greenfield_lineage_transition_readiness_result(" in provider
    assert 'response.get("mutation_performed") is not False' in provider
    assert 'response.get("pre_apply_object_version_ids") != []' in provider
    assert "def write_once_private_json(" in wrapper
    assert "def recover_greenfield_transition(" in wrapper
    assert "saved-plan-gate-*.receipt.json" in wrapper
    assert "publish an additive root-owned" in wrapper
    assert "valid_greenfield_transition(" in service


def test_external_anchor_accepts_every_purpose_scoped_read_operation() -> None:
    anchor = (ROOT / "scripts/credential_external_anchor_client.py").read_text()
    for operation in (
        "operator-read-context",
        "operator-proxy-context",
        "scoped-credential-context",
        "greenfield-lineage-transition-readiness",
    ):
        assert f'"{operation}"' in anchor


def _frozenset_assignment(path: Path, name: str) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    matches = [
        node.value.args[0]
        for node in tree.body
        if isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Name) and target.id == name
            for target in node.targets
        )
        and isinstance(node.value, ast.Call)
        and isinstance(node.value.func, ast.Name)
        and node.value.func.id == "frozenset"
        and len(node.value.args) == 1
    ]
    assert len(matches) == 1
    value = ast.literal_eval(matches[0])
    assert isinstance(value, set) and all(isinstance(item, str) for item in value)
    return value


def _provider_dispatch_operations() -> set[str]:
    path = ROOT / "scripts/credential_authority_provider.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    functions = [
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "operation_result"
    ]
    assert len(functions) == 1
    handled: set[str] = set()
    for node in ast.walk(functions[0]):
        if not isinstance(node, ast.Compare) or len(node.ops) != 1:
            continue
        if not isinstance(node.left, ast.Name) or node.left.id != "operation":
            continue
        comparator = node.comparators[0]
        if isinstance(node.ops[0], ast.Eq) and isinstance(comparator, ast.Constant):
            if isinstance(comparator.value, str):
                handled.add(comparator.value)
        elif isinstance(node.ops[0], ast.In) and isinstance(comparator, ast.Set):
            values = ast.literal_eval(comparator)
            assert isinstance(values, set)
            handled.update(values)
    return handled


def test_all_authority_operation_registries_and_purpose_maps_are_exact() -> None:
    expected = set(READ_ONLY_OPERATIONS)
    assert set(OPERATION_PURPOSES) == expected
    assert OPERATION_PURPOSES["greenfield-lineage-transition-readiness"] == {
        "release-automation"
    }
    assert _provider_dispatch_operations() == expected
    for source in (
        "credential_authority_service.py",
        "credential_provider_adapter.py",
        "credential_external_anchor_client.py",
    ):
        assert (
            _frozenset_assignment(
                ROOT / "scripts" / source, "READ_ONLY_OPERATIONS"
            )
            == expected
        )
    schema = json.loads(
        (ROOT / "security/credential-authority-config.schema.json").read_text()
    )
    assert set(schema["properties"]["operation_callers"]["required"]) == expected
    assert set(schema["properties"]["operations"]["required"]) == expected
