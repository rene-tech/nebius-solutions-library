"""Deletion-free source regressions for SAI-10 admission boundaries."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.credential_transparency import (
    TransparencyVerificationError,
    leaf_hash,
    node_hash,
    verify_consistency,
    verify_inclusion,
)

ROOT = Path(__file__).resolve().parents[1]


def test_merkle_inclusion_and_prior_head_consistency_are_both_required() -> None:
    first = leaf_hash({"record": 1})
    second = leaf_hash({"record": 2})
    root = node_hash(first, second)
    verify_inclusion(
        leaf=second, leaf_index=1, tree_size=2, proof=[first], root=root
    )
    verify_consistency(
        old_size=1,
        new_size=2,
        old_root=first,
        new_root=root,
        proof=[second],
    )
    with pytest.raises(TransparencyVerificationError):
        verify_consistency(
            old_size=1,
            new_size=1,
            old_root=first,
            new_root=second,
            proof=[],
        )


def test_source_trust_cannot_be_created_by_local_client_policy() -> None:
    trust = json.loads(
        (ROOT / "security/credential-evidence-source-trust.json").read_text()
    )
    client = (ROOT / "scripts/credential_provider_adapter.py").read_text()
    evidence = (ROOT / "scripts/credential_evidence.py").read_text()
    transparency = (ROOT / "scripts/credential_transparency.py").read_text()
    assert trust == {
        "schema": "fs2-serve.nebius.ai/credential-evidence-source-trust/v2",
        "authorization_blocker": "independent anchor and two witness public keys, genesis checkpoint, and production endpoint are not yet accepted",
        "minimum_witnesses": 2,
        "accepted_trust_bundles": [],
    }
    assert '"trusted_checkpoint"' in client
    assert 'anchor.get("endpoint") != trust.get("endpoint")' in transparency
    assert "verify_inclusion" in transparency
    assert "verify_consistency" in transparency
    assert "independent witness quorum is absent" in transparency
    assert "source_trust=policy[\"source_trust\"]" in client
    assert "producer pin differs from source trust" in evidence
    assert "authority policy differs from source trust" in evidence


def test_normal_plan_has_no_legacy_local_state_dependency() -> None:
    wrapper = (ROOT / "inference-stack").read_text()
    start = wrapper.index("def terraform_init(")
    end = wrapper.index("\ndef validate_stage_roots", start)
    init = wrapper[start:end]
    gate_start = wrapper.index("def prepare_credential_migration_gate(")
    gate_end = wrapper.index("\ndef seal_saved_plan_gate", gate_start)
    gate = wrapper[gate_start:gate_end]
    assert "legacy_backend" not in init
    assert "provider-attested encrypted remote backend" in init
    assert '"operation": "backend-custody"' in init
    assert '"state", "pull"' in gate
    assert '"show", "-json", str(state)' not in gate
    assert "state.exists()" not in gate


def test_all_current_and_static_source_go_infrastructure_iam_addresses_are_registered() -> None:
    registry = json.loads(
        (ROOT / "security/durable-credential-registry.json").read_text()
    )
    declared = {
        item["address"]
        for item in registry["provider_managed_resource_addresses"]
        if item["root"] == "infrastructure"
        and item["address"].startswith("nebius_iam_")
    }
    expected: set[str] = set()
    for path in (ROOT / "stages/infrastructure").glob("*.tf"):
        for line in path.read_text().splitlines():
            if not line.startswith('resource "nebius_iam_'):
                continue
            _, terraform_type, name, *_ = line.replace('"', "").split()
            expected.add(f"{terraform_type}.{name}")
    sai06 = {
        "nebius_iam_v2_access_key.postgresql_backup",
        "nebius_iam_v2_access_key.postgresql_backup_inventory",
        "nebius_iam_v2_access_key.postgresql_backup_restore",
        "nebius_iam_v2_access_key.postgresql_restore_receipt",
    }
    assert len(expected) == 25
    assert declared == expected | sai06
    rules = registry["provider_inventory_rules"]["kubernetes_secrets"]
    assert {rule["id"] for rule in rules} == {
        "helm-release-records",
        "bound-service-account-token",
    }


def test_release_identity_is_separate_from_evidence_viewer_and_ambient_profiles() -> None:
    wrapper = (ROOT / "inference-stack").read_text()
    provider = (ROOT / "scripts/credential_authority_provider.py").read_text()
    service = (ROOT / "scripts/credential_authority_service.py").read_text()
    assert '"NEBIUS_PROFILE" in os.environ' in wrapper
    assert '"NEBIUS_CONFIG" in os.environ' in wrapper
    assert "--nebius-profile" not in wrapper
    assert '"operation": "release-identity"' in wrapper
    assert "def evidence_identity_proof" in provider
    assert "def release_identity_proof" in provider
    assert '"credential-evidence-reader"' in provider
    assert '"credential-release-automation"' in provider
    assert '"workload_identity_session"' in provider
    assert "authorization_closure_proof" in provider
    assert 'response.get("impersonation_permits") != []' in provider
    assert '"access_keys", "auth_public_keys"' not in service[
        service.index("release_fields") : service.index("cidrs =", service.index("release_fields"))
    ]
    assert 'release.get("maximum_lifetime_seconds") != 3600' in service


def test_every_active_credential_class_requires_a_pinned_adapter() -> None:
    contracts = json.loads(
        (ROOT / "security/credential-consumer-contracts.json").read_text()
    )
    active = set(contracts["contracts"]) - set(contracts["pending_contract_ids"])
    assert contracts["pending_contract_ids"] == []
    assert len(active) == 21
    service = (ROOT / "scripts/credential_authority_service.py").read_text()
    provider = (ROOT / "scripts/credential_authority_provider.py").read_text()
    assert "set(class_adapters) != contract_ids" in service
    assert "def class_adapter_result" in provider
    assert "exact_requested_secret_bindings" in provider
    assert "requires an accepted class-specific production adapter" not in provider


def test_proxy_uses_existing_provider_bound_viewer_and_never_get_credentials() -> None:
    wrapper = (ROOT / "inference-stack").read_text()
    proxy = wrapper[
        wrapper.index("def internal_proxy_command(") : wrapper.index(
            "\ndef parse_args", wrapper.index("def internal_proxy_command(")
        )
    ]
    assert "ACTIVE_OPERATOR_IDENTITY" in proxy
    assert "ensure_kubeconfig(" not in proxy
    assert "get-credentials" not in proxy
    assert 'authority_observation("operator-proxy-context")' in wrapper
    assert "--terraform" not in wrapper[wrapper.index("def parse_args") :]
    assert "--kubectl" not in wrapper[wrapper.index("def parse_args") :]
    assert "--nebius" not in wrapper[wrapper.index("def parse_args") :]
    verifier = (ROOT / "scripts/operator_handoff_readonly.py").read_text()
    contract = json.loads(
        (ROOT / "security/credential-authority-deployment-contract.json").read_text()
    )
    assert contract["fixed_components"]["operator_handoff_verifier"] == (
        "scripts/operator_handoff_readonly.py"
    )
    for forbidden in (" create ", " delete ", " revoke ", " issue "):
        assert forbidden not in verifier.lower()


def test_inventory_and_backend_require_provider_exact_bindings() -> None:
    provider = (ROOT / "scripts/credential_authority_provider.py").read_text()
    schema = json.loads(
        (ROOT / "security/credential-authority-config.schema.json").read_text()
    )
    assert "helm_release_secret_inventory" in provider
    assert "kubernetes_service_accounts" in provider
    assert 'len(owners) != 1' in provider
    assert 'live_account.get("uid") != service_account_uid' in provider
    assert "state_secret_bindings" in provider
    assert "exemption is expired or overlong" in provider
    root = schema["$defs"]["terraform_root"]
    assert "backend_expectation" in root["required"]
    assert "legacy_state_source" in root["required"]
    backend = schema["$defs"]["backend_expectation"]["required"]
    for field in (
        "bucket_parent_id",
        "bucket_owner_service_account_id",
        "kms_key_parent_id",
        "access_log_prefix",
        "object_lock_mode",
    ):
        assert field in backend


def test_state_migration_is_additive_copy_only_and_source_retaining() -> None:
    provider = (ROOT / "scripts/credential_authority_provider.py").read_text()
    contract = json.loads(
        (ROOT / "security/credential-authority-deployment-contract.json").read_text()
    )
    assert "state_migration_readiness_result" in provider
    assert '"copy_semantics": "create-new-object-version-no-source-mutation"' in provider
    assert 'response.get("source_retained") is not True' in provider
    assert 'response.get("overwrite_performed") is not False' in provider
    assert contract["state_migration"]["execution_authorized"] is False
