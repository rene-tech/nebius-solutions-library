from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient
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


def _config() -> nim_admission.NimAdmissionConfig:
    return nim_admission.NimAdmissionConfig(
        {
            "schema": nim_admission.CONFIG_SCHEMA,
            "namespace": "fs2-models",
            "security_session_id": "sha256:" + "1" * 64,
            "trusted_attestors": {"sha256:" + "2" * 64: "A" * 43},
            "entries": [
                {
                    "resource_kind": "NIMService",
                    "model_id": "boltz2",
                    "security_envelope": {
                        "subject_sha256": "3" * 64,
                        "subject": {
                            "descendant_image": "registry.example/boltz2@sha256:" + "4" * 64,
                            "pod_spec": {
                                "serviceAccountName": "nim-operator-runtime",
                            },
                            "admission_actors": {
                                "custom_resource": "system:serviceaccount:fs2-system:controller",
                                "descendant_pod": "system:serviceaccount:fs2-models:nim-operator",
                            }
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
        config=_config(), catalog=_Catalog()  # type: ignore[arg-type]
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

    response = TestClient(app).post("/admit", json=review)

    assert response.status_code == 200
    assert response.json()["response"] == {"uid": "review-1", "allowed": True}
    assert called["resource_kind"] == "NIMService"
    assert called["security_envelope"]["subject_sha256"] == "3" * 64


def test_webhook_denies_nim_actor_or_owner_paths_without_an_exact_selector() -> None:
    app = nim_admission.create_nim_admission_app(
        config=_config(), catalog=_Catalog()  # type: ignore[arg-type]
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

    response = TestClient(app).post("/admit", json=review)

    assert response.status_code == 200
    assert response.json()["response"]["allowed"] is False
    assert response.json()["response"]["status"]["code"] == 403


def test_chart_installs_a_fail_closed_nim_webhook_for_crs_pods_and_ephemeral_containers() -> None:
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
    assert "namespaceSelector:" in template
    assert "validate_nim_operator_admission_review(" in source
    assert "await server.serve()" in source
