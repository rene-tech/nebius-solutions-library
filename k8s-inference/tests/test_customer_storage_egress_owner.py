"""Offline adversaries for the signed customer-storage identity inventory."""

from __future__ import annotations

import copy
import importlib.util
import hashlib
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
from rbac_authority import (  # noqa: E402
    deterministic_groups,
    reject_unapproved_dangerous,
    subject_authority,
)


CONTROLLERS = {
    role: {
        "kind": "ServiceAccount",
        "namespace": "kube-system",
        "name": f"observed-{role}-controller",
        "username": f"system:serviceaccount:kube-system:observed-{role}-controller",
        "uid": f"10000000-0000-4000-8000-{index:012d}",
        "groups": [
            "system:authenticated",
            "system:serviceaccounts",
            "system:serviceaccounts:kube-system",
        ],
        "audit_evidence_sha256": f"{index}" * 64,
    }
    for index, role in enumerate(
        ("deployment", "replicaset", "daemonset", "scheduler"), start=1
    )
}


def system_subject_inventory() -> list[dict[str, object]]:
    return []


def controller_service_accounts() -> list[dict[str, object]]:
    empty_authority = hashlib.sha256(b"[]").hexdigest()
    return [
        {
            "namespace": identity["namespace"],
            "name": identity["name"],
            "uid": identity["uid"],
            "owner": role,
            "groups": identity["groups"],
            "effective_authority_sha256": empty_authority,
            "dangerous_permissions": [],
        }
        for role, identity in sorted(CONTROLLERS.items())
    ]


def inventory() -> dict[str, dict[str, object]]:
    categories = ("owner", "workloads", "release", "human", "break-glass", "other")
    return {
        category: {
            "kubeconfig_path": f"/secure/{category}",
            "kube_context": category,
            "username": f"subject:{category}",
            "groups": (
                ["fs2:customer-storage-egress-security-owner"]
                if category == "owner"
                else [f"fs2:{category}"]
            ),
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
        "service_account_inventory_json": json.dumps(controller_service_accounts()),
        "system_subject_inventory_json": json.dumps(system_subject_inventory()),
        "controller_identities_json": json.dumps(CONTROLLERS),
        "expected_rbac_inventory_sha256": "a" * 64,
        "expected_effective_authority_sha256": hashlib.sha256(b"[]").hexdigest(),
        "protected_names_json": json.dumps(
            {
                "namespace": "fs2-system",
                "boundary_policy": "fs2-customer-storage-egress-boundary-g1",
                "workload_policy": "fs2-customer-storage-egress-workload-g1",
                "contract": "fs2-customer-storage-egress-contract-g1",
                "trust": "fs2-customer-storage-egress-trust-g1",
                "network_policy": "fs2-customer-storage-egress-g1",
                "release_role": "fs2-storage-v2-g1-r1",
                "release_workload": "fs2-storage-v2-g1-r1",
                "release_record": "sh.helm.release.v1.fs2-storage-v2-g1-r1.v1",
            }
        ),
    }


def protected(*, category: str) -> dict[str, bool]:
    values = {
        f"{verb}:{kind}": category == "owner" and verb == "create"
        for verb in ("create", "update", "patch", "delete")
        for kind in (
            "policy",
            "binding",
            "contract",
            "trust",
            "network-policy",
            "release-role",
            "release-binding",
            "release-serviceaccount",
            "release-deployment",
            "release-record",
        )
    }
    if category == "release":
        for permission in (
            "create:release-serviceaccount",
            "create:release-deployment",
            "create:release-record",
            "update:release-record",
            "patch:release-record",
        ):
            values[permission] = True
    return values


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
        lambda _path, context, _names: protected(category=context),
    )
    monkeypatch.setattr(owner_module, "_dangerous_permissions", lambda *_args: [])
    monkeypatch.setattr(
        owner_module,
        "_rbac_inventory",
        lambda *_args: ("a" * 64, [], []),
    )
    monkeypatch.setattr(owner_module, "_namespaces", lambda *_args: ["fs2-system"])


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
        lambda _path, context, _namespaces: ["get-secrets"] if context == "human" else [],
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
    assert "verify_subject_inventory" in source
    assert "release-record" in source


def test_service_account_group_authority_is_semantically_reconciled():
    authority = [
        {
            "subject": {
                "kind": "Group",
                "namespace": "",
                "name": "system:serviceaccounts",
            },
            "scope": "*",
            "binding": {
                "kind": "ClusterRoleBinding",
                "namespace": "",
                "name": "dangerous",
                "uid": "binding-uid",
            },
            "roleRef": {
                "kind": "ClusterRole",
                "namespace": "",
                "name": "dangerous",
            },
            "rules": [
                {
                    "apiGroups": [""],
                    "resources": ["secrets"],
                    "verbs": ["get"],
                }
            ],
        }
    ]
    digest, dangerous = subject_authority(
        authority,
        kind="ServiceAccount",
        namespace="fs2-system",
        name="storage",
        groups=deterministic_groups(
            kind="ServiceAccount", namespace="fs2-system", name="storage"
        ),
    )
    declaration = {
        "namespace": "fs2-system",
        "name": "storage",
        "uid": "10000000-0000-4000-8000-000000000099",
        "owner": "storage",
        "groups": deterministic_groups(
            kind="ServiceAccount", namespace="fs2-system", name="storage"
        ),
        "effective_authority_sha256": digest,
        "dangerous_permissions": dangerous,
    }
    with pytest.raises(ValueError, match="independently derived dangerous"):
        owner_module.verify_subject_inventory([declaration], [], authority)


def test_service_account_group_membership_is_deterministic():
    declaration = {
        "namespace": "fs2-system",
        "name": "storage",
        "uid": "10000000-0000-4000-8000-000000000098",
        "owner": "storage",
        "groups": ["system:authenticated"],
        "effective_authority_sha256": hashlib.sha256(b"[]").hexdigest(),
        "dangerous_permissions": [],
    }
    with pytest.raises(ValueError, match="groups are not deterministic"):
        owner_module.verify_subject_inventory([declaration], [], [])


def test_controller_inventory_accepts_only_canonical_identities_and_authority():
    service_accounts = controller_service_accounts()
    owner_module.verify_subject_inventory(
        service_accounts, [], [], controller_identities=CONTROLLERS
    )

    substituted = copy.deepcopy(CONTROLLERS)
    substituted["scheduler"]["uid"] = "10000000-0000-4000-8000-999999999999"
    with pytest.raises(
        ValueError, match="not bound to its audited live subject"
    ):
        owner_module.verify_subject_inventory(
            service_accounts, [], [], controller_identities=substituted
        )


def test_resource_name_expansion_cannot_hide_extra_configmap_authority():
    authority = [
        {
            "subject": {"kind": "User", "namespace": "", "name": "subject:release"},
            "scope": "fs2-system",
            "binding": {
                "kind": "RoleBinding",
                "namespace": "fs2-system",
                "name": "release",
                "uid": "release-binding-uid",
            },
            "roleRef": {"kind": "Role", "namespace": "fs2-system", "name": "release"},
            "rules": [
                {
                    "apiGroups": [""],
                    "resources": ["configmaps"],
                    "verbs": ["update"],
                    "resourceNames": ["approved", "unapproved"],
                }
            ],
        }
    ]
    _, dangerous = subject_authority(
        authority,
        kind="User",
        namespace="",
        name="subject:release",
        groups=[],
    )
    with pytest.raises(ValueError, match="independently derived dangerous"):
        reject_unapproved_dangerous(
            dangerous,
            explicitly_allowed={
                "fs2-system|core|configmaps|update|names=approved"
            },
        )
