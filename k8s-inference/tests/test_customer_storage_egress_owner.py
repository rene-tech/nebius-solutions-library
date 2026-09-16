"""Offline adversaries for the signed customer-storage identity inventory."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

SCRIPT = (
    Path(__file__).parents[1]
    / "security/customer-storage-egress-boundary/verify_owner_identity.py"
)
SPEC = importlib.util.spec_from_file_location("customer_storage_egress_owner", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
owner_module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(owner_module)


def inventory() -> dict[str, dict[str, str]]:
    categories = ("owner", "workloads", "release", "human", "break-glass", "other")
    return {
        category: {
            "kubeconfig_path": f"/secure/{category}",
            "kube_context": category,
            "username": f"subject:{category}",
            "category": category,
            "credential_sha256": str(index + 1) * 64,
            "provider_principal_id": f"principal-{category}",
        }
        for index, category in enumerate(categories)
    }


def query() -> dict[str, str]:
    return {
        "security_owner_group": "fs2:customer-storage-egress-security-owner",
        "identity_inventory_json": json.dumps(inventory()),
        "expected_rbac_inventory_sha256": "a" * 64,
        "protected_names_json": json.dumps(
            {
                "namespace": "fs2-system",
                "boundary_policy": "fs2-customer-storage-egress-boundary-g1",
                "contract": "fs2-customer-storage-egress-contract-g1",
                "trust": "fs2-customer-storage-egress-trust-g1",
                "network_policy": "fs2-customer-storage-egress-g1",
                "release_role": "fs2-storage-v2-g1-r1",
            }
        ),
    }


def protected(*, owner: bool) -> dict[str, bool]:
    return {
        f"{verb}:{kind}": owner and verb == "create"
        for verb in ("create", "update", "patch", "delete")
        for kind in (
            "policy",
            "binding",
            "contract",
            "trust",
            "network-policy",
            "release-role",
            "release-binding",
        )
    }


def install_safe_fakes(monkeypatch) -> None:
    expected = inventory()
    monkeypatch.setattr(
        owner_module,
        "_credential_sha256",
        lambda path: expected[path.name]["credential_sha256"],
    )
    monkeypatch.setattr(
        owner_module,
        "_identity",
        lambda _path, context: {
            "username": f"subject:{context}",
            "groups": (
                ["fs2:customer-storage-egress-security-owner"]
                if context == "owner"
                else [f"fs2:{context}"]
            ),
        },
    )
    monkeypatch.setattr(
        owner_module,
        "_protected_permissions",
        lambda _path, context, _names: protected(owner=context == "owner"),
    )
    monkeypatch.setattr(owner_module, "_dangerous_permissions", lambda *_args: [])
    monkeypatch.setattr(
        owner_module,
        "_rbac_inventory_sha256",
        lambda *_args: "a" * 64,
    )


def test_identity_preflight_binds_every_exact_credential(monkeypatch):
    install_safe_fakes(monkeypatch)

    result = owner_module.verify(query())

    assert result["authorized"] == "true"
    assert len(result["security_owner_subject_sha256"]) == 64
    assert len(result["workloads_subject_sha256"]) == 64
    assert len(result["identity_inventory_sha256"]) == 64
    assert result["rbac_inventory_sha256"] == "a" * 64
    assert all("subject:" not in value for value in result.values())


def test_identity_preflight_rejects_omitted_category(monkeypatch):
    install_safe_fakes(monkeypatch)
    value = query()
    declared = json.loads(value["identity_inventory_json"])
    del declared["break-glass"]
    value["identity_inventory_json"] = json.dumps(declared)

    with pytest.raises(ValueError, match="exhaustive"):
        owner_module.verify(value)


def test_identity_preflight_rejects_credential_byte_mismatch(monkeypatch):
    install_safe_fakes(monkeypatch)
    monkeypatch.setattr(owner_module, "_credential_sha256", lambda _path: "f" * 64)

    with pytest.raises(ValueError, match="credential bytes"):
        owner_module.verify(query())


def test_identity_preflight_rejects_secret_exec_csr_or_rbac_authority(monkeypatch):
    install_safe_fakes(monkeypatch)
    monkeypatch.setattr(
        owner_module,
        "_dangerous_permissions",
        lambda _path, context: ["get-secrets"] if context == "human" else [],
    )

    with pytest.raises(ValueError, match="dangerous authority"):
        owner_module.verify(query())


def test_descriptor_open_rejects_symlink_before_kubectl(tmp_path):
    kubeconfig = tmp_path / "kubeconfig"
    kubeconfig.write_text("apiVersion: v1\n", encoding="utf-8")
    kubeconfig.chmod(0o600)

    linked = tmp_path / "linked"
    linked.symlink_to(kubeconfig)
    with pytest.raises(OSError):
        owner_module._open_regular_nofollow(linked)


def test_dangerous_inventory_covers_owner_release_and_cluster_escape_surfaces():
    source = SCRIPT.read_text(encoding="utf-8")
    for permission in (
        "impersonate-users",
        "impersonate-groups",
        "impersonate-serviceaccounts",
        "impersonate-uids",
        "impersonate-userextras",
        "serviceaccount-tokenrequest",
        "bind-clusterroles",
        "escalate-clusterroles",
        "secrets",
        "pods/exec",
        "pods/attach",
        "pods/portforward",
        "pods/ephemeralcontainers",
        "certificatesigningrequests",
        "validatingadmissionpolicies",
        "networkpolicies",
        "configmaps",
    ):
        assert permission in source
    assert "_rbac_inventory_sha256" in source
    assert "live cluster RBAC inventory differs from the signed receipt" in source
