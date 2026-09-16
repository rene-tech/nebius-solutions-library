from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("cleanup", ROOT / "scripts" / "cleanup_sai07_legacy_resources.py")
assert SPEC and SPEC.loader
cleanup = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(cleanup)


def manifest(objects: list[dict[str, str]]) -> dict[str, object]:
    return {
        "schema": cleanup.SCHEMA,
        "cluster_id": "mk8scluster-test",
        "run_id": "sai07test",
        "kube_system_uid": "kube-system-uid",
        "objects": objects,
    }


def item(kind: str, name: str = "legacy", uid: str = "uid-1") -> dict[str, str]:
    return {"kind": kind, "namespace": "fs2-models", "name": name, "uid": uid}


def test_cleanup_scope_is_exact_and_bounded() -> None:
    _, objects = cleanup.validate_manifest(manifest([item("NetworkPolicy"), item("ServiceAccount", "runtime", "uid-2")]))
    assert [value["kind"] for value in objects] == ["NetworkPolicy", "ServiceAccount"]
    with pytest.raises(cleanup.CleanupError, match="outside the bounded scope"):
        cleanup.validate_manifest(manifest([{**item("NetworkPolicy"), "namespace": "default"}]))
    with pytest.raises(cleanup.CleanupError, match="at most"):
        cleanup.validate_manifest(manifest([item("NetworkPolicy", f"p-{index}", f"uid-{index}") for index in range(129)]))


def test_uid_and_controller_labels_are_mandatory() -> None:
    candidate = item("ServiceAccount", "model-a")
    live = {
        "metadata": {
            "name": "model-a",
            "namespace": "fs2-models",
            "uid": "uid-1",
            "labels": {"app.kubernetes.io/managed-by": "fs2-model-controller"},
        }
    }
    cleanup.validate_live(candidate, live)
    with pytest.raises(cleanup.CleanupError, match="UID differs"):
        cleanup.validate_live({**candidate, "uid": "other"}, live)
    live["metadata"]["labels"] = {}
    with pytest.raises(cleanup.CleanupError, match="not a legacy"):
        cleanup.validate_live(candidate, live)


def test_finite_profile_network_policies_are_never_cleanup_candidates() -> None:
    candidate = item("NetworkPolicy", "fs2-network-profile-standard")
    live = {
        "metadata": {
            "name": candidate["name"],
            "namespace": "fs2-models",
            "uid": candidate["uid"],
            "labels": {"app.kubernetes.io/part-of": "fs2-serve"},
        }
    }
    with pytest.raises(cleanup.CleanupError, match="may never be cleaned"):
        cleanup.validate_live(candidate, live)


def test_delete_path_is_namespaced_and_kind_bounded() -> None:
    assert cleanup.path_for("ServiceAccount", "model/a") == "/api/v1/namespaces/fs2-models/serviceaccounts/model%2Fa"
    assert cleanup.path_for("NetworkPolicy", "old") == "/apis/networking.k8s.io/v1/namespaces/fs2-models/networkpolicies/old"
