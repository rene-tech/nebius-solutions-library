"""Offline adversaries for the separate customer-storage security owner."""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

SCRIPT = (
    Path(__file__).parents[1]
    / "security/customer-storage-egress-boundary/verify_owner_identity.py"
)
SPEC = importlib.util.spec_from_file_location("customer_storage_egress_owner", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
owner_module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(owner_module)


def query() -> dict[str, str]:
    return {
        "security_owner_kubeconfig_path": "/secure/owner",
        "security_owner_kube_context": "owner",
        "workloads_kubeconfig_path": "/secure/workloads",
        "workloads_kube_context": "workloads",
        "security_owner_group": "fs2:customer-storage-egress-security-owner",
    }


def permissions(*, allowed: bool) -> dict[tuple[str, str], bool]:
    return {
        (verb, resource): allowed
        for resource in (
            "validatingadmissionpolicies",
            "validatingadmissionpolicybindings",
        )
        for verb in ("create", "update", "delete")
    }


def test_identity_preflight_proves_external_owner_and_denied_workloads(monkeypatch):
    monkeypatch.setattr(
        owner_module,
        "_identity",
        lambda _path, context: (
            {
                "username": "security-owner",
                "groups": ["fs2:customer-storage-egress-security-owner"],
            }
            if context == "owner"
            else {"username": "workloads", "groups": ["fs2:workloads"]}
        ),
    )
    monkeypatch.setattr(
        owner_module,
        "_admission_permissions",
        lambda _path, context: permissions(allowed=context == "owner"),
    )

    result = owner_module.verify(query())

    assert result["authorized"] == "true"
    assert len(result["security_owner_subject_sha256"]) == 64
    assert len(result["workloads_subject_sha256"]) == 64
    assert "security-owner" not in result.values()
    assert "workloads" not in result.values()


@pytest.mark.parametrize(
    "failure", ["same-subject", "owner-group", "workload-group", "workload-rbac"]
)
def test_identity_preflight_rejects_shared_or_privileged_workloads(
    monkeypatch, failure
):
    def identity(_path, context):
        if context == "owner":
            return {
                "username": "same" if failure == "same-subject" else "security-owner",
                "groups": []
                if failure == "owner-group"
                else ["fs2:customer-storage-egress-security-owner"],
            }
        return {
            "username": "same" if failure == "same-subject" else "workloads",
            "groups": (
                ["fs2:customer-storage-egress-security-owner"]
                if failure == "workload-group"
                else ["fs2:workloads"]
            ),
        }

    monkeypatch.setattr(owner_module, "_identity", identity)
    monkeypatch.setattr(
        owner_module,
        "_admission_permissions",
        lambda _path, context: permissions(
            allowed=context == "owner" or failure == "workload-rbac"
        ),
    )

    with pytest.raises(ValueError):
        owner_module.verify(query())


def test_identity_preflight_uses_descriptor_path_and_rejects_symlinks(
    tmp_path, monkeypatch
):
    kubeconfig = tmp_path / "kubeconfig"
    kubeconfig.write_text("apiVersion: v1\n", encoding="utf-8")
    kubeconfig.chmod(0o600)
    observed: dict[str, object] = {}

    def run(command, **kwargs):
        observed["command"] = command
        observed["pass_fds"] = kwargs["pass_fds"]
        return SimpleNamespace(returncode=0, stdout="no\n")

    monkeypatch.setattr(owner_module.subprocess, "run", run)
    assert (
        owner_module._kubectl(
            kubeconfig, "fixture", "auth", "can-i", "delete", "fixture"
        )
        == "no"
    )
    descriptor = observed["pass_fds"][0]
    assert observed["command"][2] == f"/proc/self/fd/{descriptor}"

    linked = tmp_path / "linked"
    linked.symlink_to(kubeconfig)
    with pytest.raises(OSError):
        owner_module._open_regular_nofollow(linked)

    assert not os.path.islink(kubeconfig)
