from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from fs2_serve_catalog.artifacts import canonical_bytes
from fs2_serve_catalog.loader import CatalogError

from fs2_serve import nim_admission


SOLUTION_ROOT = Path(__file__).resolve().parents[3]


class _NimRecord:
    def to_dict(self) -> dict[str, Any]:
        return {"runtime": {"kind": "nim"}}


class _Catalog:
    def model(self, model_id: str) -> _NimRecord:
        if model_id != "boltz2":
            raise CatalogError("unknown model")
        return _NimRecord()


class _Resolver:
    async def reconcile_root_enrollments(self, *, config, catalog):
        return None

    async def actor_chain(self, user_info, identity):
        return []

    async def owner_chain(self, value, *, root_kind, namespace):
        return []

    async def owner_chain_to_nim(self, value, *, namespace):
        return None

    async def verify_root_enrollment(self, root, *, model_id, subject_sha256, policy):
        return None


def _config() -> nim_admission.NimAdmissionConfig:
    policy = {
        "schema": "fs2-serve.nebius.ai/nim-admission-policy/v5",
        "name": "fs2-serve-control-plane-nim-admission",
        "namespace": "fs2-models",
        "failure_policy": "Fail",
        "match_policy": "Equivalent",
        "side_effects": "None",
        "timeout_seconds": 5,
        "admission_review_versions": ["v1"],
        "operations": ["CREATE", "UPDATE"],
        "resources": [
            "apps.nvidia.com/v1alpha1/nimcaches",
            "apps.nvidia.com/v1alpha1/nimservices",
            "apps/v1/deployments",
            "apps/v1/replicasets",
            "apps/v1/statefulsets",
            "batch/v1/jobs",
            "v1/pods",
            "v1/pods/ephemeralcontainers",
        ],
        "service": {
            "namespace": "fs2-system",
            "name": "fs2-serve-control-plane-nim-admission",
            "path": "/admit",
            "port": 8443,
        },
        "ca_bundle_sha256": "5" * 64,
        "owner_resolution": "live-read-through-exact-uid-chain",
        "root_enrollment": {
            "namespace": "fs2-system",
            "name_prefix": "fs2-nim-root-",
            "storage_kind": "immutable-configmap-create-once",
            "reconciler": "persisted-root-readback",
            "reconcile_interval_seconds": 2,
            "admission_behavior": "verify-existing-deny-until-enrolled",
        },
        "security_boundary": {
            "name": "fs2-platform-security-admission-guard",
            "policy_uid": "22222222-2222-4222-8222-222222222222",
            "policy_resource_version": "11",
            "binding_uid": "33333333-3333-4333-8333-333333333333",
            "binding_resource_version": "12",
            "subject_sha256": "2" * 64,
        },
    }
    policy_sha256 = hashlib.sha256(canonical_bytes(policy)).hexdigest()
    return nim_admission.NimAdmissionConfig(
        {
            "schema": nim_admission.CONFIG_SCHEMA,
            "namespace": "fs2-models",
            "security_session_id": "sha256:" + "1" * 64,
            "trusted_attestors": {"sha256:" + "2" * 64: "A" * 43},
            "admission_policy": policy,
            "admission_policy_sha256": policy_sha256,
            "entries": [
                {
                    "resource_kind": "NIMService",
                    "model_id": "boltz2",
                    "security_envelope": {
                        "subject_sha256": "3" * 64,
                        "subject": {
                            "descendant_image": "registry.example/boltz2@sha256:" + "4" * 64,
                            "admission_policy_sha256": policy_sha256,
                            "pod_spec": {
                                "serviceAccountName": "nim-operator-runtime",
                            },
                            "actor_identities": {
                                "custom_resource": {
                                    "kind": "pod-bound-service-account",
                                    "username": "system:serviceaccount:fs2-system:controller",
                                },
                                "nim_operator": {
                                    "kind": "pod-bound-service-account",
                                    "username": "system:serviceaccount:fs2-models:nim-operator",
                                },
                                "kube_controller_manager": {
                                    "kind": "kubernetes-control-plane",
                                    "username": "system:kube-controller-manager",
                                },
                            },
                            "descendant_resources": {
                                "Deployment": {
                                    "group": "apps",
                                    "version": "v1",
                                    "resource": "deployments",
                                    "actor_identity": "nim_operator",
                                }
                            },
                        },
                    },
                }
            ],
        },
        catalog=_Catalog(),  # type: ignore[arg-type]
    )


def _review(*, resource: dict[str, str], obj: dict[str, Any], actor: str) -> dict[str, Any]:
    return {
        "apiVersion": "admission.k8s.io/v1",
        "kind": "AdmissionReview",
        "request": {
            "uid": "review-1",
            "operation": "CREATE",
            "namespace": "fs2-models",
            "resource": resource,
            "userInfo": {"username": actor},
            "object": obj,
        },
    }


def test_persisted_root_selects_authority_without_descendant_hints() -> None:
    selected = _config().select_persisted_root(
        {
            "apiVersion": "apps.nvidia.com/v1alpha1",
            "kind": "NIMService",
            "metadata": {
                "name": "boltz2",
                "namespace": "fs2-models",
                "uid": "11111111-1111-4111-8111-111111111111",
            },
        },
        catalog=_Catalog(),  # type: ignore[arg-type]
    )

    assert selected[0:2] == ("NIMService", "boltz2")
    assert selected[2]["subject_sha256"] == "3" * 64


def test_webhook_calls_the_catalog_validator_for_a_configured_nim_resource(monkeypatch) -> None:
    called: dict[str, Any] = {}

    def validate(review, **kwargs):
        called.update(kwargs)
        return {
            "apiVersion": "admission.k8s.io/v1",
            "kind": "AdmissionReview",
            "response": {"uid": review["request"]["uid"], "allowed": True},
        }

    monkeypatch.setattr(nim_admission, "validate_nim_operator_admission_review", validate)
    app = nim_admission.create_nim_admission_app(
        config=_config(), catalog=_Catalog(), resolver=_Resolver()  # type: ignore[arg-type]
    )
    review = _review(
        resource={"group": "apps.nvidia.com", "version": "v1alpha1", "resource": "nimservices"},
        actor="system:serviceaccount:fs2-system:controller",
        obj={
            "apiVersion": "apps.nvidia.com/v1alpha1",
            "kind": "NIMService",
            "metadata": {
                "name": "boltz2",
                "namespace": "fs2-models",
                "annotations": {
                    nim_admission.ENVELOPE_ANNOTATION: "3" * 64,
                },
            },
        },
    )

    with TestClient(app) as client:
        response = client.post("/admit", json=review)

    assert response.status_code == 200
    assert response.json()["response"] == {"uid": "review-1", "allowed": True}
    assert called["resource_kind"] == "NIMService"
    assert called["security_envelope"]["subject_sha256"] == "3" * 64


def test_webhook_denies_nim_actor_or_owner_paths_without_an_exact_selector() -> None:
    app = nim_admission.create_nim_admission_app(
        config=_config(), catalog=_Catalog(), resolver=_Resolver()  # type: ignore[arg-type]
    )
    review = _review(
        resource={"group": "", "version": "v1", "resource": "pods"},
        actor="system:serviceaccount:fs2-models:nim-operator",
        obj={
            "apiVersion": "v1",
            "kind": "Pod",
            "metadata": {
                "name": "unbound-descendant",
                "namespace": "fs2-models",
                "ownerReferences": [],
            },
        },
    )

    with TestClient(app) as client:
        response = client.post("/admit", json=review)

    assert response.status_code == 200
    assert response.json()["response"]["allowed"] is False
    assert response.json()["response"]["status"]["code"] == 403


def test_chart_installs_a_mandatory_fail_closed_nim_webhook_for_real_controller_chains() -> None:
    template = (
        SOLUTION_ROOT
        / "charts/control-plane/fs2-serve-control-plane/templates/nim-admission.yaml"
    ).read_text(encoding="utf-8")
    source = (
        SOLUTION_ROOT / "components/control-plane/src/fs2_serve/nim_admission.py"
    ).read_text(encoding="utf-8")

    assert "kind: ValidatingWebhookConfiguration" in template
    assert "failurePolicy: Fail" in template
    assert 'resources: ["nimcaches", "nimservices"]' in template
    assert 'resources: ["pods", "pods/ephemeralcontainers"]' in template
    assert 'resources: ["deployments", "replicasets", "statefulsets"]' in template
    assert 'resources: ["jobs"]' in template
    assert "defaultMode: 0440" in template
    assert "fsGroup: 65532" in template
    assert 'serviceAccountName: {{ include "fs2-serve.fullname" . }}-nim-admission' in template
    assert 'verbs: ["create", "get"]' in template
    assert 'verbs: ["get", "list"]' in template
    assert '"kind": "ConfigMap"' in source
    assert "await resolver.enroll_root(" in source
    assert "await resolver.verify_root_enrollment(" in source
    admission_handler = source.split('@app.post("/admit"', 1)[1].split(
        "    return app", 1
    )[0]
    assert "resolver.enroll_root(" not in admission_handler
    assert "resolver.verify_root_enrollment(" in admission_handler
    assert 'httpGet: {path: /readyz, port: https, scheme: HTTPS}' in template
    assert "policyTypes: [Ingress, Egress]" in template
    assert ".Values.networkPolicy.dns.namespaceLabels" in template
    assert ".Values.networkPolicy.kubernetesApiCidrs" in template
    assert 'fail "nimAdmission is mandatory for this chart and cannot be disabled"' in template
    assert "namespaceSelector:" in template
    assert "validate_nim_operator_admission_review(" in source
    assert "await resolver.verify_admission_policy(config.admission_policy)" in source
    assert "await resolver.owner_chain(" in source
    assert source.index("await resolver.owner_chain_to_nim(") < source.index(
        "selected = config.select(review"
    )
    assert "await resolver.actor_chain(" in source
    assert "await server.serve()" in source


def test_root_uid_enrollment_is_create_once_and_rejects_a_recreated_root(tmp_path: Path) -> None:
    token_file = tmp_path / "token"
    token_file.write_text("signed-pod-bound-token", encoding="utf-8")
    subject_sha256 = "3" * 64
    expected_name = (
        f"fs2-nim-root-{subject_sha256[:12]}-11111111-1111-4111-8111-111111111111"
    )
    expected_data = {
        "schema": "fs2-serve.nebius.ai/nim-root-enrollment/v1",
        "resource_kind": "NIMService",
        "model_id": "boltz2",
        "root_uid": "11111111-1111-4111-8111-111111111111",
        "subject_sha256": subject_sha256,
    }
    calls: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path))
        if request.method == "POST":
            return httpx.Response(409, json={"kind": "Status", "reason": "AlreadyExists"})
        return httpx.Response(
            200,
            json={
                "apiVersion": "v1",
                "kind": "ConfigMap",
                "metadata": {
                    "name": expected_name,
                    "namespace": "fs2-system",
                    "labels": {nim_admission.ROOT_ENROLLMENT_LABEL: "true"},
                },
                "immutable": True,
                "data": {**expected_data, "root_uid": "22222222-2222-4222-8222-222222222222"},
            },
        )

    client = httpx.AsyncClient(
        base_url="https://kubernetes.invalid",
        transport=httpx.MockTransport(handler),
    )
    resolver = nim_admission.KubernetesNimOwnerResolver(
        base_url="https://kubernetes.invalid",
        token_file=token_file,
        ca_file=tmp_path / "unused-ca",
        client=client,
    )
    root = {
        "apiVersion": "apps.nvidia.com/v1alpha1",
        "kind": "NIMService",
        "metadata": {
            "name": "boltz2",
            "namespace": "fs2-models",
            "uid": expected_data["root_uid"],
        },
    }

    async def exercise() -> None:
        try:
            with pytest.raises(CatalogError, match="persisted NIM root enrollment differs"):
                await resolver.enroll_root(
                    root,
                    model_id="boltz2",
                    subject_sha256=subject_sha256,
                    policy=_config().admission_policy,
                )
        finally:
            await client.aclose()

    asyncio.run(exercise())

    assert calls == [
        ("POST", "/api/v1/namespaces/fs2-system/configmaps"),
        ("GET", f"/api/v1/namespaces/fs2-system/configmaps/{expected_name}"),
    ]
