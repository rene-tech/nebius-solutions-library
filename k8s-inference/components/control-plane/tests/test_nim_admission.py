from __future__ import annotations

import asyncio
import hashlib
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
from fastapi.testclient import TestClient
from fs2_serve_catalog.artifacts import canonical_bytes
from fs2_serve_catalog.attestations import (
    create_signed_attestation,
    public_key_id,
    public_key_value,
)
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
    private_key = Ed25519PrivateKey.from_private_bytes(b"\x19" * 32)
    key_id = public_key_id(private_key.public_key())
    session_id = "sha256:" + "1" * 64
    boundary_sha256 = "2" * 64
    evidence_sha256 = "6" * 64
    issued = datetime.now(timezone.utc).replace(microsecond=0)
    attestation = create_signed_attestation(
        private_key=private_key,
        session_id=session_id,
        nonce="sha256:" + "7" * 64,
        issued_at=issued.isoformat().replace("+00:00", "Z"),
        expires_at=(issued + timedelta(hours=1)).isoformat().replace("+00:00", "Z"),
        kind="platform-admission-boundary",
        subject_schema=nim_admission.BOUNDARY_SCHEMA,
        subject_digest="sha256:" + boundary_sha256,
        model_id="platform",
        claims={
            "authorization_id": "platform-security-boundary-review",
            "decision": "accepted",
            "reviewer_role": "independent-platform-security",
            "evidence_sha256": evidence_sha256,
        },
    )
    policy = {
        "schema": nim_admission.POLICY_SCHEMA,
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
        "tls": {
            "secret_name": "fs2-nim-admission-tls-" + "9" * 16,
            "secret_uid": "99999999-9999-4999-8999-999999999999",
            "secret_resource_version": "13",
            "secret_type": "kubernetes.io/tls",
            "generation_sha256": "9" * 64,
            "certificate_sha256": "a" * 64,
            "public_key_spki_sha256": "b" * 64,
            "ca_bundle_sha256": "5" * 64,
            "service_dns_name": "fs2-serve-control-plane-nim-admission.fs2-system.svc",
        },
        "owner_resolution": "live-read-through-exact-uid-chain",
        "owner_lookup_namespaces": ["fs2-models", "fs2-system"],
        "network_policy": {
            "webhook_source_cidrs": ["10.10.0.0/24"],
            "kubernetes_api_cidrs": ["10.20.0.1/32"],
        },
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
            "subject_sha256": boundary_sha256,
            "authorization_id": "platform-security-boundary-review",
            "cluster_uid": "44444444-4444-4444-8444-444444444444",
            "provider_authorization_id": "provider-admission-custody-review",
            "provider_authorization_sha256": "",
            "owner_lookup_namespaces": ["fs2-models", "fs2-system"],
        },
    }
    permissions = [
        {
            "authorization_source_id": "provider-policy/security-owner",
            "principal": "fs2-platform-security-external-automation",
            "grant_principal": "fs2-platform-security-external-automation",
            "grant_principal_kind": "user",
            "inherited_groups": [],
            "scope": "namespace:fs2-system",
            "api_group": "",
            "resource": "configmaps",
            "subresource": "",
            "verb": "get",
            "resource_names": [],
        }
    ]
    provider_subject = {
        "schema": nim_admission.PROVIDER_CUSTODY_SCHEMA,
        "cluster_uid": policy["security_boundary"]["cluster_uid"],
        "collector": {
            "method": "provider-iam-live-enumeration/v1",
            "identity": "platform-security-collector",
            "observed_at": issued.isoformat().replace("+00:00", "Z"),
            "adapter_sha256": "c" * 64,
            "tool_digest": "sha256:" + "d" * 64,
            "resource_ids": ["cluster/fixture"],
            "complete": True,
            "page_count": 1,
            "terminal_page_sha256": "e" * 64,
        },
        "security_principal": "fs2-platform-security-external-automation",
        "security_principal_kind": "external-automation-user",
        "security_principal_groups": ["platform-security-automation"],
        "security_credential_targets": [
            {
                "api_group": "provider.iam.test",
                "resource": "automationcredentials",
                "subresource": "",
                "scope": "cluster",
                "name": "fs2-platform-security-external-automation",
            }
        ],
        "workload_release_principal": "system:serviceaccount:fs2-system:fs2-release-automation",
        "security_principal_is_distinct": True,
        "denied_verbs": ["create", "delete", "deletecollection", "patch", "update"],
        "denied_indirect_verbs": ["bind", "escalate", "impersonate"],
        "owner_lookup_namespaces": ["fs2-models", "fs2-system"],
        "webhook_source_cidrs": ["10.10.0.0/24"],
        "protected_objects": nim_admission._protected_security_objects(policy),
        "effective_permissions": permissions,
        "effective_permissions_sha256": hashlib.sha256(canonical_bytes(permissions)).hexdigest(),
    }
    provider_sha256 = hashlib.sha256(canonical_bytes(provider_subject)).hexdigest()
    policy["security_boundary"]["provider_authorization_sha256"] = provider_sha256
    provider_evidence_sha256 = "f" * 64
    provider_attestation = create_signed_attestation(
        private_key=private_key,
        session_id=session_id,
        nonce="sha256:" + "0" * 64,
        issued_at=issued.isoformat().replace("+00:00", "Z"),
        expires_at=(issued + timedelta(hours=1)).isoformat().replace("+00:00", "Z"),
        kind="provider-admission-custody",
        subject_schema=nim_admission.PROVIDER_CUSTODY_SCHEMA,
        subject_digest="sha256:" + provider_sha256,
        model_id="platform",
        claims={
            "authorization_id": "provider-admission-custody-review",
            "decision": "accepted",
            "reviewer_role": "independent-platform-security",
            "evidence_sha256": provider_evidence_sha256,
        },
    )
    policy_sha256 = hashlib.sha256(canonical_bytes(policy)).hexdigest()
    return nim_admission.NimAdmissionConfig(
        {
            "schema": nim_admission.CONFIG_SCHEMA,
            "namespace": "fs2-models",
            "security_session_id": session_id,
            "trusted_attestors": {key_id: public_key_value(private_key.public_key())},
            "admission_policy": policy,
            "admission_policy_sha256": policy_sha256,
            "security_boundary_envelope": {
                "subject_sha256": boundary_sha256,
                "evidence_sha256": evidence_sha256,
                "attestation": attestation,
                "attestation_sha256": hashlib.sha256(canonical_bytes(attestation)).hexdigest(),
                "provider_custody": {
                    "subject": provider_subject,
                    "subject_sha256": provider_sha256,
                    "evidence_sha256": provider_evidence_sha256,
                    "attestation": provider_attestation,
                    "attestation_sha256": hashlib.sha256(
                        canonical_bytes(provider_attestation)
                    ).hexdigest(),
                },
            },
            "non_nim_controller_exemptions": [],
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
    workload_template = (
        SOLUTION_ROOT
        / "charts/control-plane/fs2-serve-control-plane/templates/nim-admission.yaml"
    ).read_text(encoding="utf-8")
    template = (
        SOLUTION_ROOT
        / "charts/security/fs2-nim-admission-security/templates/backend.yaml"
    ).read_text(encoding="utf-8")
    boundary_template = (
        SOLUTION_ROOT
        / "charts/security/fs2-platform-security-boundary/templates/guard.yaml"
    ).read_text(encoding="utf-8")
    receipt_template = (
        SOLUTION_ROOT
        / "charts/security/fs2-nim-admission-installation-receipt/templates/receipt.yaml"
    ).read_text(encoding="utf-8")
    source = (
        SOLUTION_ROOT / "components/control-plane/src/fs2_serve/nim_admission.py"
    ).read_text(encoding="utf-8")

    assert "kind: ValidatingWebhookConfiguration" in template
    assert 'mode "preserve-legacy-selector"' in template
    assert "app.kubernetes.io/instance: {{ .Values.ownership.legacyReleaseName }}" in template
    assert "helm.sh/resource-policy: keep" in template
    assert "failurePolicy: Fail" in template
    assert 'resources: ["nimcaches", "nimservices"]' in template
    assert 'resources: ["pods", "pods/ephemeralcontainers"]' in template
    assert 'resources: ["deployments", "replicasets", "statefulsets"]' in template
    assert 'resources: ["jobs"]' in template
    assert 'resources: ["deployments", "daemonsets", "replicasets", "statefulsets"]' in template
    assert 'resources: ["cronjobs", "jobs"]' in template
    assert 'resources: ["jobsets"]' in template
    assert 'ne .Values.securityCustody "external-platform-security"' in template
    assert "defaultMode: 0440" in template
    assert "fsGroup: 65532" in template
    assert "serviceAccountName: fs2-serve-control-plane-nim-admission" in template
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
    assert 'fail "the workload control-plane release must not render the Platform Security-owned NIM admission backend"' in workload_template
    assert "manageSecurityBackend" in workload_template
    assert 'ownershipHandoffMode "legacy-prepare"' in workload_template
    assert "helm.sh/resource-policy: keep" in workload_template
    assert "namespaceSelector:" in template
    assert "validate_nim_operator_admission_review(" in source
    assert "await resolver.verify_admission_policy(config)" in source
    assert "await resolver.owner_chain(" in source
    assert source.index("await resolver.owner_chain_to_nim(") < source.index(
        "selected = config.select(review"
    )
    assert "await resolver.actor_chain(" in source
    assert '"apps/v1", "DaemonSet"' in source
    assert '"batch/v1", "CronJob"' in source
    assert '"jobset.x-k8s.io/v1alpha2", "JobSet"' in source
    assert "live_boundary_subject" in source
    assert "config.verify_security_boundary_authorization(" in source
    assert "renewed_envelope=renewed_envelope" in source
    assert "verify_mounted_tls_identity(" in source
    assert "PolicyBuilder()" in source
    assert "await server.serve()" in source
    assert "admission.json | nindent 4 }}\n---\napiVersion: v1\nkind: ConfigMap" in template
    assert "FS2_NIM_ADMISSION_TLS_CA_FILE" in template
    assert "privateKeyBase64" not in template
    assert "certificateBase64" not in template
    assert "kind: Secret" not in template
    assert "kind: ValidatingAdmissionPolicy" in boundary_template
    assert "kind: ValidatingAdmissionPolicyBinding" in boundary_template
    assert "object != null" in boundary_template
    assert "oldObject != null" in boundary_template
    assert "object.spec.serviceAccountName == 'fs2-serve-control-plane-nim-admission'" in boundary_template
    assert "oldObject.spec.serviceAccountName == 'fs2-serve-control-plane-nim-admission'" in boundary_template
    assert ".Values.derivedObjects.replicaSets" in boundary_template
    assert "ownerReferences[0].apiVersion == 'apps/v1'" in boundary_template
    assert "ownerReferences[0].apiVersion == 'v1'" in boundary_template
    assert "ownerReferences[0].controller == true" in boundary_template
    assert "providerHeadBootstrap.generation" in boundary_template
    assert "head-generation'] == '1'" not in boundary_template
    assert "fs2-serve-control-plane-nim-owner-reader" in boundary_template
    assert "kind: ConfigMap" in receipt_template
    assert "fs2-nim-installation-" in receipt_template
    assert "_installation_receipt_from_reference" in source
    assert "expected_handoff_sha256=security_handoff_sha256" in source


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


@pytest.mark.parametrize(
    ("child_kind", "owner_api_version", "owner_kind", "owner_resource"),
    [
        ("Pod", "apps/v1", "DaemonSet", "daemonsets"),
        ("Job", "batch/v1", "CronJob", "cronjobs"),
        ("Job", "jobset.x-k8s.io/v1alpha2", "JobSet", "jobsets"),
    ],
)
def test_non_nim_controller_inventory_is_classified_from_live_uid_edges(
    tmp_path: Path,
    child_kind: str,
    owner_api_version: str,
    owner_kind: str,
    owner_resource: str,
) -> None:
    token_file = tmp_path / "token"
    token_file.write_text("signed-pod-bound-token", encoding="utf-8")
    owner_uid = "55555555-5555-4555-8555-555555555555"

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == (
            f"/apis/{owner_api_version}/namespaces/fs2-models/{owner_resource}/ordinary-owner"
        )
        return httpx.Response(
            200,
            json={
                "apiVersion": owner_api_version,
                "kind": owner_kind,
                "metadata": {
                    "name": "ordinary-owner",
                    "namespace": "fs2-models",
                    "uid": owner_uid,
                },
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
    child = {
        "apiVersion": "v1" if child_kind == "Pod" else "batch/v1",
        "kind": child_kind,
        "metadata": {
            "name": "ordinary-child",
            "namespace": "fs2-models",
            "ownerReferences": [
                {
                    "apiVersion": owner_api_version,
                    "kind": owner_kind,
                    "name": "ordinary-owner",
                    "uid": owner_uid,
                    "controller": True,
                    "blockOwnerDeletion": True,
                }
            ],
        },
    }

    async def exercise() -> None:
        try:
            chain = await resolver.owner_chain_to_nim(child, namespace="fs2-models")
            assert chain[-1]["kind"] == owner_kind
            assert chain[-1]["metadata"]["uid"] == owner_uid
        finally:
            await client.aclose()

    asyncio.run(exercise())


def test_unknown_controller_kind_is_not_an_omission_allowlist(tmp_path: Path) -> None:
    token_file = tmp_path / "token"
    token_file.write_text("signed-pod-bound-token", encoding="utf-8")
    client = httpx.AsyncClient(
        base_url="https://kubernetes.invalid",
        transport=httpx.MockTransport(
            lambda request: pytest.fail(f"unexpected request: {request.url}")
        ),
    )
    resolver = nim_admission.KubernetesNimOwnerResolver(
        base_url="https://kubernetes.invalid",
        token_file=token_file,
        ca_file=tmp_path / "unused-ca",
        client=client,
    )
    child = {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {
            "name": "unknown-owner-child",
            "namespace": "fs2-models",
            "ownerReferences": [
                {
                    "apiVersion": "example.invalid/v1",
                    "kind": "UnknownController",
                    "name": "unknown-owner",
                    "uid": "66666666-6666-4666-8666-666666666666",
                    "controller": True,
                    "blockOwnerDeletion": True,
                }
            ],
        },
    }

    async def exercise() -> None:
        try:
            with pytest.raises(CatalogError, match="unsupported GVK"):
                await resolver.owner_chain_to_nim(child, namespace="fs2-models")
        finally:
            await client.aclose()

    asyncio.run(exercise())


def test_mounted_tls_identity_requires_signed_generation_chain_san_and_key(
    tmp_path: Path,
) -> None:
    now = datetime.now(timezone.utc).replace(microsecond=0)
    ca_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "FS2 admission test CA")])
    ca = (
        x509.CertificateBuilder()
        .subject_name(ca_name)
        .issuer_name(ca_name)
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(hours=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=True,
                crl_sign=True,
                encipher_only=None,
                decipher_only=None,
            ),
            critical=True,
        )
        .sign(ca_key, hashes.SHA256())
    )
    leaf_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    service_dns = "fs2-serve-control-plane-nim-admission.fs2-system.svc"
    leaf = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, service_dns)]))
        .issuer_name(ca.subject)
        .public_key(leaf_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(minutes=30))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.SubjectAlternativeName([x509.DNSName(service_dns)]), critical=True)
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=True,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=False,
                crl_sign=False,
                encipher_only=None,
                decipher_only=None,
            ),
            critical=True,
        )
        .sign(ca_key, hashes.SHA256())
    )
    certificate_file = tmp_path / "tls.crt"
    key_file = tmp_path / "tls.key"
    ca_file = tmp_path / "ca.crt"
    certificate_file.write_bytes(leaf.public_bytes(serialization.Encoding.PEM))
    key_file.write_bytes(
        leaf_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    ca_bytes = ca.public_bytes(serialization.Encoding.PEM)
    ca_file.write_bytes(ca_bytes)
    certificate_sha256 = hashlib.sha256(
        leaf.public_bytes(serialization.Encoding.DER)
    ).hexdigest()
    spki_sha256 = hashlib.sha256(
        leaf.public_key().public_bytes(
            serialization.Encoding.DER,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    ).hexdigest()
    ca_sha256 = hashlib.sha256(ca_bytes).hexdigest()
    generation = nim_admission._tls_generation_sha256(
        certificate=certificate_sha256,
        spki=spki_sha256,
        ca=ca_sha256,
    )
    policy = dict(_config().admission_policy)
    policy["tls"] = {
        "secret_name": f"fs2-nim-admission-tls-{generation[:16]}",
        "secret_uid": "99999999-9999-4999-8999-999999999999",
        "secret_resource_version": "13",
        "secret_type": "kubernetes.io/tls",
        "generation_sha256": generation,
        "certificate_sha256": certificate_sha256,
        "public_key_spki_sha256": spki_sha256,
        "ca_bundle_sha256": ca_sha256,
        "service_dns_name": service_dns,
    }

    nim_admission.verify_mounted_tls_identity(
        policy=policy,
        certificate_file=certificate_file,
        private_key_file=key_file,
        ca_file=ca_file,
    )
    policy["tls"] = {**policy["tls"], "service_dns_name": "wrong.fs2-system.svc"}
    with pytest.raises(CatalogError, match="mounted TLS identity differs"):
        nim_admission.verify_mounted_tls_identity(
            policy=policy,
            certificate_file=certificate_file,
            private_key_file=key_file,
            ca_file=ca_file,
        )


def test_provider_permission_intersection_preserves_unrelated_release_writes() -> None:
    protected = {
        "api_group": "apps",
        "resource": "deployments",
        "scope": "namespace:fs2-system",
        "names": ["fs2-serve-control-plane-nim-admission"],
        "name_prefixes": [],
    }
    unrelated = {
        "api_group": "apps",
        "resource": "deployments",
        "scope": "namespace:fs2-models",
        "verb": "update",
        "resource_names": [],
    }
    exact = {
        **unrelated,
        "scope": "namespace:fs2-system",
        "resource_names": ["fs2-serve-control-plane-nim-admission"],
    }

    assert not nim_admission._permission_reaches_protected_object(unrelated, protected)
    assert nim_admission._permission_reaches_protected_object(exact, protected)


def test_nim_attributed_non_nim_controller_needs_exact_signed_exemption() -> None:
    config = _config()
    controller = {
        "apiVersion": "apps/v1",
        "kind": "DaemonSet",
        "metadata": {
            "name": "signed-keeper",
            "namespace": "fs2-models",
            "uid": "55555555-5555-4555-8555-555555555555",
            "labels": {"app.kubernetes.io/component": "cold-start-keeper"},
            "annotations": {},
        },
        "spec": {"selector": {"matchLabels": {"app": "signed-keeper"}}},
    }
    admitted = {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {"name": "signed-keeper-pod", "namespace": "fs2-models"},
        "spec": {
            "serviceAccountName": "nim-operator-runtime",
            "containers": [
                {"name": "runtime", "image": "registry.example/boltz2@sha256:" + "4" * 64}
            ],
        },
    }
    review = _review(
        resource={"group": "", "version": "v1", "resource": "pods"},
        obj=admitted,
        actor="system:kube-controller-manager",
    )
    projection = {
        "apiVersion": controller["apiVersion"],
        "kind": controller["kind"],
        "metadata": controller["metadata"],
        "spec": controller["spec"],
    }
    subject = {
        "schema": nim_admission.NON_NIM_EXEMPTION_SCHEMA,
        "model_id": "boltz2",
        "namespace": "fs2-models",
        "controller": {
            "api_version": "apps/v1",
            "kind": "DaemonSet",
            "name": "signed-keeper",
            "uid": controller["metadata"]["uid"],
            "projection_sha256": hashlib.sha256(canonical_bytes(projection)).hexdigest(),
        },
        "descendant": {
            "group": "",
            "version": "v1",
            "resource": "pods",
            "kind": "Pod",
            "spec_sha256": hashlib.sha256(canonical_bytes(admitted["spec"])).hexdigest(),
        },
        "reason": "cold-start-compatibility",
    }
    subject_sha256 = hashlib.sha256(canonical_bytes(subject)).hexdigest()
    private_key = Ed25519PrivateKey.from_private_bytes(b"\x19" * 32)
    issued = datetime.now(timezone.utc).replace(microsecond=0)
    evidence_sha256 = "1" * 64
    attestation = create_signed_attestation(
        private_key=private_key,
        session_id=config.security_session_id,
        nonce="sha256:" + "2" * 64,
        issued_at=issued.isoformat().replace("+00:00", "Z"),
        expires_at=(issued + timedelta(minutes=15)).isoformat().replace("+00:00", "Z"),
        kind="non-nim-controller-exemption",
        subject_schema=nim_admission.NON_NIM_EXEMPTION_SCHEMA,
        subject_digest="sha256:" + subject_sha256,
        model_id="boltz2",
        claims={
            "authorization_id": "signed-keeper-exemption",
            "decision": "accepted",
            "reviewer_role": "independent-platform-security",
            "evidence_sha256": evidence_sha256,
        },
    )
    envelope = {
        "subject": subject,
        "subject_sha256": subject_sha256,
        "authorization_id": "signed-keeper-exemption",
        "evidence_sha256": evidence_sha256,
        "attestation": attestation,
        "attestation_sha256": hashlib.sha256(canonical_bytes(attestation)).hexdigest(),
    }

    assert not config.allows_non_nim_controller(
        review=review, owner_chain=[controller], model_id="boltz2"
    )
    config._verify_non_nim_controller_exemption(envelope)
    config.non_nim_controller_exemptions.append(envelope)
    assert config.allows_non_nim_controller(
        review=review, owner_chain=[controller], model_id="boltz2"
    )
    admitted["spec"]["hostNetwork"] = True
    assert not config.allows_non_nim_controller(
        review=review, owner_chain=[controller], model_id="boltz2"
    )
