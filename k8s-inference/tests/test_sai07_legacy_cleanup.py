from __future__ import annotations

import hashlib
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
        "baseline_artifact_sha256": "a" * 64,
        "prior_inventory_sha256": "b" * 64,
        "token_fence_observed_at": "2020-01-01T00:00:00Z",
        "service_account_max_token_expiration_seconds": 600,
        "legacy_service_account_token_secrets": [],
        "fence_objects": [
            {
                "api_version": api_version,
                "kind": kind,
                "namespace": namespace,
                "name": name,
                "uid": f"fence-{index}",
                "resource_version": str(index),
                "object_sha256": "d" * 64,
            }
            for index, (api_version, kind, namespace, name) in enumerate(sorted(cleanup.FENCE_IDENTITIES), start=1)
        ],
        "objects": objects,
    }


def item(kind: str, name: str = "legacy", uid: str = "uid-1") -> dict[str, str]:
    api_version = {
        "NetworkPolicy": "networking.k8s.io/v1",
        "ServiceAccount": "v1",
        "DaemonSet": "apps/v1",
    }[kind]
    return {
        "api_version": api_version,
        "kind": kind,
        "namespace": "fs2-models",
        "name": name,
        "uid": uid,
        "resource_version": "17",
        "object_sha256": "c" * 64,
    }


def bind(candidate: dict[str, str], live: dict[str, object]) -> dict[str, str]:
    result = dict(candidate)
    result["resource_version"] = str(live["metadata"]["resourceVersion"])  # type: ignore[index]
    result["object_sha256"] = hashlib.sha256(cleanup.canonical(cleanup.live_projection(live))).hexdigest()
    return result


def test_cleanup_scope_is_exact_and_bounded() -> None:
    _, objects = cleanup.validate_manifest(
        manifest([item("NetworkPolicy"), item("ServiceAccount", "runtime", "uid-2")])
    )
    assert [value["kind"] for value in objects] == ["NetworkPolicy", "ServiceAccount"]
    with pytest.raises(cleanup.CleanupError, match="outside the bounded scope"):
        cleanup.validate_manifest(manifest([{**item("NetworkPolicy"), "namespace": "default"}]))
    with pytest.raises(cleanup.CleanupError, match="at most"):
        cleanup.validate_manifest(
            manifest([item("NetworkPolicy", f"p-{index}", f"uid-{index}") for index in range(129)])
        )


def test_uid_and_controller_labels_are_mandatory() -> None:
    candidate = item("ServiceAccount", "model-a")
    live = {
        "metadata": {
            "name": "model-a",
            "namespace": "fs2-models",
            "uid": "uid-1",
            "resourceVersion": "17",
            "labels": {"app.kubernetes.io/managed-by": "fs2-model-controller"},
        },
        "apiVersion": "v1",
        "kind": "ServiceAccount",
    }
    candidate = bind(candidate, live)
    cleanup.validate_live(candidate, live)
    with pytest.raises(cleanup.CleanupError, match="UID/resourceVersion/spec differs"):
        cleanup.validate_live({**candidate, "uid": "other"}, live)
    live["metadata"]["labels"] = {}
    candidate = bind(candidate, live)
    with pytest.raises(cleanup.CleanupError, match="not a legacy"):
        cleanup.validate_live(candidate, live)


def test_finite_profile_network_policies_are_never_cleanup_candidates() -> None:
    candidate = item("NetworkPolicy", "fs2-network-profile-standard")
    live = {
        "metadata": {
            "name": candidate["name"],
            "namespace": "fs2-models",
            "uid": candidate["uid"],
            "resourceVersion": candidate["resource_version"],
            "labels": {"app.kubernetes.io/part-of": "fs2-serve"},
        },
        "apiVersion": "networking.k8s.io/v1",
        "kind": "NetworkPolicy",
    }
    candidate = bind(candidate, live)
    with pytest.raises(cleanup.CleanupError, match="may never be cleaned"):
        cleanup.validate_live(candidate, live)


def test_read_path_is_namespaced_and_kind_bounded() -> None:
    assert cleanup.path_for("ServiceAccount", "model/a") == "/api/v1/namespaces/fs2-models/serviceaccounts/model%2Fa"
    assert (
        cleanup.path_for("NetworkPolicy", "old")
        == "/apis/networking.k8s.io/v1/namespaces/fs2-models/networkpolicies/old"
    )


def test_cleanup_contract_binds_resource_version_spec_and_result() -> None:
    source = (ROOT / "scripts" / "cleanup_sai07_legacy_resources.py").read_text()
    assert "hashlib.sha256(canonical(live_projection(live))).hexdigest()" in source
    assert "validate_cleanup_fence(client, manifest)" in source
    assert "validate_quarantined_object(client, item, live, retained_daemonsets)" in source
    assert "retained NetworkPolicy grants traffic" in source
    assert "retained DaemonSet still owns Pods" in source
    assert '"delete"' not in source
    assert '"--execute"' not in source
    assert '"retained_objects": checked' in source
    assert '"removed_objects": []' in source
    assert '"result_sha256"' in source
    for controller in (
        "StatefulSet",
        "ReplicaSet",
        "ReplicationController",
        "PodTemplate",
        "CronJob",
        "JobSet",
        "ModelDeployment",
    ):
        assert controller in source


def test_retained_networkpolicy_must_be_deny_only() -> None:
    candidate = item("NetworkPolicy", "legacy-deny")
    safe = {
        "apiVersion": "networking.k8s.io/v1",
        "kind": "NetworkPolicy",
        "metadata": {
            "name": candidate["name"],
            "namespace": "fs2-models",
            "uid": candidate["uid"],
            "resourceVersion": candidate["resource_version"],
            "labels": {"app.kubernetes.io/part-of": "fs2-serve"},
        },
        "spec": {"podSelector": {"matchLabels": {"app": "legacy"}}, "policyTypes": ["Ingress", "Egress"]},
    }
    cleanup.validate_quarantined_object(None, candidate, safe)
    unsafe = {**safe, "spec": {**safe["spec"], "ingress": [{}]}}
    with pytest.raises(cleanup.CleanupError, match="grants traffic"):
        cleanup.validate_quarantined_object(None, candidate, unsafe)


def test_retained_serviceaccount_must_be_tokenless_and_unused() -> None:
    class FakeClient:
        def raw(self, _uri: str, *, allow_absent: bool = False) -> dict[str, object]:
            del allow_absent
            return {"items": []}

    candidate = item("ServiceAccount", "legacy-runtime")
    safe = {
        "apiVersion": "v1",
        "kind": "ServiceAccount",
        "metadata": {"name": "legacy-runtime", "namespace": "fs2-models"},
        "automountServiceAccountToken": False,
    }
    cleanup.validate_quarantined_object(FakeClient(), candidate, safe)
    with pytest.raises(cleanup.CleanupError, match="not tokenless"):
        cleanup.validate_quarantined_object(
            FakeClient(), candidate, {**safe, "automountServiceAccountToken": True}
        )


def test_cleanup_and_collector_use_the_identical_v4_projection() -> None:
    value = {
        "apiVersion": "apps/v1",
        "kind": "DaemonSet",
        "metadata": {
            "name": "legacy",
            "namespace": "fs2-models",
            "uid": "uid-1",
            "resourceVersion": "17",
            "generation": 2,
            "deletionTimestamp": None,
            "labels": {},
            "annotations": {},
            "ownerReferences": [],
        },
        "spec": {"selector": {"matchLabels": {"app": "legacy"}}},
    }
    assert cleanup.live_projection(value)["projectionSchema"] == ("fs2-serve.nebius.ai/sai07-object-projection/v4")
    assert cleanup.live_projection.__module__ == "sai07_inventory_projection"
    collector = (ROOT / "scripts" / "audit_sai07_baseline_inventory.py").read_text()
    assert "from sai07_inventory_projection import ProjectionError, live_projection" in collector


def test_service_account_consumer_scan_includes_podtemplates_and_custom_controllers() -> None:
    class FakeClient:
        def raw(self, uri: str, *, allow_absent: bool = False) -> dict[str, object]:
            del allow_absent
            if uri.endswith("/podtemplates"):
                return {
                    "items": [
                        {
                            "metadata": {"name": "debug-template"},
                            "template": {"spec": {"serviceAccountName": "legacy-runtime"}},
                        }
                    ]
                }
            if uri.endswith("/modeldeployments"):
                return {
                    "items": [
                        {
                            "metadata": {"name": "app-dynamic"},
                            "spec": {
                                "template": {
                                    "spec": {"serviceAccountName": "legacy-runtime"},
                                }
                            },
                        }
                    ]
                }
            return {"items": []}

    assert cleanup.service_account_references(FakeClient(), "legacy-runtime") == [
        "PodTemplate/debug-template",
        "ModelDeployment/app-dynamic",
    ]
    assert cleanup.nested_service_account_names(
        {"items": [{"serviceAccountName": "one"}, {"nested": {"serviceAccountName": "two"}}]}
    ) == {"one", "two"}


def test_live_annotated_service_account_token_secrets_block_closure() -> None:
    class FakeClient:
        def raw(self, uri: str, *, allow_absent: bool = False) -> dict[str, object]:
            del allow_absent
            assert uri.endswith("/secrets")
            return {
                "metadata": {"resourceVersion": "91"},
                "items": [
                    {
                        "type": "kubernetes.io/service-account-token",
                        "metadata": {
                            "name": "legacy-token",
                            "uid": "secret-uid",
                            "resourceVersion": "90",
                            "annotations": {
                                "kubernetes.io/service-account.name": "legacy-runtime",
                            },
                        },
                        "data": {"token": "must-not-enter-result"},
                    }
                ],
            }

    resource_version, secrets = cleanup.legacy_service_account_token_secrets(
        FakeClient(), frozenset({"legacy-runtime"})
    )
    assert resource_version == "91"
    assert secrets == [
        {
            "name": "legacy-token",
            "uid": "secret-uid",
            "resource_version": "90",
            "service_account_name": "legacy-runtime",
        }
    ]
    assert "data" not in secrets[0]
