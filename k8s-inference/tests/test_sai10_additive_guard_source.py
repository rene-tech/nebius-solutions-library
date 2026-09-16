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


def test_registry_normatively_covers_all_52_addresses() -> None:
    registry = GUARD.load_registry()
    assert len(registry["terraform_resource_addresses"]) == 52
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
    assert 'PRODUCTION_TERRAFORM_COMMAND = "/snap/bin/terraform"' in guard_source
    assert "FS2_TERRAFORM_EXECUTABLE" not in guard_source
    assert "private_temporary_json" not in wrapper_source
    assert "live_secret_inventory(" not in wrapper_source


def test_evidence_and_rotation_have_no_disable_revoke_or_delete_transition() -> None:
    evidence = (ROOT / "scripts/append_only_evidence.py").read_text()
    rotation = (ROOT / "scripts/credential_rotation.py").read_text()
    parser = rotation[rotation.index("def parse_args") :]
    assert "os.O_EXCL" in evidence
    assert "previous_sha256" in evidence
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
    tests = (
        ROOT / "components/control-plane/tests/test_keyring_rotation_compatibility.py"
    ).read_text()
    assert "class CustomerStorageCrypto" in crypto
    assert crypto.count("PayloadCipher.customer_storage_aad") >= 3
    assert "customer_storage_crypto = _customer_storage_crypto(settings)" in cli
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
    for ticket in ("SAI-06", "SAI-08", "SAI-09"):
        assert dependencies["dependencies"][ticket]["commit"] is None

    guard_source = (ROOT / "scripts/secret_migration_guard.py").read_text()
    assert 'operation": "artifact-inventory"' in guard_source
    assert "caller-supplied live Secret inventories are forbidden" in guard_source
    assert 'DISPOSITION_ACTIONS = frozenset({"encrypted-rewrap"})' in guard_source
