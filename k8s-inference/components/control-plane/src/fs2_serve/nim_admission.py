"""Fail-closed Kubernetes admission server for NIM CRs and descendants."""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import ipaddress
import json
import re
from functools import wraps
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence
from urllib.parse import quote

import httpx
import uvicorn
from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.serialization import load_pem_private_key
from cryptography.x509.oid import ExtendedKeyUsageOID, ExtensionOID
from cryptography.x509.verification import PolicyBuilder, Store, VerificationError
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fs2_serve_catalog.artifacts import canonical_bytes
from fs2_serve_catalog.attestations import verify_signed_attestation
from fs2_serve_catalog.loader import Catalog, CatalogError, load_catalog
from fs2_serve_catalog.nim_apply_fence import (
    canonical_nim_root_finalizers,
    canonical_nim_root_projection,
)
from fs2_serve_catalog.workloads import (
    validate_nim_operator_admission_review,
    validate_persisted_nim_operator_root,
)


CONFIG_SCHEMA = "fs2-serve.nebius.ai/nim-operator-admission-config/v6"
POLICY_SCHEMA = "fs2-serve.nebius.ai/nim-admission-policy/v8"
BOUNDARY_SCHEMA = "fs2-serve.nebius.ai/platform-security-admission-boundary/v3"
PROVIDER_CUSTODY_SCHEMA = "fs2-serve.nebius.ai/platform-security-provider-custody/v2"
NATIVE_ACCESS_EVIDENCE_SCHEMA = (
    "fs2-serve.nebius.ai/platform-security-native-access-evidence/v1"
)
PRINCIPAL_EPOCH_SCHEMA = "fs2-serve.nebius.ai/platform-security-principal-epoch/v1"
PROVIDER_RENEWAL_SCHEMA = "fs2-serve.nebius.ai/provider-custody-renewal/v2"
PROVIDER_CHECKPOINT_SCHEMA = "fs2-serve.nebius.ai/provider-custody-renewal-checkpoint/v2"
PROVIDER_HEAD_SCHEMA = "fs2-serve.nebius.ai/provider-custody-renewal-head/v1"
INSTALLATION_RECEIPT_SCHEMA = "fs2-serve.nebius.ai/nim-admission-installation-receipt/v5"
NON_NIM_EXEMPTION_SCHEMA = "fs2-serve.nebius.ai/non-nim-controller-exemption/v1"
ENVELOPE_ANNOTATION = "fs2-serve.nebius.ai/operator-security-envelope-sha256"
ROOT_ENROLLMENT_LABEL = "fs2-serve.nebius.ai/nim-root-enrollment"
_REQUIRED_PROVIDER_NATIVE_SCOPES = (
    "provider:authentication-paths",
    "provider:credential-issuance",
    "provider:group-memberships",
    "provider:iam-bindings",
    "provider:impersonation-paths",
)
_REQUIRED_KUBERNETES_NATIVE_SCOPES = (
    "rbac.authorization.k8s.io/v1/clusterrolebindings",
    "rbac.authorization.k8s.io/v1/clusterroles",
    "rbac.authorization.k8s.io/v1/rolebindings@all-namespaces",
    "rbac.authorization.k8s.io/v1/roles@all-namespaces",
)
_RESOURCE_PATHS = {
    ("apps.nvidia.com/v1alpha1", "NIMCache"): "/apis/apps.nvidia.com/v1alpha1/namespaces/{namespace}/nimcaches/{name}",
    ("apps.nvidia.com/v1alpha1", "NIMService"): "/apis/apps.nvidia.com/v1alpha1/namespaces/{namespace}/nimservices/{name}",
    ("apps/v1", "Deployment"): "/apis/apps/v1/namespaces/{namespace}/deployments/{name}",
    ("apps/v1", "DaemonSet"): "/apis/apps/v1/namespaces/{namespace}/daemonsets/{name}",
    ("apps/v1", "ReplicaSet"): "/apis/apps/v1/namespaces/{namespace}/replicasets/{name}",
    ("apps/v1", "StatefulSet"): "/apis/apps/v1/namespaces/{namespace}/statefulsets/{name}",
    ("batch/v1", "Job"): "/apis/batch/v1/namespaces/{namespace}/jobs/{name}",
    ("batch/v1", "CronJob"): "/apis/batch/v1/namespaces/{namespace}/cronjobs/{name}",
    ("jobset.x-k8s.io/v1alpha2", "JobSet"): "/apis/jobset.x-k8s.io/v1alpha2/namespaces/{namespace}/jobsets/{name}",
    ("v1", "Pod"): "/api/v1/namespaces/{namespace}/pods/{name}",
}
_SECURITY_OBJECT_PATHS = {
    ("v1", "ConfigMap"): "/api/v1/namespaces/{namespace}/configmaps/{name}",
    ("v1", "Secret"): "/api/v1/namespaces/{namespace}/secrets/{name}",
    ("v1", "ServiceAccount"): "/api/v1/namespaces/{namespace}/serviceaccounts/{name}",
    ("v1", "Service"): "/api/v1/namespaces/{namespace}/services/{name}",
    ("apps/v1", "Deployment"): "/apis/apps/v1/namespaces/{namespace}/deployments/{name}",
    ("networking.k8s.io/v1", "NetworkPolicy"): "/apis/networking.k8s.io/v1/namespaces/{namespace}/networkpolicies/{name}",
    ("policy/v1", "PodDisruptionBudget"): "/apis/policy/v1/namespaces/{namespace}/poddisruptionbudgets/{name}",
    ("rbac.authorization.k8s.io/v1", "Role"): "/apis/rbac.authorization.k8s.io/v1/namespaces/{namespace}/roles/{name}",
    ("rbac.authorization.k8s.io/v1", "RoleBinding"): "/apis/rbac.authorization.k8s.io/v1/namespaces/{namespace}/rolebindings/{name}",
    ("rbac.authorization.k8s.io/v1", "ClusterRole"): "/apis/rbac.authorization.k8s.io/v1/clusterroles/{name}",
    ("rbac.authorization.k8s.io/v1", "ClusterRoleBinding"): "/apis/rbac.authorization.k8s.io/v1/clusterrolebindings/{name}",
    ("admissionregistration.k8s.io/v1", "ValidatingAdmissionPolicy"): "/apis/admissionregistration.k8s.io/v1/validatingadmissionpolicies/{name}",
    ("admissionregistration.k8s.io/v1", "ValidatingAdmissionPolicyBinding"): "/apis/admissionregistration.k8s.io/v1/validatingadmissionpolicybindings/{name}",
    ("admissionregistration.k8s.io/v1", "ValidatingWebhookConfiguration"): "/apis/admissionregistration.k8s.io/v1/validatingwebhookconfigurations/{name}",
}


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("NIM admission configuration contains a duplicate key")
        value[key] = item
    return value


def _root_enrollment_name(*, subject_sha256: str, root_uid: str) -> str:
    """Return a generation-addressed name while retaining old immutable records."""

    if (
        re.fullmatch(r"[0-9a-f]{64}", subject_sha256) is None
        or re.fullmatch(
            r"[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}",
            root_uid,
        )
        is None
    ):
        raise CatalogError("NIM root enrollment generation identity is invalid")
    return f"fs2-nim-root-{subject_sha256[:12]}-{root_uid}"


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _valid_cidr(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        network = ipaddress.ip_network(value, strict=True)
    except ValueError:
        return False
    return str(network) == value


def _ip_matches_address_type(value: object, address_type: object) -> bool:
    if not isinstance(value, str) or address_type not in {"IPv4", "IPv6"}:
        return False
    try:
        version = ipaddress.ip_address(value).version
    except ValueError:
        return False
    return version == (4 if address_type == "IPv4" else 6)


def _parse_timestamp(value: object, *, field: str) -> datetime:
    if not isinstance(value, str) or re.fullmatch(
        r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z", value
    ) is None:
        raise CatalogError(f"NIM {field} timestamp is invalid")
    return datetime.fromisoformat(value[:-1] + "+00:00")


def _validate_principal_epoch(value: object) -> tuple[str, set[str]]:
    """Validate the append-only external-identity epoch and return protected principals.

    Rotation is two phase.  An optional next principal may coexist only during
    the bounded overlap window; it is not the active successor.  A later epoch
    can activate that successor only after the previous principal appears in
    retained_predecessors with independently signed denied/expired evidence.
    """

    if not isinstance(value, Mapping) or set(value) != {
        "schema",
        "generation",
        "epoch_id",
        "active_principal",
        "activated_at",
        "max_overlap_seconds",
        "overlap_principals",
        "retained_predecessors",
        "predecessor_chain_sha256",
    }:
        raise CatalogError("NIM Platform Security principal epoch fields differ")
    generation = value.get("generation")
    epoch_id = value.get("epoch_id")
    active = value.get("active_principal")
    overlaps = value.get("overlap_principals")
    predecessors = value.get("retained_predecessors")
    if (
        value.get("schema") != PRINCIPAL_EPOCH_SCHEMA
        or not isinstance(generation, int)
        or generation < 1
        or not isinstance(epoch_id, str)
        or re.fullmatch(r"[a-f0-9]{64}", epoch_id) is None
        or active != f"fs2-platform-security-external-automation-{epoch_id}"
        or not isinstance(value.get("max_overlap_seconds"), int)
        or not 60 <= value["max_overlap_seconds"] <= 900
        or not isinstance(overlaps, list)
        or len(overlaps) > 1
        or not isinstance(predecessors, list)
    ):
        raise CatalogError("NIM Platform Security principal epoch identity differs")
    activated_at = _parse_timestamp(value.get("activated_at"), field="principal activation")
    now = datetime.now(timezone.utc).replace(microsecond=0)
    if activated_at > now + timedelta(minutes=5):
        raise CatalogError("NIM Platform Security principal activation is future dated")
    protected = {str(active)}
    normalized_overlaps: list[Mapping[str, Any]] = []
    for overlap in overlaps:
        if not isinstance(overlap, Mapping) or set(overlap) != {
            "generation", "epoch_id", "principal", "not_after"
        }:
            raise CatalogError("NIM Platform Security overlap principal differs")
        overlap_epoch = overlap.get("epoch_id")
        not_after = _parse_timestamp(overlap.get("not_after"), field="principal overlap")
        if (
            overlap.get("generation") != generation + 1
            or not isinstance(overlap_epoch, str)
            or re.fullmatch(r"[a-f0-9]{64}", overlap_epoch) is None
            or overlap.get("principal")
            != f"fs2-platform-security-external-automation-{overlap_epoch}"
            or not_after <= now
            or not_after - now > timedelta(seconds=value["max_overlap_seconds"])
        ):
            raise CatalogError("NIM Platform Security overlap is not bounded")
        normalized_overlaps.append(dict(overlap))
        protected.add(str(overlap["principal"]))
    if overlaps != sorted(normalized_overlaps, key=canonical_bytes):
        raise CatalogError("NIM Platform Security overlap principals are not canonical")
    normalized_predecessors: list[Mapping[str, Any]] = []
    for predecessor in predecessors:
        if not isinstance(predecessor, Mapping) or set(predecessor) != {
            "generation",
            "epoch_id",
            "principal",
            "disposition",
            "effective_at",
            "evidence_sha256",
        }:
            raise CatalogError("NIM Platform Security predecessor evidence differs")
        predecessor_epoch = predecessor.get("epoch_id")
        effective_at = _parse_timestamp(
            predecessor.get("effective_at"), field="principal retirement"
        )
        if (
            not isinstance(predecessor.get("generation"), int)
            or not 1 <= predecessor["generation"] < generation
            or not isinstance(predecessor_epoch, str)
            or re.fullmatch(r"[a-f0-9]{64}", predecessor_epoch) is None
            or predecessor.get("principal")
            != f"fs2-platform-security-external-automation-{predecessor_epoch}"
            or predecessor.get("disposition") not in {"denied", "expired"}
            or effective_at > activated_at
            or re.fullmatch(r"[a-f0-9]{64}", str(predecessor.get("evidence_sha256"))) is None
        ):
            raise CatalogError("NIM Platform Security predecessor is not retired")
        normalized_predecessors.append(dict(predecessor))
        protected.add(str(predecessor["principal"]))
    if (
        predecessors != sorted(normalized_predecessors, key=lambda item: item["generation"])
        or len({item["generation"] for item in normalized_predecessors})
        != len(normalized_predecessors)
        or len({item["principal"] for item in normalized_predecessors})
        != len(normalized_predecessors)
        or str(active) in {item["principal"] for item in normalized_predecessors}
        or any(
            overlap["principal"] in {item["principal"] for item in normalized_predecessors}
            for overlap in normalized_overlaps
        )
        or (generation == 1 and predecessors)
        or (generation > 1 and [item["generation"] for item in normalized_predecessors]
            != list(range(1, generation)))
        or value.get("predecessor_chain_sha256")
        != _sha256(canonical_bytes(normalized_predecessors))
    ):
        raise CatalogError("NIM Platform Security predecessor chain is incomplete")
    return str(active), protected


def _tls_generation_sha256(*, certificate: str, spki: str, ca: str) -> str:
    return _sha256(f"{certificate}\n{spki}\n{ca}\n".encode())


def _desired_object_projection(value: Mapping[str, Any], *, secret: bool) -> Mapping[str, Any]:
    """Project live bytes using the handoff's canonical desired-object contract."""

    projection = {
        key: item
        for key, item in value.items()
        if key not in {"metadata", "status"}
        and not (secret and key in {"data", "stringData"})
    }
    metadata = value.get("metadata")
    if not isinstance(metadata, Mapping):
        raise CatalogError("NIM live installation object metadata is absent")
    projection["metadata"] = {
        key: item
        for key, item in metadata.items()
        if key
        not in {
            "creationTimestamp",
            "deletionGracePeriodSeconds",
            "deletionTimestamp",
            "generation",
            "managedFields",
            "resourceVersion",
            "selfLink",
            "uid",
        }
    }
    return projection


def _controller_template_projection(value: Mapping[str, Any]) -> Mapping[str, Any]:
    """Normalize only documented Deployment-controller/Pod runtime injections."""

    metadata = value.get("metadata")
    spec = value.get("spec")
    if not isinstance(metadata, Mapping) or not isinstance(spec, Mapping):
        raise CatalogError("NIM admission backend template is absent")
    labels = dict(metadata.get("labels", {}))
    labels.pop("pod-template-hash", None)
    annotations = dict(metadata.get("annotations", {}))
    return {
        "metadata": {"labels": labels, "annotations": annotations},
        "spec": dict(spec),
    }


def _pod_template_projection(value: Mapping[str, Any]) -> Mapping[str, Any]:
    metadata = value.get("metadata")
    spec = value.get("spec")
    if not isinstance(metadata, Mapping) or not isinstance(spec, Mapping):
        raise CatalogError("NIM admission backend Pod template is absent")
    projected_spec = {
        key: item
        for key, item in spec.items()
        if key
        not in {
            "hostname",
            "nodeName",
            "preemptionPolicy",
            "priority",
            "priorityClassName",
            "resourceClaims",
            "schedulerName",
            "serviceAccount",
            "subdomain",
        }
    }
    if isinstance(projected_spec.get("tolerations"), list):
        projected_spec["tolerations"] = [
            toleration
            for toleration in projected_spec["tolerations"]
            if not (
                isinstance(toleration, Mapping)
                and toleration.get("key")
                in {"node.kubernetes.io/not-ready", "node.kubernetes.io/unreachable"}
                and toleration.get("operator") == "Exists"
                and toleration.get("effect") == "NoExecute"
                and toleration.get("tolerationSeconds") == 300
            )
        ]
    return _controller_template_projection(
        {"metadata": metadata, "spec": projected_spec}
    )


def verify_mounted_tls_identity(
    *,
    policy: Mapping[str, Any],
    certificate_file: Path,
    private_key_file: Path,
    ca_file: Path,
) -> None:
    """Verify public TLS identity before Uvicorn can bind its socket.

    Private-key bytes remain confined to the mounted generation-addressed
    Secret. Only the public SPKI is compared with the signed policy.
    """

    tls = policy.get("tls")
    if not isinstance(tls, Mapping) or set(tls) != {
        "secret_name",
        "secret_uid",
        "secret_resource_version",
        "secret_type",
        "generation_sha256",
        "certificate_sha256",
        "public_key_spki_sha256",
        "ca_bundle_sha256",
        "service_dns_name",
    }:
        raise CatalogError("NIM admission TLS policy differs")
    try:
        certificate_bytes = certificate_file.read_bytes()
        private_key_bytes = private_key_file.read_bytes()
        ca_bytes = ca_file.read_bytes()
        certificates = x509.load_pem_x509_certificates(certificate_bytes)
        ca_certificates = x509.load_pem_x509_certificates(ca_bytes)
        private_key = load_pem_private_key(private_key_bytes, password=None)
    except (OSError, TypeError, ValueError) as error:
        raise CatalogError("NIM admission mounted TLS material is invalid") from error
    if not certificates or not ca_certificates:
        raise CatalogError("NIM admission TLS leaf or CA bundle cardinality differs")
    leaf = certificates[0]
    certificate_sha256 = _sha256(leaf.public_bytes(serialization.Encoding.DER))
    public_key = leaf.public_key().public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    private_public_key = private_key.public_key().public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    spki_sha256 = _sha256(public_key)
    ca_sha256 = _sha256(ca_bytes)
    generation_sha256 = _tls_generation_sha256(
        certificate=certificate_sha256,
        spki=spki_sha256,
        ca=ca_sha256,
    )
    try:
        dns_names = set(leaf.extensions.get_extension_for_oid(ExtensionOID.SUBJECT_ALTERNATIVE_NAME).value.get_values_for_type(x509.DNSName))
        extended_key_usage = leaf.extensions.get_extension_for_oid(ExtensionOID.EXTENDED_KEY_USAGE).value
        key_usage = leaf.extensions.get_extension_for_oid(ExtensionOID.KEY_USAGE).value
        basic_constraints = leaf.extensions.get_extension_for_oid(ExtensionOID.BASIC_CONSTRAINTS).value
    except x509.ExtensionNotFound as error:
        raise CatalogError("NIM admission TLS certificate constraints are incomplete") from error
    expected_dns_name = tls.get("service_dns_name")
    if (
        not isinstance(expected_dns_name, str)
        or dns_names != {expected_dns_name}
        or ExtendedKeyUsageOID.SERVER_AUTH not in extended_key_usage
        or not key_usage.digital_signature
        or basic_constraints.ca
        or certificate_sha256 != tls.get("certificate_sha256")
        or spki_sha256 != tls.get("public_key_spki_sha256")
        or ca_sha256 != tls.get("ca_bundle_sha256")
        or generation_sha256 != tls.get("generation_sha256")
        or private_public_key != public_key
        or tls.get("secret_type") != "kubernetes.io/tls"
        or not isinstance(tls.get("secret_uid"), str)
        or re.fullmatch(
            r"[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}",
            str(tls.get("secret_uid")),
        )
        is None
        or not isinstance(tls.get("secret_resource_version"), str)
        or re.fullmatch(r"[1-9][0-9]*", str(tls.get("secret_resource_version"))) is None
        or tls.get("secret_name")
        != f"fs2-nim-admission-tls-{generation_sha256[:16]}"
    ):
        raise CatalogError("NIM admission mounted TLS identity differs")
    try:
        verifier = (
            PolicyBuilder()
            .store(Store(ca_certificates))
            .time(datetime.now(timezone.utc))
            .build_server_verifier(x509.DNSName(expected_dns_name))
        )
        verifier.verify(leaf, certificates[1:])
    except (TypeError, ValueError, VerificationError) as error:
        raise CatalogError(
            "NIM admission TLS leaf is expired, untrusted, or unusable for server authentication"
        ) from error


def _protected_security_objects(policy: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    """Return the exact object set that the external provider deny must cover."""

    namespaces = policy.get("owner_lookup_namespaces")
    tls = policy.get("tls")
    if (
        not isinstance(namespaces, list)
        or namespaces != sorted(set(namespaces))
        or not namespaces
        or any(not isinstance(namespace, str) or not namespace for namespace in namespaces)
        or not isinstance(tls, Mapping)
        or not isinstance(tls.get("secret_name"), str)
    ):
        raise CatalogError("NIM protected security object inventory is absent")
    objects: list[Mapping[str, Any]] = [
        {"api_group": "", "resource": "configmaps", "scope": "namespace:fs2-system", "names": [], "name_prefixes": ["fs2-nim-admission-", "fs2-nim-installation-", "fs2-nim-root-"]},
        {"api_group": "", "resource": "secrets", "scope": "namespace:fs2-system", "names": [str(tls["secret_name"])], "name_prefixes": []},
        {"api_group": "", "resource": "serviceaccounts", "scope": "namespace:fs2-system", "names": ["fs2-serve-control-plane-nim-admission"], "name_prefixes": []},
        {"api_group": "", "resource": "services", "scope": "namespace:fs2-system", "names": ["fs2-serve-control-plane-nim-admission"], "name_prefixes": []},
        {"api_group": "apps", "resource": "deployments", "scope": "namespace:fs2-system", "names": ["fs2-serve-control-plane-nim-admission"], "name_prefixes": []},
        {"api_group": "networking.k8s.io", "resource": "networkpolicies", "scope": "namespace:fs2-system", "names": ["fs2-serve-control-plane-nim-admission"], "name_prefixes": []},
        {"api_group": "policy", "resource": "poddisruptionbudgets", "scope": "namespace:fs2-system", "names": ["fs2-serve-control-plane-nim-admission"], "name_prefixes": []},
        {"api_group": "admissionregistration.k8s.io", "resource": "validatingadmissionpolicies", "scope": "cluster", "names": ["fs2-platform-security-admission-guard", "fs2-scientific-cache-controller-chain", "fs2-scientific-runtime-cache-writer-fence"], "name_prefixes": []},
        {"api_group": "admissionregistration.k8s.io", "resource": "validatingadmissionpolicybindings", "scope": "cluster", "names": ["fs2-platform-security-admission-guard", "fs2-scientific-cache-controller-chain", "fs2-scientific-runtime-cache-writer-fence"], "name_prefixes": []},
        {"api_group": "admissionregistration.k8s.io", "resource": "validatingwebhookconfigurations", "scope": "cluster", "names": ["fs2-serve-control-plane-nim-admission"], "name_prefixes": []},
        {"api_group": "rbac.authorization.k8s.io", "resource": "clusterroles", "scope": "cluster", "names": ["fs2-serve-control-plane-nim-policy-reader"], "name_prefixes": []},
        {"api_group": "rbac.authorization.k8s.io", "resource": "clusterrolebindings", "scope": "cluster", "names": ["fs2-serve-control-plane-nim-policy-reader"], "name_prefixes": []},
        {"api_group": "rbac.authorization.k8s.io", "resource": "roles", "scope": "namespace:fs2-models", "names": ["fs2-serve-control-plane-nim-root-inventory"], "name_prefixes": []},
        {"api_group": "rbac.authorization.k8s.io", "resource": "rolebindings", "scope": "namespace:fs2-models", "names": ["fs2-serve-control-plane-nim-root-inventory"], "name_prefixes": []},
        {"api_group": "rbac.authorization.k8s.io", "resource": "roles", "scope": "namespace:fs2-system", "names": ["fs2-serve-control-plane-nim-root-enrollment"], "name_prefixes": []},
        {"api_group": "rbac.authorization.k8s.io", "resource": "rolebindings", "scope": "namespace:fs2-system", "names": ["fs2-serve-control-plane-nim-root-enrollment"], "name_prefixes": []},
    ]
    for namespace in namespaces:
        for resource in ("roles", "rolebindings"):
            objects.append(
                {
                    "api_group": "rbac.authorization.k8s.io",
                    "resource": resource,
                    "scope": f"namespace:{namespace}",
                    "names": ["fs2-serve-control-plane-nim-owner-reader"],
                    "name_prefixes": [],
                }
            )
    return sorted(objects, key=canonical_bytes)


def _permission_reaches_protected_object(
    permission: Mapping[str, Any], protected: Mapping[str, Any]
) -> bool:
    if (
        permission["api_group"] not in {"*", protected["api_group"]}
        or permission["resource"] not in {"*", protected["resource"]}
        or permission["subresource"] not in {"", "*"}
    ):
        return False
    permission_scope = str(permission["scope"])
    protected_scope = str(protected["scope"])
    if protected_scope == "cluster":
        scope_matches = permission_scope in {"cluster", "all"}
    else:
        scope_matches = permission_scope in {protected_scope, "all-namespaces", "all"}
    if not scope_matches:
        return False
    # Kubernetes does not enforce resourceNames on CREATE. An empty list on
    # any other write verb means all names in the exact intersecting scope.
    if permission["verb"] == "create" or not permission["resource_names"]:
        return True
    return any(
        name in protected["names"]
        or any(name.startswith(prefix) for prefix in protected["name_prefixes"])
        for name in permission["resource_names"]
    )


def _permission_impersonates_security_principal(
    permission: Mapping[str, Any], *, principal: str, groups: set[str]
) -> bool:
    if permission["verb"] not in {"impersonate", "*"}:
        return False
    names = set(permission["resource_names"])
    if permission["api_group"] not in {"", "*"}:
        return False
    return (
        permission["resource"] in {"users", "*"}
        and (not names or principal in names)
    ) or (
        permission["resource"] in {"groups", "*"}
        and (not names or bool(names.intersection(groups)))
    )


def _permission_reaches_credential_target(
    permission: Mapping[str, Any], target: Mapping[str, Any]
) -> bool:
    return (
        permission["verb"]
        in {"*", "create", "get", "impersonate", "list", "patch", "update", "watch"}
        and permission["api_group"] in {"*", target["api_group"]}
        and permission["resource"] in {"*", target["resource"]}
        and permission["subresource"] in {"*", target["subresource"]}
        and permission["scope"] in {target["scope"], "all", "all-namespaces"}
        and (
            not permission["resource_names"]
            or target["name"] in permission["resource_names"]
        )
    )


def _native_permission_rows(
    evidence: Mapping[str, Any], *, principals: set[str], collector: Mapping[str, Any]
) -> list[Mapping[str, Any]]:
    """Derive effective rows from signed native provider pages and Kubernetes RBAC.

    The semantic conclusion is never accepted from the collector. Each page and
    RBAC object is content-addressed inside a separately signed evidence subject,
    then this function expands provider grants, group inheritance, namespaced and
    cluster bindings, ClusterRole aggregation, wildcards, subresources, and
    resourceNames into the rows consumed by the deny analysis.
    """

    pages = evidence.get("provider_pages")
    kubernetes_pages = evidence.get("kubernetes_pages")
    memberships = evidence.get("group_memberships")
    if not isinstance(pages, list) or not isinstance(kubernetes_pages, list) or not isinstance(
        memberships, Mapping
    ):
        raise CatalogError("NIM native access evidence inventory is absent")
    group_memberships: dict[str, set[str]] = {}
    for principal in principals:
        groups = memberships.get(principal, [])
        if (
            not isinstance(groups, list)
            or groups != sorted(set(groups))
            or any(not isinstance(group, str) or not group for group in groups)
        ):
            raise CatalogError("NIM native access evidence group closure differs")
        group_memberships[principal] = set(groups)

    rows: list[Mapping[str, Any]] = []
    page_chains: dict[str, list[Mapping[str, Any]]] = {}
    for page in pages:
        if not isinstance(page, Mapping) or set(page) != {
            "source_id", "scope", "page_number", "request", "request_sha256",
            "response", "response_sha256", "next_page_token_sha256", "terminal",
            "authority", "snapshot_id", "request_id", "received_at",
        }:
            raise CatalogError("NIM native provider page fields differ")
        if (
            not isinstance(page.get("source_id"), str)
            or not page["source_id"]
            or not isinstance(page.get("scope"), str)
            or not page["scope"]
            or not isinstance(page.get("page_number"), int)
            or page["page_number"] < 1
            or _sha256(canonical_bytes(page.get("request"))) != page.get("request_sha256")
            or _sha256(canonical_bytes(page.get("response"))) != page.get("response_sha256")
            or re.fullmatch(r"(?:|[a-f0-9]{64})", str(page.get("next_page_token_sha256")))
            is None
            or not isinstance(page.get("terminal"), bool)
            or not isinstance(page.get("authority"), str)
            or not page["authority"]
            or not isinstance(page.get("snapshot_id"), str)
            or not page["snapshot_id"]
            or not isinstance(page.get("request_id"), str)
            or not page["request_id"]
        ):
            raise CatalogError("NIM native provider page provenance differs")
        _parse_timestamp(page.get("received_at"), field="native provider response")
        page_chains.setdefault(str(page["scope"]), []).append(page)
        response = page["response"]
        bindings = response.get("bindings") if isinstance(response, Mapping) else None
        next_page_token = (
            response.get("next_page_token") if isinstance(response, Mapping) else None
        )
        if (
            not isinstance(bindings, list)
            or not isinstance(next_page_token, str)
            or page["next_page_token_sha256"]
            != (_sha256(next_page_token.encode()) if next_page_token else "")
            or page["terminal"] != (next_page_token == "")
        ):
            raise CatalogError("NIM native provider binding page differs")
        for binding in bindings:
            if not isinstance(binding, Mapping) or set(binding) != {
                "binding_id", "principal", "principal_kind", "groups", "scope",
                "permissions",
            }:
                raise CatalogError("NIM native provider binding differs")
            grant_principal = binding.get("principal")
            grant_kind = binding.get("principal_kind")
            grant_groups = binding.get("groups")
            permissions = binding.get("permissions")
            if (
                grant_kind not in {"user", "group", "serviceaccount"}
                or not isinstance(grant_principal, str)
                or not grant_principal
                or not isinstance(grant_groups, list)
                or grant_groups != sorted(set(grant_groups))
                or not isinstance(permissions, list)
            ):
                raise CatalogError("NIM native provider binding identity differs")
            for principal in principals:
                if not (
                    grant_principal == principal
                    or (
                        grant_kind == "group"
                        and grant_principal in group_memberships[principal]
                    )
                ):
                    continue
                for permission in permissions:
                    if not isinstance(permission, Mapping) or set(permission) != {
                        "api_groups", "resources", "verbs", "resource_names"
                    }:
                        raise CatalogError("NIM native provider permission differs")
                    for api_group in permission["api_groups"]:
                        for resource_value in permission["resources"]:
                            resource, separator, subresource = str(resource_value).partition("/")
                            for verb in permission["verbs"]:
                                rows.append(
                                    {
                                        "authorization_source_id": (
                                            f"provider:{page['source_id']}:{binding['binding_id']}"
                                        ),
                                        "principal": principal,
                                        "grant_principal": grant_principal,
                                        "grant_principal_kind": grant_kind,
                                        "inherited_groups": sorted(group_memberships[principal]),
                                        "scope": binding["scope"],
                                        "api_group": api_group,
                                        "resource": resource,
                                        "subresource": subresource if separator else "",
                                        "verb": verb,
                                        "resource_names": sorted(permission["resource_names"]),
                                    }
                                )
    for scope, chain in page_chains.items():
        ordered = sorted(chain, key=lambda item: item["page_number"])
        if (
            [item["page_number"] for item in ordered]
            != list(range(1, len(ordered) + 1))
            or [item["terminal"] for item in ordered].count(True) != 1
            or ordered[-1]["terminal"] is not True
            or ordered[-1]["next_page_token_sha256"] != ""
            or any(item["terminal"] for item in ordered[:-1])
            or any(
                not isinstance(item["request"], Mapping)
                or item["request"].get("page_token")
                != (
                    ""
                    if index == 0
                    else ordered[index - 1]["response"]["next_page_token"]
                )
                for index, item in enumerate(ordered)
            )
            or len({item["authority"] for item in ordered}) != 1
            or len({item["snapshot_id"] for item in ordered}) != 1
        ):
            raise CatalogError(f"NIM native provider pagination is incomplete for {scope}")
    scope_inventory = evidence.get("scope_inventory")
    if (
        not isinstance(scope_inventory, Mapping)
        or set(scope_inventory) != {"provider", "kubernetes"}
        or scope_inventory.get("provider") != list(_REQUIRED_PROVIDER_NATIVE_SCOPES)
        or sorted(page_chains) != list(_REQUIRED_PROVIDER_NATIVE_SCOPES)
    ):
        raise CatalogError("NIM native provider scope inventory is incomplete")

    objects: list[Mapping[str, Any]] = []
    kubernetes_chains: dict[str, list[Mapping[str, Any]]] = {}
    for page in kubernetes_pages:
        if not isinstance(page, Mapping) or set(page) != {
            "source_id", "scope", "page_number", "request", "request_sha256",
            "response", "response_sha256", "continue_token_sha256", "terminal",
            "list_resource_version", "apiserver_identity", "snapshot_id", "received_at",
        }:
            raise CatalogError("NIM native Kubernetes authorization page fields differ")
        response = page.get("response")
        response_metadata = response.get("metadata") if isinstance(response, Mapping) else None
        items = response.get("items") if isinstance(response, Mapping) else None
        if (
            not isinstance(items, list)
            or not isinstance(response_metadata, Mapping)
            or page.get("list_resource_version") != response_metadata.get("resourceVersion")
            or _sha256(canonical_bytes(page.get("request"))) != page.get("request_sha256")
            or _sha256(canonical_bytes(response)) != page.get("response_sha256")
            or not isinstance(page.get("apiserver_identity"), str)
            or not page["apiserver_identity"]
            or not isinstance(page.get("snapshot_id"), str)
            or not page["snapshot_id"]
            or not isinstance(page.get("page_number"), int)
            or page["page_number"] < 1
            or not isinstance(response_metadata.get("continue", ""), str)
            or page.get("continue_token_sha256")
            != (
                _sha256(str(response_metadata.get("continue", "")).encode())
                if response_metadata.get("continue", "")
                else ""
            )
            or page.get("terminal") != (response_metadata.get("continue", "") == "")
        ):
            raise CatalogError("NIM native Kubernetes authorization page differs")
        _parse_timestamp(page.get("received_at"), field="native Kubernetes response")
        kubernetes_chains.setdefault(str(page["scope"]), []).append(page)
        objects.extend(items)
    for scope, chain in kubernetes_chains.items():
        ordered = sorted(chain, key=lambda item: item["page_number"])
        if (
            [item["page_number"] for item in ordered]
            != list(range(1, len(ordered) + 1))
            or [item["terminal"] for item in ordered].count(True) != 1
            or ordered[-1]["terminal"] is not True
            or ordered[-1]["continue_token_sha256"] != ""
            or any(item["terminal"] for item in ordered[:-1])
            or len({item["list_resource_version"] for item in ordered}) != 1
            or any(
                not isinstance(item["request"], Mapping)
                or item["request"].get("continue")
                != (
                    ""
                    if index == 0
                    else ordered[index - 1]["response"]["metadata"].get("continue", "")
                )
                for index, item in enumerate(ordered)
            )
            or len({item["apiserver_identity"] for item in ordered}) != 1
            or len({item["snapshot_id"] for item in ordered}) != 1
        ):
            raise CatalogError(f"NIM native Kubernetes pagination is incomplete for {scope}")
    if (
        scope_inventory.get("kubernetes") != list(_REQUIRED_KUBERNETES_NATIVE_SCOPES)
        or sorted(kubernetes_chains) != list(_REQUIRED_KUBERNETES_NATIVE_SCOPES)
    ):
        raise CatalogError("NIM native Kubernetes scope inventory is incomplete")

    observed_at = _parse_timestamp(evidence.get("observed_at"), field="native evidence")
    page_times = [
        _parse_timestamp(page.get("received_at"), field="native evidence page")
        for page in [*pages, *kubernetes_pages]
    ]
    terminal_rows = sorted(
        [
            {
                "source": "provider",
                "scope": scope,
                "response_sha256": sorted(chain, key=lambda item: item["page_number"])[-1][
                    "response_sha256"
                ],
            }
            for scope, chain in page_chains.items()
        ]
        + [
            {
                "source": "kubernetes",
                "scope": scope,
                "response_sha256": sorted(chain, key=lambda item: item["page_number"])[-1][
                    "response_sha256"
                ],
            }
            for scope, chain in kubernetes_chains.items()
        ],
        key=canonical_bytes,
    )
    if (
        collector.get("page_count") != len(pages) + len(kubernetes_pages)
        or collector.get("terminal_page_sha256")
        != _sha256(canonical_bytes(terminal_rows))
        or any(abs(received - observed_at) > timedelta(seconds=30) for received in page_times)
        or len({page["snapshot_id"] for page in [*pages, *kubernetes_pages]}) != 1
    ):
        raise CatalogError("NIM native evidence snapshot or terminal inventory differs")

    roles: dict[tuple[str, str, str], Mapping[str, Any]] = {}
    bindings: list[Mapping[str, Any]] = []
    for value in objects:
        if not isinstance(value, Mapping):
            raise CatalogError("NIM native Kubernetes RBAC object differs")
        metadata = value.get("metadata")
        if not isinstance(metadata, Mapping) or not isinstance(metadata.get("uid"), str):
            raise CatalogError("NIM native Kubernetes RBAC identity differs")
        kind = value.get("kind")
        namespace = str(metadata.get("namespace", ""))
        name = metadata.get("name")
        if kind in {"Role", "ClusterRole"} and isinstance(name, str):
            roles[(str(kind), namespace, name)] = value
        elif kind in {"RoleBinding", "ClusterRoleBinding"}:
            bindings.append(value)
        else:
            raise CatalogError("NIM native Kubernetes RBAC kind differs")

    def role_rules(role: Mapping[str, Any], seen: set[str]) -> list[Mapping[str, Any]]:
        metadata = role["metadata"]
        uid = str(metadata["uid"])
        if uid in seen:
            raise CatalogError("NIM ClusterRole aggregation contains a cycle")
        seen = {*seen, uid}
        rules = role.get("rules", [])
        if not isinstance(rules, list):
            raise CatalogError("NIM native Kubernetes role rules differ")
        aggregation = role.get("aggregationRule")
        if aggregation is not None:
            selectors = aggregation.get("clusterRoleSelectors") if isinstance(aggregation, Mapping) else None
            if not isinstance(selectors, list):
                raise CatalogError("NIM ClusterRole aggregation differs")
            for selector in selectors:
                if not isinstance(selector, Mapping) or set(selector).difference(
                    {"matchLabels", "matchExpressions"}
                ):
                    raise CatalogError("NIM ClusterRole aggregation selector differs")
            for candidate in roles.values():
                if candidate.get("kind") != "ClusterRole":
                    continue
                labels = candidate.get("metadata", {}).get("labels", {})
                def matches(selector: Mapping[str, Any]) -> bool:
                    if not all(
                        labels.get(key) == item
                        for key, item in selector.get("matchLabels", {}).items()
                    ):
                        return False
                    for expression in selector.get("matchExpressions", []):
                        if not isinstance(expression, Mapping) or set(expression) != {
                            "key", "operator", "values"
                        }:
                            raise CatalogError("NIM ClusterRole aggregation expression differs")
                        actual = labels.get(expression["key"])
                        values = expression["values"]
                        operator = expression["operator"]
                        if (
                            (operator == "In" and actual not in values)
                            or (operator == "NotIn" and actual in values)
                            or (operator == "Exists" and expression["key"] not in labels)
                            or (operator == "DoesNotExist" and expression["key"] in labels)
                            or operator not in {"In", "NotIn", "Exists", "DoesNotExist"}
                        ):
                            return False
                    return True

                if any(matches(selector) for selector in selectors):
                    rules = [*rules, *role_rules(candidate, seen)]
        return rules

    for binding in bindings:
        metadata = binding["metadata"]
        namespace = str(metadata.get("namespace", ""))
        role_ref = binding.get("roleRef")
        subjects = binding.get("subjects")
        if not isinstance(role_ref, Mapping) or not isinstance(subjects, list):
            raise CatalogError("NIM native Kubernetes binding differs")
        role_kind = str(role_ref.get("kind"))
        role_namespace = "" if role_kind == "ClusterRole" else namespace
        role = roles.get((role_kind, role_namespace, str(role_ref.get("name"))))
        if role is None:
            raise CatalogError("NIM native Kubernetes binding role is absent")
        scope = "cluster" if binding.get("kind") == "ClusterRoleBinding" else f"namespace:{namespace}"
        for subject in subjects:
            if not isinstance(subject, Mapping):
                raise CatalogError("NIM native Kubernetes binding subject differs")
            subject_kind = str(subject.get("kind", "")).lower()
            subject_name = str(subject.get("name", ""))
            if subject_kind == "serviceaccount":
                subject_name = (
                    f"system:serviceaccount:{subject.get('namespace', namespace)}:{subject_name}"
                )
            for principal in principals:
                if not (
                    subject_name == principal
                    or (
                        subject_kind == "group"
                        and subject_name in group_memberships[principal]
                    )
                ):
                    continue
                for rule in role_rules(role, set()):
                    if not isinstance(rule, Mapping):
                        raise CatalogError("NIM native Kubernetes rule differs")
                    for api_group in rule.get("apiGroups", [""]):
                        for resource_value in rule.get("resources", []):
                            resource, separator, subresource = str(resource_value).partition("/")
                            for verb in rule.get("verbs", []):
                                rows.append(
                                    {
                                        "authorization_source_id": (
                                            f"kubernetes:{metadata['uid']}:{role['metadata']['uid']}"
                                        ),
                                        "principal": principal,
                                        "grant_principal": subject_name,
                                        "grant_principal_kind": subject_kind,
                                        "inherited_groups": sorted(group_memberships[principal]),
                                        "scope": scope,
                                        "api_group": api_group,
                                        "resource": resource,
                                        "subresource": subresource if separator else "",
                                        "verb": verb,
                                        "resource_names": sorted(rule.get("resourceNames", [])),
                                    }
                                )
    return sorted(rows, key=canonical_bytes)


class AmbiguousNimDescendant(CatalogError):
    """A selector-less descendant needs its persisted NIM root to disambiguate."""


def _admission_deadline(seconds: float) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Apply one monotonic deadline below the API-server webhook budget."""

    def decorate(function: Callable[..., Any]) -> Callable[..., Any]:
        @wraps(function)
        async def bounded(*args: Any, **kwargs: Any) -> Any:
            uid = ""
            try:
                async with asyncio.timeout(seconds):
                    request = args[0] if args else kwargs.get("request")
                    if isinstance(request, Request):
                        review = await request.json()
                        if isinstance(review, Mapping) and isinstance(
                            review.get("request"), Mapping
                        ):
                            candidate = review["request"].get("uid")
                            uid = candidate if isinstance(candidate, str) else ""
                    return await function(*args, **kwargs)
            except TimeoutError:
                return JSONResponse(
                    {
                        "apiVersion": "admission.k8s.io/v1",
                        "kind": "AdmissionReview",
                        "response": {
                            "uid": uid,
                            "allowed": False,
                            "status": {
                                "code": 504,
                                "reason": "Timeout",
                                "message": "NIM admission exceeded its bounded end-to-end deadline",
                            },
                        },
                    }
                )

        return bounded

    return decorate


class NimAdmissionConfig:
    def __init__(self, value: object, *, catalog: Catalog) -> None:
        if not isinstance(value, Mapping) or set(value) != {
            "schema",
            "namespace",
            "security_session_id",
            "trusted_attestors",
            "admission_policy",
            "admission_policy_sha256",
            "security_boundary_envelope",
            "non_nim_controller_exemptions",
            "entries",
        }:
            raise ValueError("NIM admission configuration fields differ")
        if value["schema"] != CONFIG_SCHEMA or value["namespace"] != "fs2-models":
            raise ValueError("NIM admission configuration identity differs")
        if not isinstance(value["security_session_id"], str):
            raise ValueError("NIM admission security session is absent")
        trusted = value["trusted_attestors"]
        entries = value["entries"]
        policy_sha256 = value["admission_policy_sha256"]
        policy = value["admission_policy"]
        boundary_envelope = value["security_boundary_envelope"]
        if (
            not isinstance(trusted, Mapping)
            or not trusted
            or not isinstance(policy_sha256, str)
            or len(policy_sha256) != 64
            or any(character not in "0123456789abcdef" for character in policy_sha256)
            or not isinstance(policy, Mapping)
            or hashlib.sha256(canonical_bytes(policy)).hexdigest() != policy_sha256
            or not isinstance(entries, list)
        ):
            raise ValueError("NIM admission trust or entries are absent")
        if policy.get("schema") != POLICY_SCHEMA or set(policy) != {
            "schema",
            "name",
            "namespace",
            "failure_policy",
            "match_policy",
            "side_effects",
            "timeout_seconds",
            "admission_review_versions",
            "operations",
            "resources",
            "service",
            "ca_bundle_sha256",
            "tls",
            "owner_resolution",
            "owner_lookup_namespaces",
            "network_policy",
            "root_enrollment",
            "security_boundary",
        } or policy.get("root_enrollment") != {
            "namespace": "fs2-system",
            "name_prefix": "fs2-nim-root-",
            "storage_kind": "immutable-configmap-create-once",
            "reconciler": "persisted-root-readback",
            "reconcile_interval_seconds": 30,
            "admission_behavior": "verify-existing-deny-until-enrolled",
        }:
            raise ValueError("NIM admission root enrollment policy differs")
        network_policy = policy.get("network_policy")
        if (
            not isinstance(network_policy, Mapping)
            or set(network_policy) != {"webhook_source_cidrs", "kubernetes_api_cidrs"}
            or not all(
                isinstance(network_policy[field], list)
                and network_policy[field] == sorted(set(network_policy[field]))
                and bool(network_policy[field])
                and all(_valid_cidr(cidr) for cidr in network_policy[field])
                for field in ("webhook_source_cidrs", "kubernetes_api_cidrs")
            )
        ):
            raise ValueError("NIM admission network policy contract differs")
        self.namespace = str(value["namespace"])
        self.security_session_id = str(value["security_session_id"])
        self.trusted_attestors = {str(key): str(item) for key, item in trusted.items()}
        self.admission_policy_sha256 = policy_sha256
        self.admission_policy = dict(policy)
        if not isinstance(boundary_envelope, Mapping):
            raise ValueError("NIM admission external security boundary envelope is absent")
        self.security_boundary_envelope = dict(boundary_envelope)
        exemptions = value["non_nim_controller_exemptions"]
        if not isinstance(exemptions, list):
            raise ValueError("NIM non-NIM controller exemptions are absent")
        self.non_nim_controller_exemptions: list[Mapping[str, Any]] = []
        for exemption in exemptions:
            self._verify_non_nim_controller_exemption(exemption)
            self.non_nim_controller_exemptions.append(dict(exemption))
        self.entries: dict[tuple[str, str, str], Mapping[str, Any]] = {}
        self.entries_by_digest: dict[str, tuple[str, str, Mapping[str, Any]]] = {}
        self.entries_by_root: dict[tuple[str, str], Mapping[str, Any]] = {}
        self.descendant_candidates: list[
            tuple[str, str, Mapping[str, Any], frozenset[str], frozenset[str], str]
        ] = []
        self.descendant_actors: set[str] = set()
        self.descendant_images: set[str] = set()
        self.descendant_service_accounts: set[str] = set()
        for entry in entries:
            if not isinstance(entry, Mapping) or set(entry) != {
                "resource_kind",
                "model_id",
                "security_envelope",
            }:
                raise ValueError("NIM admission entry fields differ")
            kind = entry["resource_kind"]
            model_id = entry["model_id"]
            envelope = entry["security_envelope"]
            if kind not in {"NIMCache", "NIMService"} or not isinstance(model_id, str):
                raise ValueError("NIM admission entry identity differs")
            if not isinstance(envelope, Mapping) or not isinstance(envelope.get("subject_sha256"), str):
                raise ValueError("NIM admission entry envelope is absent")
            record = catalog.model(model_id)
            subject = envelope.get("subject")
            if not isinstance(subject, Mapping):
                raise ValueError("NIM admission subject is absent")
            actors = subject.get("actor_identities")
            descendants = subject.get("descendant_resources")
            if (
                not isinstance(actors, Mapping)
                or not isinstance(descendants, Mapping)
                or subject.get("admission_policy_sha256") != self.admission_policy_sha256
            ):
                raise ValueError("NIM admission actor/resource or policy binding is absent")
            key = (str(kind), model_id, str(envelope["subject_sha256"]))
            if key in self.entries:
                raise ValueError("NIM admission entry is duplicated")
            # The exact cryptographic envelope is verified by every real
            # request below. Loading the catalog here also rejects unknown IDs.
            self.entries[key] = envelope
            root_key = (str(kind), model_id)
            if root_key in self.entries_by_root:
                raise ValueError("NIM admission root identity is duplicated")
            self.entries_by_root[root_key] = envelope
            digest = str(envelope["subject_sha256"])
            if digest in self.entries_by_digest:
                raise ValueError("NIM admission subject selector is ambiguous")
            self.entries_by_digest[digest] = (str(kind), model_id, envelope)
            descendant_actor_keys = {
                str(contract["actor_identity"])
                for contract in descendants.values()
                if isinstance(contract, Mapping)
                and isinstance(contract.get("actor_identity"), str)
            }
            for actor_key in descendant_actor_keys:
                identity = actors.get(actor_key)
                if (
                    isinstance(identity, Mapping)
                    and identity.get("kind") == "pod-bound-service-account"
                    and isinstance(identity.get("username"), str)
                ):
                    self.descendant_actors.add(str(identity["username"]))
            image = subject.get("descendant_image")
            pod_spec = subject.get("pod_spec")
            if not isinstance(image, str) or not isinstance(pod_spec, Mapping):
                raise ValueError("NIM admission descendant identity is absent")
            service_account = pod_spec.get("serviceAccountName")
            if not isinstance(service_account, str) or not service_account:
                raise ValueError("NIM admission descendant service account is absent")
            self.descendant_images.add(image)
            self.descendant_service_accounts.add(service_account)
            signed_images = frozenset(
                str(contract["image"])
                for contract in subject.get("containers", {}).values()
                if isinstance(contract, Mapping)
                and isinstance(contract.get("image"), str)
            )
            actor_usernames = frozenset(
                str(identity["username"])
                for actor_key, identity in actors.items()
                if actor_key in descendant_actor_keys
                if isinstance(identity, Mapping)
                and identity.get("kind") == "pod-bound-service-account"
                and isinstance(identity.get("username"), str)
            )
            self.descendant_candidates.append(
                (str(kind), model_id, envelope, signed_images, actor_usernames, service_account)
            )
            if record.to_dict()["runtime"]["kind"] != "nim":
                raise ValueError("NIM admission entry names a non-NIM model")

    def _verify_non_nim_controller_exemption(self, envelope: object) -> None:
        if not isinstance(envelope, Mapping) or set(envelope) != {
            "subject",
            "subject_sha256",
            "authorization_id",
            "evidence_sha256",
            "attestation",
            "attestation_sha256",
        }:
            raise CatalogError("non-NIM controller exemption envelope differs")
        subject = envelope["subject"]
        if not isinstance(subject, Mapping) or set(subject) != {
            "schema",
            "model_id",
            "namespace",
            "controller",
            "descendant",
            "reason",
        }:
            raise CatalogError("non-NIM controller exemption subject differs")
        controller = subject["controller"]
        descendant = subject["descendant"]
        if (
            subject.get("schema") != NON_NIM_EXEMPTION_SCHEMA
            or subject.get("reason") != "cold-start-compatibility"
            or not isinstance(subject.get("model_id"), str)
            or not isinstance(subject.get("namespace"), str)
            or not isinstance(controller, Mapping)
            or set(controller)
            != {"api_version", "kind", "name", "uid", "projection_sha256"}
            or (controller.get("api_version"), controller.get("kind"))
            not in _RESOURCE_PATHS
            or controller.get("kind") in {"NIMCache", "NIMService", "Pod"}
            or not isinstance(descendant, Mapping)
            or set(descendant)
            != {"group", "version", "resource", "kind", "spec_sha256"}
            or re.fullmatch(r"[a-f0-9]{64}", str(controller.get("projection_sha256")))
            is None
            or re.fullmatch(r"[a-f0-9]{64}", str(descendant.get("spec_sha256"))) is None
        ):
            raise CatalogError("non-NIM controller exemption contract differs")
        subject_sha256 = hashlib.sha256(canonical_bytes(subject)).hexdigest()
        if (
            subject_sha256 != envelope["subject_sha256"]
            or hashlib.sha256(canonical_bytes(envelope["attestation"])).hexdigest()
            != envelope["attestation_sha256"]
        ):
            raise CatalogError("non-NIM controller exemption digest differs")
        verified = verify_signed_attestation(
            envelope["attestation"],
            trusted_attestors=self.trusted_attestors,
            expected_session_id=self.security_session_id,
            expected_kind="non-nim-controller-exemption",
            expected_schema=NON_NIM_EXEMPTION_SCHEMA,
            expected_digest="sha256:" + subject_sha256,
            expected_model_id=str(subject["model_id"]),
        )
        if verified["claims"] != {
            "authorization_id": envelope["authorization_id"],
            "decision": "accepted",
            "reviewer_role": "independent-platform-security",
            "evidence_sha256": envelope["evidence_sha256"],
        }:
            raise CatalogError("non-NIM controller exemption claims differ")

    def allows_non_nim_controller(
        self,
        *,
        review: Mapping[str, Any],
        owner_chain: Sequence[Mapping[str, Any]],
        model_id: str,
    ) -> bool:
        """Allow only an exact independently signed non-NIM controller tuple."""

        if not owner_chain:
            return False
        request = review.get("request")
        admitted = request.get("object") if isinstance(request, Mapping) else None
        resource = request.get("resource") if isinstance(request, Mapping) else None
        root = owner_chain[-1]
        metadata = root.get("metadata") if isinstance(root, Mapping) else None
        spec = admitted.get("spec") if isinstance(admitted, Mapping) else None
        if not isinstance(metadata, Mapping) or not isinstance(resource, Mapping) or not isinstance(spec, Mapping):
            return False
        projection = {
            "apiVersion": root.get("apiVersion"),
            "kind": root.get("kind"),
            "metadata": {
                "name": metadata.get("name"),
                "namespace": metadata.get("namespace"),
                "uid": metadata.get("uid"),
                "labels": metadata.get("labels", {}),
                "annotations": metadata.get("annotations", {}),
            },
            "spec": root.get("spec"),
        }
        controller_sha256 = hashlib.sha256(canonical_bytes(projection)).hexdigest()
        spec_sha256 = hashlib.sha256(canonical_bytes(spec)).hexdigest()
        for envelope in self.non_nim_controller_exemptions:
            self._verify_non_nim_controller_exemption(envelope)
            subject = envelope["subject"]
            controller = subject["controller"]
            descendant = subject["descendant"]
            if (
                subject["model_id"] == model_id
                and subject["namespace"] == metadata.get("namespace")
                and controller
                == {
                    "api_version": root.get("apiVersion"),
                    "kind": root.get("kind"),
                    "name": metadata.get("name"),
                    "uid": metadata.get("uid"),
                    "projection_sha256": controller_sha256,
                }
                and descendant
                == {
                    "group": resource.get("group"),
                    "version": resource.get("version"),
                    "resource": resource.get("resource"),
                    "kind": admitted.get("kind"),
                    "spec_sha256": spec_sha256,
                }
            ):
                return True
        return False

    def verify_security_boundary_authorization(
        self,
        *,
        renewed_envelope: Mapping[str, Any],
        renewal_metadata: Mapping[str, str],
    ) -> None:
        """Verify the independently renewed custody envelope on every pass.

        The immutable admission configuration carries only the activation copy.
        Readiness is gated by one immutable generation envelope.  A separately
        signed checkpoint and installation receipt bind the final Kubernetes
        UID/resourceVersion after creation; neither is embedded here.
        """

        boundary = self.admission_policy["security_boundary"]
        envelope = renewed_envelope
        if (
            not isinstance(boundary, Mapping)
            or not isinstance(envelope, Mapping)
            or set(envelope)
            != {
                "subject_sha256",
                "evidence_sha256",
                "attestation",
                "attestation_sha256",
                "provider_custody",
            }
            or envelope.get("subject_sha256") != boundary.get("subject_sha256")
            or hashlib.sha256(canonical_bytes(envelope.get("attestation"))).hexdigest()
            != envelope.get("attestation_sha256")
        ):
            raise CatalogError("NIM external security boundary authorization differs")
        verified = verify_signed_attestation(
            envelope["attestation"],
            trusted_attestors=self.trusted_attestors,
            expected_session_id=self.security_session_id,
            expected_kind="platform-admission-boundary",
            expected_schema=BOUNDARY_SCHEMA,
            expected_digest="sha256:" + str(boundary["subject_sha256"]),
            expected_model_id="platform",
        )
        if verified["claims"] != {
            "authorization_id": boundary["authorization_id"],
            "decision": "accepted",
            "reviewer_role": "independent-platform-security",
            "evidence_sha256": envelope["evidence_sha256"],
        }:
            raise CatalogError("NIM external security boundary authorization claims differ")
        provider_envelope = envelope["provider_custody"]
        if not isinstance(provider_envelope, Mapping) or set(provider_envelope) != {
            "subject", "subject_sha256", "evidence_sha256", "attestation", "attestation_sha256"
        }:
            raise CatalogError("NIM external provider custody envelope is absent")
        provider_subject = provider_envelope["subject"]
        if not isinstance(provider_subject, Mapping):
            raise CatalogError("NIM external provider custody subject is absent")
        security_principal, protected_security_principals = _validate_principal_epoch(
            provider_subject.get("principal_epoch")
        )
        collector = provider_subject.get("collector") if isinstance(provider_subject, Mapping) else None
        renewal = provider_subject.get("renewal")
        expected_namespaces = self.admission_policy["owner_lookup_namespaces"]
        expected_protected_objects = _protected_security_objects(self.admission_policy)
        if (
            not isinstance(provider_subject, Mapping)
            or set(provider_subject)
            != {
                "schema", "cluster_uid", "collector", "security_principal",
                "security_principal_kind",
                "security_credential_targets",
                "workload_release_principal", "security_principal_is_distinct",
                "denied_verbs", "denied_indirect_verbs",
                "owner_lookup_namespaces", "protected_objects",
                "webhook_source_cidrs",
                "principal_epoch", "renewal",
                "native_evidence", "native_evidence_authorization_id",
                "effective_permissions", "effective_permissions_sha256",
            }
            or provider_subject.get("schema") != PROVIDER_CUSTODY_SCHEMA
            or provider_subject.get("cluster_uid") != boundary.get("cluster_uid")
            or boundary.get("owner_lookup_namespaces") != expected_namespaces
            or provider_subject.get("principal_epoch") != boundary.get("principal_epoch")
            or provider_subject.get("security_principal") != security_principal
            or provider_subject.get("security_principal_kind") != "external-automation-user"
            or not isinstance(provider_subject.get("security_credential_targets"), list)
            or not provider_subject["security_credential_targets"]
            or provider_subject.get("workload_release_principal")
            != "system:serviceaccount:fs2-system:fs2-release-automation"
            or provider_subject.get("security_principal_is_distinct") is not True
            or provider_subject.get("denied_verbs")
            != ["create", "delete", "deletecollection", "patch", "update"]
            or provider_subject.get("denied_indirect_verbs")
            != ["bind", "escalate", "impersonate"]
            or provider_subject.get("owner_lookup_namespaces") != expected_namespaces
            or provider_subject.get("webhook_source_cidrs")
            != self.admission_policy["network_policy"]["webhook_source_cidrs"]
            or provider_subject.get("protected_objects") != expected_protected_objects
            or not isinstance(renewal, Mapping)
            or set(renewal)
            != {
                "schema", "namespace", "envelope_name_prefix",
                "checkpoint_name_prefix", "receipt_name_prefix",
                "head_name", "head_uid", "activation_head_generation",
                "activation_head_subject_sha256", "activation_head_chain_sha256",
                "controller_resource_id", "refresh_interval_seconds",
                "max_observation_age_seconds", "max_clock_skew_seconds",
                "generation", "previous_subject_sha256",
                "refreshed_at", "refresh_deadline",
            }
            or renewal.get("schema") != PROVIDER_RENEWAL_SCHEMA
            or {
                key: renewal.get(key)
                for key in (
                    "schema", "namespace", "envelope_name_prefix",
                    "checkpoint_name_prefix", "receipt_name_prefix",
                    "head_name", "head_uid", "activation_head_generation",
                    "activation_head_subject_sha256", "activation_head_chain_sha256",
                    "controller_resource_id", "refresh_interval_seconds",
                    "max_observation_age_seconds", "max_clock_skew_seconds",
                )
            } != {
                key: boundary.get("provider_renewal", {}).get(key)
                for key in (
                    "schema", "namespace", "envelope_name_prefix",
                    "checkpoint_name_prefix", "receipt_name_prefix",
                    "head_name", "head_uid", "activation_head_generation",
                    "activation_head_subject_sha256", "activation_head_chain_sha256",
                    "controller_resource_id", "refresh_interval_seconds",
                    "max_observation_age_seconds", "max_clock_skew_seconds",
                )
            }
            or not isinstance(renewal.get("generation"), int)
            or renewal["generation"] < 1
            or re.fullmatch(
                r"(?:|[a-f0-9]{64})", str(renewal.get("previous_subject_sha256"))
            ) is None
            or set(renewal_metadata) != {
                "name", "uid", "resource_version", "projection_sha256"
            }
            or re.fullmatch(r"[1-9][0-9]*", renewal_metadata.get("resource_version", ""))
            is None
            or re.fullmatch(
                r"[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}",
                renewal_metadata.get("uid", ""),
            )
            is None
            or re.fullmatch(r"[a-f0-9]{64}", renewal_metadata.get("projection_sha256", ""))
            is None
            or not isinstance(collector, Mapping)
            or set(collector)
            != {
                "method", "identity", "observed_at", "adapter_sha256", "tool_digest",
                "resource_ids", "complete", "page_count", "terminal_page_sha256",
            }
            or collector.get("method") != "provider-iam-live-enumeration/v1"
            or not isinstance(collector.get("identity"), str)
            or not collector["identity"]
            or not isinstance(collector.get("observed_at"), str)
            or re.fullmatch(
                r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z",
                collector["observed_at"],
            ) is None
            or not isinstance(collector.get("adapter_sha256"), str)
            or re.fullmatch(r"[a-f0-9]{64}", collector["adapter_sha256"]) is None
            or not isinstance(collector.get("tool_digest"), str)
            or re.fullmatch(r"sha256:[a-f0-9]{64}", collector["tool_digest"]) is None
            or not isinstance(collector.get("resource_ids"), list)
            or not collector["resource_ids"]
            or collector["resource_ids"] != sorted(set(collector["resource_ids"]))
            or any(not isinstance(item, str) or not item for item in collector["resource_ids"])
            or collector.get("complete") is not True
            or not isinstance(collector.get("page_count"), int)
            or not 1 <= collector["page_count"] <= 10000
            or not isinstance(collector.get("terminal_page_sha256"), str)
            or re.fullmatch(r"[a-f0-9]{64}", collector["terminal_page_sha256"]) is None
            or renewal.get("controller_resource_id") not in collector.get("resource_ids", [])
        ):
            raise CatalogError("NIM external provider custody subject differs")
        credential_targets = provider_subject["security_credential_targets"]
        for target in credential_targets:
            if (
                not isinstance(target, Mapping)
                or set(target) != {"api_group", "resource", "subresource", "scope", "name"}
                or any(
                    not isinstance(target[field], str) or not target[field]
                    for field in ("resource", "scope", "name")
                )
                or not isinstance(target["api_group"], str)
                or not isinstance(target["subresource"], str)
            ):
                raise CatalogError("NIM external security credential target differs")
        if (
            credential_targets != sorted(credential_targets, key=canonical_bytes)
            or len({canonical_bytes(target) for target in credential_targets})
            != len(credential_targets)
        ):
            raise CatalogError("NIM external security credential targets are not canonical")
        native_envelope = provider_subject["native_evidence"]
        if not isinstance(native_envelope, Mapping) or set(native_envelope) != {
            "subject", "subject_sha256", "evidence_sha256", "attestation",
            "attestation_sha256",
        }:
            raise CatalogError("NIM native access evidence envelope is absent")
        native_subject = native_envelope["subject"]
        if not isinstance(native_subject, Mapping) or set(native_subject) != {
            "schema", "cluster_uid", "observed_at", "adapter_sha256", "tool_digest",
            "provider_pages", "kubernetes_pages", "group_memberships",
            "scope_inventory", "principal_credentials", "complete",
        }:
            raise CatalogError("NIM native access evidence subject differs")
        native_sha256 = _sha256(canonical_bytes(native_subject))
        if (
            native_subject.get("schema") != NATIVE_ACCESS_EVIDENCE_SCHEMA
            or native_subject.get("cluster_uid") != boundary.get("cluster_uid")
            or native_subject.get("complete") is not True
            or native_sha256 != native_envelope.get("subject_sha256")
            or _sha256(canonical_bytes(native_envelope.get("attestation")))
            != native_envelope.get("attestation_sha256")
            or collector.get("observed_at") != native_subject.get("observed_at")
            or collector.get("adapter_sha256") != native_subject.get("adapter_sha256")
            or collector.get("tool_digest") != native_subject.get("tool_digest")
        ):
            raise CatalogError("NIM native access evidence digest differs")
        native_verified = verify_signed_attestation(
            native_envelope["attestation"],
            trusted_attestors=self.trusted_attestors,
            expected_session_id=self.security_session_id,
            expected_kind="provider-native-access-evidence",
            expected_schema=NATIVE_ACCESS_EVIDENCE_SCHEMA,
            expected_digest="sha256:" + native_sha256,
            expected_model_id="platform",
        )
        if native_verified["claims"] != {
            "authorization_id": provider_subject["native_evidence_authorization_id"],
            "decision": "accepted",
            "reviewer_role": "independent-access-evidence-collector",
            "evidence_sha256": native_envelope["evidence_sha256"],
        }:
            raise CatalogError("NIM native access evidence claims differ")
        native_memberships = native_subject.get("group_memberships")
        native_credentials = native_subject.get("principal_credentials")
        workload_principal = provider_subject["workload_release_principal"]
        all_evidence_principals = protected_security_principals | {workload_principal}
        if (
            not isinstance(native_memberships, Mapping)
            or set(native_memberships) != all_evidence_principals
            or not isinstance(native_credentials, Mapping)
            or set(native_credentials) != protected_security_principals
        ):
            raise CatalogError("NIM native principal closure differs")
        now = datetime.now(timezone.utc).replace(microsecond=0)
        overlap_by_principal = {
            item["principal"]: item
            for item in provider_subject["principal_epoch"]["overlap_principals"]
        }
        predecessor_by_principal = {
            item["principal"]: item
            for item in provider_subject["principal_epoch"]["retained_predecessors"]
        }
        for principal in sorted(protected_security_principals):
            credential = native_credentials.get(principal)
            if not isinstance(credential, Mapping) or set(credential) != {
                "principal", "provider_identity", "status", "usable", "not_after",
                "authentication_paths", "evidence_sha256",
            }:
                raise CatalogError("NIM native principal credential evidence differs")
            not_after = _parse_timestamp(
                credential.get("not_after"), field="principal credential expiry"
            )
            if (
                credential.get("principal") != principal
                or not isinstance(credential.get("provider_identity"), str)
                or not credential["provider_identity"]
                or not isinstance(credential.get("authentication_paths"), list)
                or credential["authentication_paths"]
                != sorted(set(credential["authentication_paths"]))
                or re.fullmatch(
                    r"[a-f0-9]{64}", str(credential.get("evidence_sha256"))
                )
                is None
            ):
                raise CatalogError("NIM native principal credential identity differs")
            if principal == security_principal:
                if credential.get("status") != "active" or credential.get("usable") is not True or not_after <= now:
                    raise CatalogError("NIM active security principal is not usable")
            elif principal in overlap_by_principal:
                overlap_expiry = _parse_timestamp(
                    overlap_by_principal[principal]["not_after"], field="principal overlap"
                )
                if (
                    credential.get("status") != "overlap"
                    or credential.get("usable") is not True
                    or not_after != overlap_expiry
                ):
                    raise CatalogError("NIM overlap principal credential differs")
            elif principal in predecessor_by_principal:
                predecessor = predecessor_by_principal[principal]
                retirement = _parse_timestamp(
                    predecessor["effective_at"], field="principal retirement"
                )
                if (
                    credential.get("status") not in {"denied", "expired"}
                    or credential.get("usable") is not False
                    or not_after > retirement
                    or credential.get("authentication_paths")
                ):
                    raise CatalogError("NIM predecessor credential remains usable")
        permissions = provider_subject["effective_permissions"]
        if not isinstance(permissions, list) or not permissions:
            raise CatalogError("NIM external provider custody permissions are absent")
        normalized_permissions: list[Mapping[str, Any]] = []
        for permission in permissions:
            if (
                not isinstance(permission, Mapping)
                or set(permission)
                != {
                    "authorization_source_id", "principal", "grant_principal",
                    "grant_principal_kind", "inherited_groups", "scope", "api_group",
                    "resource", "subresource", "verb", "resource_names",
                }
                or not all(
                    isinstance(permission[field], str) and permission[field]
                    for field in (
                        "authorization_source_id", "principal", "scope", "resource", "verb",
                        "grant_principal", "grant_principal_kind",
                    )
                )
                or not isinstance(permission["api_group"], str)
                or not isinstance(permission["subresource"], str)
                or permission["grant_principal_kind"] not in {"user", "group", "serviceaccount"}
                or not isinstance(permission["inherited_groups"], list)
                or permission["inherited_groups"]
                != sorted(set(permission["inherited_groups"]))
                or any(
                    not isinstance(group, str) or not group
                    for group in permission["inherited_groups"]
                )
                or (
                    permission["grant_principal_kind"] == "group"
                    and permission["grant_principal"] not in permission["inherited_groups"]
                )
                or (
                    permission["scope"] not in {"cluster", "all-namespaces", "all"}
                    and re.fullmatch(
                        r"namespace:[a-z0-9](?:[-a-z0-9.]*[a-z0-9])?",
                        permission["scope"],
                    )
                    is None
                )
                or permission["principal"]
                not in protected_security_principals
                | {provider_subject["workload_release_principal"]}
                or not isinstance(permission["resource_names"], list)
                or permission["resource_names"] != sorted(set(permission["resource_names"]))
                or any(not isinstance(name, str) or not name for name in permission["resource_names"])
            ):
                raise CatalogError("NIM external provider custody permission row differs")
            normalized_permissions.append(dict(permission))
        canonical_rows = sorted(
            normalized_permissions,
            key=lambda row: canonical_bytes(row),
        )
        derived_rows = _native_permission_rows(
            native_subject,
            principals=protected_security_principals
            | {provider_subject["workload_release_principal"]},
            collector=collector,
        )
        permissions_sha256 = hashlib.sha256(canonical_bytes(canonical_rows)).hexdigest()
        if (
            normalized_permissions != canonical_rows
            or len({canonical_bytes(row) for row in canonical_rows}) != len(canonical_rows)
            or canonical_rows != derived_rows
            or provider_subject.get("effective_permissions_sha256") != permissions_sha256
        ):
            raise CatalogError("NIM external provider custody permission inventory differs")
        denied_verbs = set(provider_subject["denied_verbs"])
        mandatory_credential_targets = [
            {
                "api_group": "",
                "resource": "secrets",
                "subresource": "",
                "scope": "namespace:fs2-system",
                "name": self.admission_policy["tls"]["secret_name"],
            },
            {
                "api_group": "",
                "resource": "serviceaccounts",
                "subresource": "token",
                "scope": "namespace:fs2-system",
                "name": "fs2-serve-control-plane-nim-admission",
            },
        ]
        if any(target not in credential_targets for target in mandatory_credential_targets):
            raise CatalogError("NIM mandatory credential targets are absent")
        for permission in canonical_rows:
            principal = permission["principal"]
            forbidden = False
            if principal == workload_principal:
                forbidden = permission["verb"] in denied_verbs | {"*"} and any(
                    _permission_reaches_protected_object(permission, protected)
                    for protected in expected_protected_objects
                )
                if permission["verb"] in {"*", "bind", "escalate", "impersonate"}:
                    forbidden = True
                for protected_principal in protected_security_principals:
                    if _permission_impersonates_security_principal(
                        permission,
                        principal=protected_principal,
                        groups=set(native_memberships[protected_principal]),
                    ):
                        forbidden = True
                for target in credential_targets:
                    if _permission_reaches_credential_target(permission, target):
                        forbidden = True
            elif principal in predecessor_by_principal:
                # Retained epochs are evidence only. They may not retain any
                # provider or Kubernetes authorization path after retirement.
                forbidden = True
            if forbidden:
                raise CatalogError("a denied principal retains a security-boundary permission")
        provider_sha256 = hashlib.sha256(canonical_bytes(provider_subject)).hexdigest()
        renewal_contract = boundary["provider_renewal"]
        if (
            provider_sha256 != provider_envelope.get("subject_sha256")
            or hashlib.sha256(canonical_bytes(provider_envelope.get("attestation"))).hexdigest()
            != provider_envelope.get("attestation_sha256")
        ):
            raise CatalogError("NIM external provider custody digest differs")
        if (
            renewal["generation"] < renewal_contract["activation_generation"]
            or (
                renewal["generation"] == renewal_contract["activation_generation"]
                and (
                    provider_sha256
                    != renewal_contract["activation_subject_sha256"]
                    or renewal_metadata["name"] != renewal_contract["activation_envelope_name"]
                    or renewal_metadata["uid"] != renewal_contract["activation_envelope_uid"]
                    or renewal_metadata["resource_version"]
                    != renewal_contract["activation_envelope_resource_version"]
                    or renewal_metadata["projection_sha256"]
                    != renewal_contract["activation_envelope_projection_sha256"]
                )
            )
            or (
                renewal["generation"] > renewal_contract["activation_generation"]
                and not renewal["previous_subject_sha256"]
            )
        ):
            raise CatalogError("NIM provider renewal is below its activation floor")
        provider_verified = verify_signed_attestation(
            provider_envelope["attestation"],
            trusted_attestors=self.trusted_attestors,
            expected_session_id=self.security_session_id,
            expected_kind="provider-admission-custody",
            expected_schema=PROVIDER_CUSTODY_SCHEMA,
            expected_digest="sha256:" + provider_sha256,
            expected_model_id="platform",
        )
        if provider_verified["claims"] != {
            "authorization_id": boundary["provider_authorization_id"],
            "decision": "accepted",
            "reviewer_role": "independent-platform-security",
            "evidence_sha256": provider_envelope["evidence_sha256"],
        }:
            raise CatalogError("NIM external provider custody claims differ")
        observed_at = _parse_timestamp(collector["observed_at"], field="provider observation")
        issued_at = _parse_timestamp(provider_verified["issued_at"], field="provider authorization")
        refreshed_at = _parse_timestamp(renewal["refreshed_at"], field="provider renewal")
        refresh_deadline = _parse_timestamp(
            renewal["refresh_deadline"], field="provider renewal deadline"
        )
        now = datetime.now(timezone.utc).replace(microsecond=0)
        max_age = timedelta(seconds=renewal["max_observation_age_seconds"])
        clock_skew = timedelta(seconds=renewal["max_clock_skew_seconds"])
        if (
            observed_at > now + clock_skew
            or now - observed_at > max_age
            or abs(issued_at - observed_at) > clock_skew
            or abs(refreshed_at - observed_at) > clock_skew
            or refresh_deadline <= now
            or refresh_deadline - refreshed_at > max_age
        ):
            raise CatalogError("NIM external provider custody observation is stale")
        self.accepted_provider_renewal_candidate = {
            "generation": renewal["generation"],
            "subject_sha256": provider_sha256,
            "previous_subject_sha256": renewal["previous_subject_sha256"],
            "envelope_name": renewal_metadata["name"],
            "envelope_uid": renewal_metadata["uid"],
            "envelope_resource_version": renewal_metadata["resource_version"],
            "envelope_projection_sha256": renewal_metadata["projection_sha256"],
        }
        expected_envelope_name = (
            f"{renewal_contract['envelope_name_prefix']}"
            f"{renewal['generation']}-{provider_sha256[:16]}"
        )
        if renewal_metadata["name"] != expected_envelope_name:
            raise CatalogError("NIM provider renewal envelope name differs")

    def verify_installation_receipt_authorization(
        self, *, receipt_envelope: Mapping[str, Any], expected_handoff_sha256: str
    ) -> None:
        """Verify the long-lived installed-object identity independently of renewal."""

        boundary = self.admission_policy["security_boundary"]
        if not isinstance(receipt_envelope, Mapping) or set(receipt_envelope) != {
            "subject", "subject_sha256", "evidence_sha256", "attestation", "attestation_sha256"
        }:
            raise CatalogError("NIM live installation receipt envelope is absent")
        receipt = receipt_envelope["subject"]
        if not isinstance(receipt, Mapping) or set(receipt) != {
            "schema", "cluster_uid", "security_handoff_sha256", "installed_at",
            "owner_lookup_namespaces", "network_policy", "tls", "principal_epoch",
            "provider_head", "apply_fence", "objects",
        }:
            raise CatalogError("NIM live installation receipt fields differ")
        installed_at = _parse_timestamp(
            receipt.get("installed_at"), field="installation receipt creation"
        )
        now = datetime.now(timezone.utc).replace(microsecond=0)
        clock_skew = timedelta(
            seconds=boundary["provider_renewal"]["max_clock_skew_seconds"]
        )
        objects = receipt.get("objects")
        if (
            receipt.get("schema")
            != INSTALLATION_RECEIPT_SCHEMA
            or receipt.get("cluster_uid") != boundary.get("cluster_uid")
            or receipt.get("security_handoff_sha256") != expected_handoff_sha256
            or receipt.get("owner_lookup_namespaces")
            != self.admission_policy["owner_lookup_namespaces"]
            or receipt.get("network_policy") != self.admission_policy["network_policy"]
            or receipt.get("tls") != self.admission_policy["tls"]
            or receipt.get("principal_epoch") != boundary.get("principal_epoch")
            or receipt.get("provider_head")
            != {
                "name": boundary["provider_renewal"]["head_name"],
                "uid": boundary["provider_renewal"]["head_uid"],
                "schema": PROVIDER_HEAD_SCHEMA,
                "update_policy": "external-cas-monotonic-signed",
                "bounded_generations": 2,
            }
            or receipt.get("apply_fence")
            != {
                "lease_name": "fs2-nim-admission-apply-fence",
                "policy_name": "fs2-nim-admission-apply-fence",
                "binding_name": "fs2-nim-admission-apply-fence",
                "authorization_name_prefix": "fs2-nim-apply-authorization-",
                "authorization_schema": "fs2-serve.nebius.ai/nim-admission-apply-fence/v1",
                "lifecycle": "immutable-generation-retained-no-delete",
            }
            or installed_at > now + clock_skew
            or not isinstance(objects, list)
            or not objects
        ):
            raise CatalogError("NIM live installation receipt is stale or differs")
        object_keys: set[tuple[str, str, str, str]] = set()
        for item in objects:
            if (
                not isinstance(item, Mapping)
                or set(item)
                != {
                    "api_version", "kind", "namespace", "name", "uid",
                    "resource_version", "generation", "projection_sha256",
                }
                or not all(
                    isinstance(item.get(field), str) and item[field]
                    for field in (
                        "api_version", "kind", "name", "uid", "resource_version",
                        "generation", "projection_sha256",
                    )
                )
                or not isinstance(item.get("namespace"), str)
                or re.fullmatch(r"[a-f0-9]{64}", item["projection_sha256"]) is None
            ):
                raise CatalogError("NIM live installation receipt object differs")
            object_keys.add(
                (item["api_version"], item["kind"], item["namespace"], item["name"])
            )
        if len(object_keys) != len(objects):
            raise CatalogError("NIM live installation receipt object inventory is duplicated")
        receipt_sha256 = _sha256(canonical_bytes(receipt))
        if (
            receipt_sha256 != receipt_envelope.get("subject_sha256")
            or _sha256(canonical_bytes(receipt_envelope.get("attestation")))
            != receipt_envelope.get("attestation_sha256")
        ):
            raise CatalogError("NIM live installation receipt digest differs")
        receipt_verified = verify_signed_attestation(
            receipt_envelope["attestation"],
            trusted_attestors=self.trusted_attestors,
            expected_session_id=self.security_session_id,
            expected_kind="nim-admission-installation-receipt",
            expected_schema=str(receipt["schema"]),
            expected_digest="sha256:" + receipt_sha256,
            expected_model_id="platform",
        )
        if receipt_verified["claims"] != {
            "authorization_id": boundary["installation_receipt_authorization_id"],
            "decision": "accepted",
            "reviewer_role": "independent-platform-security",
            "evidence_sha256": receipt_envelope["evidence_sha256"],
        }:
            raise CatalogError("NIM live installation receipt claims differ")
        if _parse_timestamp(
            receipt_verified["issued_at"], field="installation receipt attestation"
        ) != installed_at:
            raise CatalogError("NIM installation receipt issuance differs")
        self.live_installation_receipt = dict(receipt)

    @classmethod
    def load(cls, path: Path, *, catalog_dir: Path) -> "NimAdmissionConfig":
        raw = path.read_bytes()
        if not raw or len(raw) > 4 * 1024 * 1024:
            raise ValueError("NIM admission configuration is empty or too large")
        return cls(
            json.loads(raw, object_pairs_hook=_unique_object),
            catalog=load_catalog(catalog_dir),
        )

    def select(
        self,
        review: Mapping[str, Any],
        *,
        catalog: Catalog,
        root_kind_hint: str | None = None,
    ) -> tuple[str, str, Mapping[str, Any]] | None:
        request = review.get("request")
        if not isinstance(request, Mapping):
            raise CatalogError("NIM admission request is absent")
        admitted = request.get("object")
        user = request.get("userInfo")
        if not isinstance(admitted, Mapping) or not isinstance(user, Mapping):
            raise CatalogError("NIM admission object or actor is absent")
        metadata = admitted.get("metadata")
        if not isinstance(metadata, Mapping):
            raise CatalogError("NIM admission metadata is absent")
        annotations = metadata.get("annotations", {})
        digest = annotations.get(ENVELOPE_ANNOTATION) if isinstance(annotations, Mapping) else None
        resource = request.get("resource")
        if not isinstance(resource, Mapping):
            raise CatalogError("NIM admission resource is absent")
        if resource.get("group") == "apps.nvidia.com" and resource.get("resource") in {
            "nimcaches",
            "nimservices",
        }:
            kind = "NIMCache" if resource["resource"] == "nimcaches" else "NIMService"
            model_id = metadata.get("name")
            if not isinstance(model_id, str) or not isinstance(digest, str):
                raise CatalogError("NIM custom resource lacks its signed envelope selector")
        elif (resource.get("group"), resource.get("version"), resource.get("resource")) in {
            ("", "v1", "pods"),
            ("apps", "v1", "deployments"),
            ("apps", "v1", "replicasets"),
            ("apps", "v1", "statefulsets"),
            ("batch", "v1", "jobs"),
        }:
            username = user.get("username")
            spec = admitted.get("spec")
            if not isinstance(spec, Mapping):
                raise CatalogError("NIM descendant spec is absent")
            template = spec.get("template")
            pod_spec = (
                template.get("spec")
                if isinstance(template, Mapping) and isinstance(template.get("spec"), Mapping)
                else spec
            )
            template_metadata = (
                template.get("metadata") if isinstance(template, Mapping) else None
            )
            template_annotations = (
                template_metadata.get("annotations", {})
                if isinstance(template_metadata, Mapping)
                else {}
            )
            template_digest = (
                template_annotations.get(ENVELOPE_ANNOTATION)
                if isinstance(template_annotations, Mapping)
                else None
            )
            if digest is not None and template_digest is not None and digest != template_digest:
                raise CatalogError("NIM descendant object and Pod-template selectors differ")
            digest = digest or template_digest
            images = {
                container.get("image")
                for container_class in ("initContainers", "containers", "ephemeralContainers")
                for container in pod_spec.get(container_class, [])
                if isinstance(container, Mapping) and isinstance(container.get("image"), str)
            }
            attributed = (
                isinstance(digest, str)
                or bool(images.intersection(self.descendant_images))
                or pod_spec.get("serviceAccountName") in self.descendant_service_accounts
                or username in self.descendant_actors
            )
            if not attributed:
                return None
            if isinstance(digest, str):
                selected = self.entries_by_digest.get(digest)
                if selected is None:
                    raise CatalogError("NIM descendant envelope selector is not configured")
            else:
                candidates = [
                    (kind, candidate_model_id, candidate_envelope)
                    for (
                        kind,
                        candidate_model_id,
                        candidate_envelope,
                        signed_images,
                        actor_usernames,
                        service_account,
                    ) in self.descendant_candidates
                    if bool(images.intersection(signed_images))
                    or username in actor_usernames
                    or pod_spec.get("serviceAccountName") == service_account
                    if root_kind_hint is None or kind == root_kind_hint
                ]
                if len(candidates) != 1:
                    if len(candidates) > 1 and root_kind_hint is None:
                        raise AmbiguousNimDescendant(
                            "NIM descendant needs its persisted root for attribution"
                        )
                    raise CatalogError(
                        "NIM descendant without a propagated selector is not uniquely attributable"
                    )
                selected = candidates[0]
            kind, model_id, envelope = selected
            catalog.model(model_id)
            return kind, model_id, envelope
        else:
            raise CatalogError("NIM admission request targets an unsupported resource")
        key = (kind, model_id, digest)
        envelope = self.entries.get(key)
        if envelope is None:
            raise CatalogError("NIM admission envelope selector is not configured")
        catalog.model(model_id)
        return kind, model_id, envelope

    def select_persisted_root(
        self, root: Mapping[str, Any], *, catalog: Catalog
    ) -> tuple[str, str, Mapping[str, Any]]:
        """Select authority only from a live, UID-resolved NIM root."""

        kind = root.get("kind")
        metadata = root.get("metadata")
        model_id = metadata.get("name") if isinstance(metadata, Mapping) else None
        if (
            kind not in {"NIMCache", "NIMService"}
            or not isinstance(model_id, str)
            or not isinstance(metadata, Mapping)
            or metadata.get("namespace") != self.namespace
        ):
            raise CatalogError("persisted NIM root identity is incomplete")
        envelope = self.entries_by_root.get((str(kind), model_id))
        if envelope is None:
            raise CatalogError("persisted NIM root has no signed admission envelope")
        catalog.model(model_id)
        return str(kind), model_id, envelope


class KubernetesNimOwnerResolver:
    """Resolve every dynamic UID edge against persisted Kubernetes objects."""

    def __init__(
        self,
        *,
        base_url: str,
        token_file: Path,
        ca_file: Path,
        timeout_seconds: float = 3.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.token_file = token_file
        self._owns_client = client is None
        self.client = client or httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            verify=str(ca_file),
            timeout=httpx.Timeout(timeout_seconds),
            follow_redirects=False,
            trust_env=False,
        )

    async def close(self) -> None:
        if self._owns_client:
            await self.client.aclose()

    def _headers(self) -> dict[str, str]:
        try:
            token = self.token_file.read_text(encoding="utf-8").strip()
        except OSError as error:
            raise CatalogError("NIM owner resolver credential is unavailable") from error
        if len(token) < 16:
            raise CatalogError("NIM owner resolver credential is unavailable")
        return {"Authorization": f"Bearer {token}", "Accept": "application/json"}

    async def verify_apply_fence(
        self, *, review: Mapping[str, Any], config: NimAdmissionConfig
    ) -> None:
        """Enforce the signed, fresh apply authority at the actual NIM root admission."""

        request = review.get("request")
        admitted = request.get("object") if isinstance(request, Mapping) else None
        metadata = admitted.get("metadata") if isinstance(admitted, Mapping) else None
        annotations = metadata.get("annotations") if isinstance(metadata, Mapping) else None
        user = request.get("userInfo") if isinstance(request, Mapping) else None
        if not isinstance(annotations, Mapping) or not isinstance(user, Mapping):
            raise CatalogError("NIM root mutation lacks apply-fence identity")
        nonce = annotations.get("fs2-serve.nebius.ai/apply-fence-nonce")
        try:
            lease_response = await self.client.get(
                "/apis/coordination.k8s.io/v1/namespaces/fs2-system/leases/"
                "fs2-nim-admission-apply-fence",
                headers=self._headers(),
            )
        except (OSError, httpx.HTTPError) as error:
            raise CatalogError("NIM apply-fence Lease lookup failed") from error
        if lease_response.status_code != 200 or len(lease_response.content) > 256 * 1024:
            raise CatalogError("NIM apply-fence Lease is absent")
        try:
            lease = lease_response.json()
        except ValueError as error:
            raise CatalogError("NIM apply-fence Lease is invalid") from error
        lease_metadata = lease.get("metadata") if isinstance(lease, Mapping) else None
        lease_annotations = (
            lease_metadata.get("annotations") if isinstance(lease_metadata, Mapping) else None
        )
        lease_spec = lease.get("spec") if isinstance(lease, Mapping) else None
        if (
            not isinstance(lease_metadata, Mapping)
            or lease_metadata.get("namespace") != "fs2-system"
            or lease_metadata.get("name") != "fs2-nim-admission-apply-fence"
            or lease_metadata.get("labels", {}).get("fs2-serve.nebius.ai/apply-fence")
            != "true"
            or not isinstance(lease_annotations, Mapping)
            or not isinstance(lease_spec, Mapping)
            or lease_spec.get("holderIdentity") != nonce
            or lease_annotations.get("fs2-serve.nebius.ai/apply-fence-principal")
            != user.get("username")
        ):
            raise CatalogError("NIM apply-fence Lease identity differs")
        now = datetime.now(timezone.utc).replace(microsecond=0)
        renew_time = _parse_timestamp(lease_spec.get("renewTime"), field="apply-fence renewal")
        valid_until = _parse_timestamp(
            lease_annotations.get("fs2-serve.nebius.ai/apply-fence-valid-until"),
            field="apply-fence validity",
        )
        duration = lease_spec.get("leaseDurationSeconds")
        authorization_name = lease_annotations.get(
            "fs2-serve.nebius.ai/apply-fence-authorization"
        )
        if (
            not isinstance(duration, int)
            or not 30 <= duration <= 300
            or renew_time + timedelta(seconds=duration) <= now
            or valid_until <= now
            or not isinstance(authorization_name, str)
            or re.fullmatch(r"fs2-nim-apply-authorization-[a-f0-9]{16}", authorization_name)
            is None
        ):
            raise CatalogError("NIM apply-fence Lease is stale")
        authorization_response = await self.client.get(
            "/api/v1/namespaces/fs2-system/configmaps/"
            + quote(authorization_name, safe=""),
            headers=self._headers(),
        )
        if authorization_response.status_code != 200 or len(authorization_response.content) > 1024 * 1024:
            raise CatalogError("NIM apply-fence authorization is absent")
        try:
            record = authorization_response.json()
            data = record["data"]
            envelope = json.loads(data["envelope.json"], object_pairs_hook=_unique_object)
        except (KeyError, TypeError, ValueError) as error:
            raise CatalogError("NIM apply-fence authorization is invalid") from error
        record_metadata = record.get("metadata")
        subject = envelope.get("subject") if isinstance(envelope, Mapping) else None
        expected_subject_fields = {
            "schema", "cluster_uid", "security_session_id", "plan_sha256",
            "terraform_sha256", "capsule_manifest_sha256", "plan_actions",
            "plan_actions_sha256", "nonce", "issued_at", "valid_until",
            "security_handoff_sha256", "receipt_name", "receipt_subject_sha256",
            "provider_generation", "provider_subject_sha256", "principal",
            "lease_name", "lease_uid", "lease_duration_seconds", "objects",
            "authorization_record_name", "allowed_root_actions",
        }
        expected_record_name = (
            "fs2-nim-apply-authorization-" + _sha256(str(nonce).encode())[:16]
        )
        if (
            not isinstance(envelope, Mapping)
            or set(envelope) != {
                "subject", "subject_sha256", "evidence_sha256", "attestation",
                "attestation_sha256",
            }
            or record.get("immutable") is not True
            or not isinstance(record_metadata, Mapping)
            or record_metadata.get("name") != authorization_name
            or record_metadata.get("namespace") != "fs2-system"
            or record_metadata.get("labels") != {
                "app.kubernetes.io/managed-by": "platform-security",
                "fs2-serve.nebius.ai/immutable-security-boundary": "true",
                "fs2-serve.nebius.ai/apply-fence-authorization": "true",
            }
            or not isinstance(data, Mapping)
            or set(data) != {"envelope.json", "envelope.sha256"}
            or data.get("envelope.json") != canonical_bytes(envelope).decode("utf-8")
            or _sha256(canonical_bytes(envelope)) != data.get("envelope.sha256")
            or not isinstance(subject, Mapping)
            or set(subject) != expected_subject_fields
            or subject.get("schema") != "fs2-serve.nebius.ai/nim-admission-apply-fence/v1"
            or envelope.get("subject_sha256") != _sha256(canonical_bytes(subject))
            or _sha256(canonical_bytes(envelope.get("attestation")))
            != envelope.get("attestation_sha256")
            or subject.get("nonce") != nonce
            or subject.get("authorization_record_name") != expected_record_name
            or authorization_name != expected_record_name
            or subject.get("valid_until") != valid_until.isoformat().replace("+00:00", "Z")
            or subject.get("lease_name") != "fs2-nim-admission-apply-fence"
            or subject.get("lease_uid") != lease_metadata.get("uid")
            or subject.get("cluster_uid")
            != config.admission_policy["security_boundary"]["cluster_uid"]
            or subject.get("security_session_id") != config.security_session_id
            or re.fullmatch(
                r"[a-f0-9]{64}", str(subject.get("security_handoff_sha256"))
            )
            is None
            or subject.get("principal") != user.get("username")
            or lease_annotations.get("fs2-serve.nebius.ai/apply-fence-subject-sha256")
            != envelope.get("subject_sha256")
        ):
            raise CatalogError("NIM apply-fence authorization binding differs")
        issued_at = _parse_timestamp(subject.get("issued_at"), field="apply-fence issue")
        if issued_at > now or valid_until - issued_at > timedelta(minutes=5):
            raise CatalogError("NIM apply-fence authorization is stale")
        verified = verify_signed_attestation(
            envelope["attestation"],
            trusted_attestors=config.trusted_attestors,
            expected_session_id=config.security_session_id,
            expected_kind="nim-admission-apply-fence",
            expected_schema="fs2-serve.nebius.ai/nim-admission-apply-fence/v1",
            expected_digest="sha256:" + str(envelope["subject_sha256"]),
            expected_model_id="platform",
        )
        if verified["claims"] != {
            "authorization_id": "nim-admission-apply-fence",
            "decision": "accepted",
            "reviewer_role": "external-platform-security",
            "evidence_sha256": envelope["evidence_sha256"],
        }:
            raise CatalogError("NIM apply-fence authorization claims differ")
        request_resource = request.get("resource")
        operation = request.get("operation")
        request_namespace = request.get("namespace")
        request_name = request.get("name")
        old_object = request.get("oldObject")
        if (
            not isinstance(request_resource, Mapping)
            or operation not in {"CREATE", "UPDATE"}
            or not isinstance(subject.get("allowed_root_actions"), list)
            or subject["allowed_root_actions"]
            != sorted(subject["allowed_root_actions"], key=canonical_bytes)
        ):
            raise CatalogError("NIM apply-fence root action inventory differs")
        try:
            projection_sha256 = _sha256(
                canonical_bytes(canonical_nim_root_projection(admitted))
            )
            admitted_finalizers = canonical_nim_root_finalizers(admitted)
            old_projection_sha256 = (
                _sha256(
                    canonical_bytes(
                        canonical_nim_root_projection(
                            old_object, require_defaults_closed=False
                        )
                    )
                )
                if isinstance(old_object, Mapping)
                else "absent"
            )
            old_finalizers = (
                canonical_nim_root_finalizers(old_object)
                if isinstance(old_object, Mapping)
                else []
            )
            old_finalizers_sha256 = (
                _sha256(canonical_bytes(old_finalizers))
                if isinstance(old_object, Mapping)
                else "absent"
            )
        except ValueError as error:
            raise CatalogError("NIM root canonical projection differs") from error
        matching_actions = [
            action
            for action in subject["allowed_root_actions"]
            if isinstance(action, Mapping)
            and action.get("api_group") == request_resource.get("group")
            and action.get("api_version") == request_resource.get("version")
            and action.get("resource") == request_resource.get("resource")
            and action.get("kind") == admitted.get("kind")
            and action.get("namespace") == request_namespace
            and action.get("name") == request_name
            and action.get("operation") == operation
            and action.get("after_projection_sha256") == projection_sha256
            and action.get("replay_semantics")
            == "idempotent-exact-persistence-precondition-and-projection"
        ]
        if len(matching_actions) != 1:
            raise CatalogError("NIM root mutation is outside the signed apply plan")
        action = matching_actions[0]
        if set(action) != {
            "action_id", "api_group", "api_version", "resource", "kind",
            "namespace", "name", "operation", "expected_uid",
            "expected_resource_version", "before_projection_sha256",
            "before_finalizers_sha256", "after_projection_sha256",
            "replay_semantics",
        }:
            raise CatalogError("NIM signed root action fields differ")
        action_identity = {
            key: action[key]
            for key in (
                "api_group", "api_version", "resource", "kind", "namespace", "name",
                "operation", "expected_uid", "expected_resource_version",
                "before_projection_sha256", "before_finalizers_sha256",
                "after_projection_sha256",
            )
        }
        admitted_metadata = admitted.get("metadata")
        old_metadata = old_object.get("metadata") if isinstance(old_object, Mapping) else None
        if (
            action.get("action_id") != _sha256(canonical_bytes(action_identity))
            or (
                operation == "CREATE"
                and (
                    old_object is not None
                    or action.get("expected_uid") != "absent"
                    or action.get("expected_resource_version") != "absent"
                    or action.get("before_projection_sha256") != "absent"
                    or action.get("before_finalizers_sha256") != "absent"
                    or admitted_finalizers
                )
            )
            or (
                operation == "UPDATE"
                and (
                    not isinstance(admitted_metadata, Mapping)
                    or not isinstance(old_metadata, Mapping)
                    or admitted_metadata.get("uid") != action.get("expected_uid")
                    or old_metadata.get("uid") != action.get("expected_uid")
                    or admitted_metadata.get("resourceVersion")
                    != action.get("expected_resource_version")
                    or old_metadata.get("resourceVersion")
                    != action.get("expected_resource_version")
                    or action.get("before_projection_sha256")
                    != old_projection_sha256
                    or action.get("before_finalizers_sha256")
                    != old_finalizers_sha256
                    or admitted_finalizers != old_finalizers
                )
            )
        ):
            raise CatalogError("NIM root persistence precondition differs")

    async def get(
        self,
        *,
        api_version: str,
        kind: str,
        namespace: str,
        name: str,
        expected_uid: str,
    ) -> Mapping[str, Any]:
        pattern = _RESOURCE_PATHS.get((api_version, kind))
        if pattern is None:
            raise CatalogError("NIM owner resolver received an unsupported GVK")
        path = pattern.format(
            namespace=quote(namespace, safe=""),
            name=quote(name, safe=""),
        )
        try:
            response = await self.client.get(path, headers=self._headers())
        except (OSError, httpx.HTTPError) as error:
            raise CatalogError("NIM owner resolver Kubernetes request failed") from error
        if response.status_code != 200 or len(response.content) > 4 * 1024 * 1024:
            raise CatalogError("NIM owner resolver could not bind the persisted object")
        try:
            value = response.json()
        except ValueError as error:
            raise CatalogError("NIM owner resolver returned invalid JSON") from error
        metadata = value.get("metadata") if isinstance(value, Mapping) else None
        if (
            not isinstance(value, Mapping)
            or value.get("apiVersion") != api_version
            or value.get("kind") != kind
            or not isinstance(metadata, Mapping)
            or metadata.get("namespace") != namespace
            or metadata.get("name") != name
            or metadata.get("uid") != expected_uid
        ):
            raise CatalogError("NIM owner resolver object identity differs")
        return value

    async def _listed_immutable_configmaps(
        self, *, label_selector: str, maximum_bytes: int = 8 * 1024 * 1024
    ) -> list[Mapping[str, Any]]:
        """List externally owned immutable records without following a pointer."""

        try:
            response = await self.client.get(
                "/api/v1/namespaces/fs2-system/configmaps?labelSelector="
                + quote(label_selector, safe="=,"),
                headers=self._headers(),
            )
        except (OSError, httpx.HTTPError) as error:
            raise CatalogError("NIM immutable generation inventory failed") from error
        if response.status_code != 200 or len(response.content) > maximum_bytes:
            raise CatalogError("NIM immutable generation inventory is unavailable")
        try:
            listing = response.json()
        except ValueError as error:
            raise CatalogError("NIM immutable generation inventory is invalid") from error
        items = listing.get("items") if isinstance(listing, Mapping) else None
        if not isinstance(items, list) or len(items) > 10000:
            raise CatalogError("NIM immutable generation inventory cardinality differs")
        return items

    async def _configmap_by_name(self, name: str) -> Mapping[str, Any]:
        if re.fullmatch(r"[a-z0-9](?:[-a-z0-9.]*[a-z0-9])?", name) is None:
            raise CatalogError("NIM security record name is invalid")
        try:
            response = await self.client.get(
                "/api/v1/namespaces/fs2-system/configmaps/" + quote(name, safe=""),
                headers=self._headers(),
            )
        except (OSError, httpx.HTTPError) as error:
            raise CatalogError("NIM security record lookup failed") from error
        if response.status_code != 200 or len(response.content) > 1024 * 1024:
            raise CatalogError("NIM security record is absent")
        try:
            value = response.json()
        except ValueError as error:
            raise CatalogError("NIM security record is invalid") from error
        if not isinstance(value, Mapping):
            raise CatalogError("NIM security record is not an object")
        return value

    async def _provider_renewal_head(
        self, *, config: NimAdmissionConfig
    ) -> tuple[list[Mapping[str, Any]], str, Mapping[str, Any]]:
        """Return the bounded provider set and its one handoff-bound receipt."""

        boundary = config.admission_policy["security_boundary"]
        contract = boundary["provider_renewal"]
        value = await self._configmap_by_name(str(contract["head_name"]))
        metadata = value.get("metadata")
        annotations = metadata.get("annotations") if isinstance(metadata, Mapping) else None
        data = value.get("data")
        if (
            not isinstance(metadata, Mapping)
            or metadata.get("uid") != contract["head_uid"]
            or metadata.get("namespace") != "fs2-system"
            or metadata.get("name") != contract["head_name"]
            or metadata.get("labels") != {
                "app.kubernetes.io/managed-by": "platform-security",
                "fs2-serve.nebius.ai/immutable-security-boundary": "true",
                "fs2-serve.nebius.ai/provider-renewal-head": "true",
            }
            or not isinstance(annotations, Mapping)
            or not isinstance(data, Mapping)
            or set(data) != {"envelope.json", "envelope.sha256"}
        ):
            raise CatalogError("NIM provider renewal head identity differs")
        try:
            envelope = json.loads(data["envelope.json"], object_pairs_hook=_unique_object)
        except (KeyError, TypeError, ValueError) as error:
            raise CatalogError("NIM provider renewal head JSON is invalid") from error
        subject = envelope.get("subject") if isinstance(envelope, Mapping) else None
        if (
            not isinstance(envelope, Mapping)
            or set(envelope) != {
                "subject", "subject_sha256", "evidence_sha256", "attestation",
                "attestation_sha256",
            }
            or not isinstance(subject, Mapping)
            or set(subject) != {
                "schema", "cluster_uid", "security_session_id",
                "security_handoff_sha256", "generation",
                "previous_head_subject_sha256", "updated_at", "valid_until",
                "max_age_seconds", "current", "previous", "anchor",
            }
            or subject.get("schema") != PROVIDER_HEAD_SCHEMA
            or subject.get("cluster_uid") != boundary["cluster_uid"]
            or subject.get("security_session_id") != config.security_session_id
            or _sha256(canonical_bytes(subject)) != envelope.get("subject_sha256")
            or _sha256(canonical_bytes(envelope.get("attestation")))
            != envelope.get("attestation_sha256")
            or _sha256(canonical_bytes(envelope)) != data.get("envelope.sha256")
            or annotations.get("fs2-serve.nebius.ai/head-generation")
            != str(subject.get("generation"))
            or annotations.get("fs2-serve.nebius.ai/head-subject-sha256")
            != envelope.get("subject_sha256")
            or annotations.get("fs2-serve.nebius.ai/head-envelope-sha256")
            != data.get("envelope.sha256")
            or annotations.get("fs2-serve.nebius.ai/previous-head-subject-sha256")
            != subject.get("previous_head_subject_sha256")
        ):
            raise CatalogError("NIM provider renewal head digest differs")
        generation = subject.get("generation")
        updated_at = _parse_timestamp(subject.get("updated_at"), field="provider head update")
        valid_until = _parse_timestamp(subject.get("valid_until"), field="provider head validity")
        now = datetime.now(timezone.utc).replace(microsecond=0)
        if (
            not isinstance(generation, int)
            or generation < contract["activation_generation"]
            or generation < int(getattr(self, "_active_provider_generation", 0))
            or not isinstance(subject.get("max_age_seconds"), int)
            or subject["max_age_seconds"] != contract["max_observation_age_seconds"]
            or updated_at > now + timedelta(seconds=contract["max_clock_skew_seconds"])
            or valid_until <= now
            or valid_until - updated_at > timedelta(seconds=subject["max_age_seconds"])
            or subject.get("anchor")
            != {
                "generation": contract["activation_head_generation"],
                "subject_sha256": contract["activation_head_subject_sha256"],
                "chain_sha256": contract["activation_head_chain_sha256"],
            }
        ):
            raise CatalogError("NIM provider renewal head is stale or replayed")
        verified = verify_signed_attestation(
            envelope["attestation"],
            trusted_attestors=config.trusted_attestors,
            expected_session_id=config.security_session_id,
            expected_kind="provider-renewal-head",
            expected_schema=PROVIDER_HEAD_SCHEMA,
            expected_digest="sha256:" + str(envelope["subject_sha256"]),
            expected_model_id="platform",
        )
        if verified["claims"] != {
            "authorization_id": boundary["provider_authorization_id"],
            "decision": "accepted",
            "reviewer_role": "independent-platform-security",
            "evidence_sha256": envelope["evidence_sha256"],
        }:
            raise CatalogError("NIM provider renewal head claims differ")
        records = [subject.get("current")]
        if subject.get("previous") is not None:
            records.append(subject["previous"])
        normalized: list[Mapping[str, Any]] = []
        for index, record in enumerate(records):
            if (
                not isinstance(record, Mapping)
                or set(record) != {
                    "generation", "provider_envelope", "checkpoint",
                    "installation_receipt",
                }
                or not isinstance(record.get("generation"), int)
                or record["generation"] != generation - index
            ):
                raise CatalogError("NIM provider head generation record differs")
            normalized.append(record)
        if len(normalized) == 2 and (
            normalized[0]["provider_envelope"].get("previous_subject_sha256")
            != normalized[1]["provider_envelope"].get("subject_sha256")
            or normalized[0]["installation_receipt"]
            != normalized[1]["installation_receipt"]
        ):
            raise CatalogError("NIM provider head fallback chain differs")
        last_generation = getattr(self, "_active_provider_head_generation", None)
        last_subject_sha256 = getattr(self, "_active_provider_head_subject_sha256", None)
        last_resource_version = getattr(
            self, "_active_provider_head_resource_version", None
        )
        if (
            not isinstance(subject.get("anchor"), Mapping)
            or set(subject["anchor"]) != {
                "generation", "subject_sha256", "chain_sha256"
            }
            or not 1 <= subject["anchor"]["generation"] <= generation
            or re.fullmatch(
                r"[a-f0-9]{64}", str(subject["anchor"].get("subject_sha256"))
            )
            is None
            or re.fullmatch(
                r"[a-f0-9]{64}", str(subject["anchor"].get("chain_sha256"))
            )
            is None
            or (
                last_generation is not None
                and (
                    generation < last_generation
                    or (
                        generation == last_generation
                        and (
                            envelope["subject_sha256"] != last_subject_sha256
                            or metadata.get("resourceVersion") != last_resource_version
                        )
                    )
                    or (
                        generation > last_generation
                        and (
                            generation != last_generation + 1
                            or subject["previous_head_subject_sha256"]
                            != last_subject_sha256
                        )
                    )
                )
            )
        ):
            raise CatalogError("NIM provider head anchor differs")
        self._active_provider_head_generation = generation
        self._active_provider_head_subject_sha256 = envelope["subject_sha256"]
        self._active_provider_head_resource_version = str(metadata.get("resourceVersion"))
        security_handoff_sha256 = str(subject["security_handoff_sha256"])
        installation_receipt_reference = normalized[0]["installation_receipt"]
        return (
            normalized,
            security_handoff_sha256,
            installation_receipt_reference,
        )

    async def _provider_envelope_from_record(
        self, *, config: NimAdmissionConfig, record: Mapping[str, Any]
    ) -> tuple[int, Mapping[str, Any], Mapping[str, str]]:
        reference = record.get("provider_envelope")
        if not isinstance(reference, Mapping) or set(reference) != {
            "name", "uid", "resource_version", "projection_sha256", "subject_sha256",
            "previous_subject_sha256",
        }:
            raise CatalogError("NIM provider envelope head reference differs")
        value = await self._configmap_by_name(str(reference["name"]))
        metadata = value.get("metadata")
        labels = metadata.get("labels") if isinstance(metadata, Mapping) else None
        data = value.get("data")
        generation = record["generation"]
        if (
            value.get("immutable") is not True
            or not isinstance(metadata, Mapping)
            or metadata.get("uid") != reference["uid"]
            or metadata.get("resourceVersion") != reference["resource_version"]
            or not isinstance(labels, Mapping)
            or labels.get("fs2-serve.nebius.ai/provider-renewal-generation")
            != str(generation)
            or not isinstance(data, Mapping)
            or set(data) != {"envelope.json", "envelope.sha256"}
            or _sha256(canonical_bytes(_desired_object_projection(value, secret=False)))
            != reference["projection_sha256"]
        ):
            raise CatalogError("NIM provider envelope live identity differs")
        try:
            envelope = json.loads(data["envelope.json"], object_pairs_hook=_unique_object)
        except (KeyError, TypeError, ValueError) as error:
            raise CatalogError("NIM provider envelope JSON is invalid") from error
        if (
            _sha256(canonical_bytes(envelope)) != data.get("envelope.sha256")
            or envelope.get("subject_sha256") != reference["subject_sha256"]
            or envelope.get("subject", {}).get("renewal", {}).get(
                "previous_subject_sha256"
            )
            != reference["previous_subject_sha256"]
        ):
            raise CatalogError("NIM provider envelope head digest differs")
        return (
            int(generation),
            envelope,
            {
                "name": str(metadata["name"]),
                "uid": str(metadata["uid"]),
                "resource_version": str(metadata["resourceVersion"]),
                "projection_sha256": str(reference["projection_sha256"]),
            },
        )

    async def _verify_live_installation_snapshot(
        self, *, config: NimAdmissionConfig, require_backend_activation: bool = False
    ) -> None:
        receipt = getattr(config, "live_installation_receipt", None)
        if not isinstance(receipt, Mapping):
            raise CatalogError("NIM live installation receipt was not verified")
        tls = config.admission_policy["tls"]
        deployment_ready = False
        live_objects: dict[tuple[str, str, str, str], Mapping[str, Any]] = {}
        for expected in receipt["objects"]:
            key = (expected["api_version"], expected["kind"])
            if expected["kind"] == "Secret":
                secret_path = _SECURITY_OBJECT_PATHS[("v1", "Secret")].format(
                    namespace=quote(str(expected["namespace"]), safe=""),
                    name=quote(str(expected["name"]), safe=""),
                )
                try:
                    secret_response = await self.client.get(
                        secret_path,
                        headers={
                            **self._headers(),
                            "Accept": (
                                "application/json;as=PartialObjectMetadata;g=meta.k8s.io;v=v1"
                            ),
                        },
                    )
                except (OSError, httpx.HTTPError) as error:
                    raise CatalogError("NIM TLS Secret metadata lookup failed") from error
                if secret_response.status_code != 200 or len(secret_response.content) > 128 * 1024:
                    raise CatalogError("NIM TLS Secret metadata is absent")
                try:
                    secret_metadata_object = secret_response.json()
                except ValueError as error:
                    raise CatalogError("NIM TLS Secret metadata is invalid") from error
                secret_metadata = (
                    secret_metadata_object.get("metadata")
                    if isinstance(secret_metadata_object, Mapping)
                    else None
                )
                if (
                    expected["api_version"] != "v1"
                    or expected["namespace"] != "fs2-system"
                    or expected["name"] != tls["secret_name"]
                    or expected["uid"] != tls["secret_uid"]
                    or expected["resource_version"] != tls["secret_resource_version"]
                    or not isinstance(secret_metadata, Mapping)
                    or secret_metadata.get("namespace") != expected["namespace"]
                    or secret_metadata.get("name") != expected["name"]
                    or secret_metadata.get("uid") != expected["uid"]
                    or secret_metadata.get("resourceVersion")
                    != expected["resource_version"]
                ):
                    raise CatalogError("NIM TLS Secret receipt identity differs")
                continue
            pattern = _SECURITY_OBJECT_PATHS.get(key)
            if pattern is None:
                raise CatalogError("NIM live installation receipt contains an unsupported GVK")
            path = pattern.format(
                namespace=quote(str(expected["namespace"]), safe=""),
                name=quote(str(expected["name"]), safe=""),
            )
            try:
                response = await self.client.get(path, headers=self._headers())
            except (OSError, httpx.HTTPError) as error:
                raise CatalogError("NIM live installation object lookup failed") from error
            if response.status_code != 200 or len(response.content) > 4 * 1024 * 1024:
                raise CatalogError("NIM live installation object is absent")
            try:
                live = response.json()
            except ValueError as error:
                raise CatalogError("NIM live installation object is invalid") from error
            metadata = live.get("metadata") if isinstance(live, Mapping) else None
            if (
                not isinstance(live, Mapping)
                or live.get("apiVersion") != expected["api_version"]
                or live.get("kind") != expected["kind"]
                or not isinstance(metadata, Mapping)
                or metadata.get("namespace", "") != expected["namespace"]
                or metadata.get("name") != expected["name"]
                or metadata.get("uid") != expected["uid"]
                or metadata.get("resourceVersion") != expected["resource_version"]
                or str(metadata.get("generation", "0")) != expected["generation"]
                or _sha256(
                    canonical_bytes(_desired_object_projection(live, secret=False))
                )
                != expected["projection_sha256"]
            ):
                raise CatalogError("NIM live installation object projection differs")
            if key == ("apps/v1", "Deployment"):
                status = live.get("status")
                deployment_ready = (
                    isinstance(status, Mapping)
                    and status.get("observedGeneration") == metadata.get("generation")
                    and isinstance(status.get("readyReplicas"), int)
                    and status["readyReplicas"] >= 2
                    and status.get("availableReplicas", 0) >= 2
                )
            live_objects[
                (
                    expected["api_version"],
                    expected["kind"],
                    expected["namespace"],
                    expected["name"],
                )
            ] = live
        # Pod readiness must not depend on the Deployment or EndpointSlices
        # already reporting this same Pod ready. External phase-three activation
        # calls this method with require_backend_activation=True after both Pods
        # have passed their local signature/identity/TLS/policy readiness.
        if not require_backend_activation:
            return
        try:
            endpoint_response = await self.client.get(
                "/apis/discovery.k8s.io/v1/namespaces/fs2-system/endpointslices"
                "?labelSelector=kubernetes.io%2Fservice-name%3D"
                "fs2-serve-control-plane-nim-admission",
                headers=self._headers(),
            )
        except (OSError, httpx.HTTPError) as error:
            raise CatalogError("NIM admission endpoint lookup failed") from error
        if endpoint_response.status_code != 200 or len(endpoint_response.content) > 4 * 1024 * 1024:
            raise CatalogError("NIM admission endpoints are absent")
        try:
            endpoint_list = endpoint_response.json()
        except ValueError as error:
            raise CatalogError("NIM admission endpoints are invalid") from error
        endpoint_items = endpoint_list.get("items") if isinstance(endpoint_list, Mapping) else None
        service = live_objects.get(
            ("v1", "Service", "fs2-system", "fs2-serve-control-plane-nim-admission")
        )
        deployment = live_objects.get(
            ("apps/v1", "Deployment", "fs2-system", "fs2-serve-control-plane-nim-admission")
        )
        service_metadata = service.get("metadata") if isinstance(service, Mapping) else None
        deployment_metadata = (
            deployment.get("metadata") if isinstance(deployment, Mapping) else None
        )
        ready_targets: dict[str, tuple[str, str, str, frozenset[str]]] = {}
        ready_addresses: set[str] = set()
        for item in endpoint_items or []:
            metadata = item.get("metadata") if isinstance(item, Mapping) else None
            owner_references = (
                metadata.get("ownerReferences") if isinstance(metadata, Mapping) else None
            )
            if (
                not isinstance(item, Mapping)
                or item.get("addressType") not in {"IPv4", "IPv6"}
                or not isinstance(metadata, Mapping)
                or metadata.get("namespace") != "fs2-system"
                or metadata.get("labels", {}).get("kubernetes.io/service-name")
                != "fs2-serve-control-plane-nim-admission"
                or metadata.get("labels", {}).get("endpointslice.kubernetes.io/managed-by")
                != "endpointslice-controller.k8s.io"
                or not isinstance(owner_references, list)
                or len(owner_references) != 1
                or not isinstance(service_metadata, Mapping)
                or owner_references[0]
                != {
                    "apiVersion": "v1",
                    "kind": "Service",
                    "name": "fs2-serve-control-plane-nim-admission",
                    "uid": service_metadata.get("uid"),
                    "controller": True,
                    "blockOwnerDeletion": True,
                }
                or item.get("ports")
                != [{"name": "https", "protocol": "TCP", "port": 8443}]
            ):
                raise CatalogError("NIM admission EndpointSlice attribution differs")
            for endpoint in item.get("endpoints", []):
                target = endpoint.get("targetRef") if isinstance(endpoint, Mapping) else None
                addresses = endpoint.get("addresses") if isinstance(endpoint, Mapping) else None
                if (
                    isinstance(endpoint, Mapping)
                    and isinstance(endpoint.get("conditions"), Mapping)
                    and endpoint["conditions"].get("ready") is True
                    and endpoint["conditions"].get("terminating") is not True
                    and endpoint["conditions"].get("serving", True) is True
                    and isinstance(target, Mapping)
                    and target.get("apiVersion") == "v1"
                    and target.get("kind") == "Pod"
                    and target.get("namespace") == "fs2-system"
                    and isinstance(target.get("name"), str)
                    and isinstance(target.get("uid"), str)
                    and isinstance(addresses, list)
                    and addresses
                    and len(addresses) == len(set(addresses))
                    and all(
                        _ip_matches_address_type(address, item["addressType"])
                        for address in addresses
                    )
                ):
                    if (
                        str(target["uid"]) in ready_targets
                        or ready_addresses.intersection(addresses)
                    ):
                        raise CatalogError("NIM admission endpoint identity is duplicated")
                    ready_addresses.update(addresses)
                    ready_targets[str(target["uid"])] = (
                        str(target["name"]),
                        str(target["uid"]),
                        str(item["addressType"]),
                        frozenset(str(address) for address in addresses),
                    )
        if not isinstance(deployment_metadata, Mapping):
            raise CatalogError("NIM admission Deployment identity is absent")
        for pod_name, pod_uid, address_type, endpoint_addresses in ready_targets.values():
            pod = await self.get(
                api_version="v1",
                kind="Pod",
                namespace="fs2-system",
                name=pod_name,
                expected_uid=pod_uid,
            )
            pod_metadata = pod.get("metadata")
            pod_status = pod.get("status") if isinstance(pod, Mapping) else None
            pod_ips = (
                {
                    str(item["ip"])
                    for item in pod_status.get("podIPs", [])
                    if isinstance(item, Mapping) and isinstance(item.get("ip"), str)
                }
                if isinstance(pod_status, Mapping)
                else set()
            )
            if isinstance(pod_status, Mapping) and isinstance(pod_status.get("podIP"), str):
                pod_ips.add(str(pod_status["podIP"]))
            family_pod_ips = {
                address
                for address in pod_ips
                if _ip_matches_address_type(address, address_type)
            }
            if (
                not isinstance(pod_metadata, Mapping)
                or pod_metadata.get("labels", {}).get("app.kubernetes.io/name")
                != "fs2-nim-admission-security"
                or not any(
                    condition.get("type") == "Ready" and condition.get("status") == "True"
                    for condition in pod.get("status", {}).get("conditions", [])
                    if isinstance(condition, Mapping)
                )
                or endpoint_addresses != family_pod_ips
            ):
                raise CatalogError("NIM admission endpoint Pod is not ready")
            owners = await self.owner_chain(
                pod, root_kind="Deployment", namespace="fs2-system"
            )
            replica_set = owners[0] if owners else None
            replica_template = (
                replica_set.get("spec", {}).get("template")
                if isinstance(replica_set, Mapping)
                else None
            )
            deployment_template = deployment.get("spec", {}).get("template")
            if (
                len(owners) != 2
                or owners[-1].get("metadata", {}).get("uid")
                != deployment_metadata.get("uid")
                or not isinstance(replica_template, Mapping)
                or not isinstance(deployment_template, Mapping)
                or _pod_template_projection(replica_template)
                != _pod_template_projection(deployment_template)
                or _pod_template_projection(pod)
                != _pod_template_projection(replica_template)
            ):
                raise CatalogError("NIM admission endpoint owner chain differs")
        if not deployment_ready or len(ready_targets) < 2:
            raise CatalogError("NIM admission backend lacks two ready independent endpoints")

    async def _verify_provider_renewal_checkpoints(
        self,
        *,
        config: NimAdmissionConfig,
        candidate: Mapping[str, Any],
        generation_record: Mapping[str, Any],
    ) -> None:
        contract = config.admission_policy["security_boundary"]["provider_renewal"]
        reference = generation_record.get("checkpoint")
        if (
            not isinstance(contract, Mapping)
            or not isinstance(candidate, Mapping)
            or not isinstance(reference, Mapping)
            or set(reference) != {
                "name", "uid", "resource_version", "projection_sha256",
                "subject_sha256",
            }
        ):
            raise CatalogError("NIM provider renewal checkpoint input is absent")
        checkpoint = await self._configmap_by_name(str(reference["name"]))
        metadata = checkpoint.get("metadata")
        data = checkpoint.get("data")
        try:
            subject = json.loads(data["subject.json"], object_pairs_hook=_unique_object)
            attestation = json.loads(data["attestation.json"], object_pairs_hook=_unique_object)
        except (KeyError, TypeError, ValueError) as error:
            raise CatalogError("NIM provider renewal checkpoint JSON is invalid") from error
        if (
            checkpoint.get("immutable") is not True
            or not isinstance(metadata, Mapping)
            or metadata.get("uid") != reference["uid"]
            or metadata.get("resourceVersion") != reference["resource_version"]
            or not isinstance(data, Mapping)
            or set(data) != {
                "subject.json", "subject.sha256", "attestation.json", "attestation.sha256"
            }
            or not isinstance(subject, Mapping)
            or set(subject) != {
                "schema", "cluster_uid", "generation", "subject_sha256",
                "previous_subject_sha256", "envelope_name", "envelope_uid",
                "envelope_resource_version", "envelope_projection_sha256",
                "observed_at", "security_handoff_sha256",
            }
            or subject.get("schema") != PROVIDER_CHECKPOINT_SCHEMA
            or subject.get("generation") != candidate["generation"]
            or subject.get("subject_sha256") != candidate["subject_sha256"]
            or subject.get("previous_subject_sha256")
            != candidate["previous_subject_sha256"]
            or subject.get("envelope_name") != candidate["envelope_name"]
            or subject.get("envelope_uid") != candidate["envelope_uid"]
            or subject.get("envelope_resource_version")
            != candidate["envelope_resource_version"]
            or subject.get("envelope_projection_sha256")
            != candidate["envelope_projection_sha256"]
            or _sha256(canonical_bytes(subject)) != data.get("subject.sha256")
            or data.get("subject.sha256") != reference["subject_sha256"]
            or _sha256(canonical_bytes(attestation)) != data.get("attestation.sha256")
            or _sha256(canonical_bytes(_desired_object_projection(checkpoint, secret=False)))
            != reference["projection_sha256"]
        ):
            raise CatalogError("NIM provider renewal checkpoint does not bind the live generation")
        verified = verify_signed_attestation(
            attestation,
            trusted_attestors=config.trusted_attestors,
            expected_session_id=config.security_session_id,
            expected_kind="provider-renewal-checkpoint",
            expected_schema=PROVIDER_CHECKPOINT_SCHEMA,
            expected_digest="sha256:" + str(data["subject.sha256"]),
            expected_model_id="platform",
        )
        if verified["claims"] != {
            "authorization_id": config.admission_policy["security_boundary"][
                "provider_authorization_id"
            ],
            "decision": "accepted",
            "reviewer_role": "independent-platform-security",
            "evidence_sha256": candidate["envelope_projection_sha256"],
        }:
            raise CatalogError("NIM provider renewal checkpoint claims differ")

    async def _installation_receipt_from_reference(
        self, *, reference: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        if not isinstance(reference, Mapping) or set(reference) != {
            "name", "uid", "resource_version", "projection_sha256", "subject_sha256"
        }:
            raise CatalogError("NIM installation receipt head reference differs")
        value = await self._configmap_by_name(str(reference["name"]))
        metadata = value.get("metadata")
        data = value.get("data")
        if (
            value.get("immutable") is not True
            or not isinstance(metadata, Mapping)
            or metadata.get("uid") != reference["uid"]
            or metadata.get("resourceVersion") != reference["resource_version"]
            or not isinstance(data, Mapping)
            or set(data) != {
                "subject.json", "subject.sha256", "attestation.json", "attestation.sha256"
            }
            or _sha256(canonical_bytes(_desired_object_projection(value, secret=False)))
            != reference["projection_sha256"]
            or data.get("subject.sha256") != reference["subject_sha256"]
        ):
            raise CatalogError("NIM installation receipt live identity differs")
        try:
            subject = json.loads(data["subject.json"], object_pairs_hook=_unique_object)
            attestation = json.loads(data["attestation.json"], object_pairs_hook=_unique_object)
        except (KeyError, TypeError, ValueError) as error:
            raise CatalogError("NIM installation receipt JSON is invalid") from error
        if (
            not isinstance(subject, Mapping)
            or _sha256(canonical_bytes(subject)) != data.get("subject.sha256")
            or _sha256(canonical_bytes(attestation)) != data.get("attestation.sha256")
            or metadata.get("name")
            != f"fs2-nim-installation-{str(data.get('subject.sha256'))[:16]}"
        ):
            raise CatalogError("NIM installation receipt digest differs")
        return {
            "subject": subject,
            "subject_sha256": data["subject.sha256"],
            "evidence_sha256": attestation.get("claims", {}).get("evidence_sha256"),
            "attestation": attestation,
            "attestation_sha256": data["attestation.sha256"],
        }

    async def verify_admission_policy(self, config: NimAdmissionConfig) -> None:
        policy = config.admission_policy
        minimum_generation = max(
            int(policy["security_boundary"]["provider_renewal"]["activation_generation"]),
            int(getattr(self, "_active_provider_generation", 0)),
        )
        (
            generation_records,
            security_handoff_sha256,
            installation_receipt_reference,
        ) = await self._provider_renewal_head(config=config)
        receipt = await self._installation_receipt_from_reference(
            reference=installation_receipt_reference
        )
        config.verify_installation_receipt_authorization(
            receipt_envelope=receipt,
            expected_handoff_sha256=security_handoff_sha256,
        )
        accepted = False
        last_error: CatalogError | None = None
        for generation_record in generation_records:
            generation, renewed_envelope, renewal_metadata = (
                await self._provider_envelope_from_record(
                    config=config, record=generation_record
                )
            )
            if generation < minimum_generation:
                continue
            try:
                config.verify_security_boundary_authorization(
                    renewed_envelope=renewed_envelope,
                    renewal_metadata=renewal_metadata,
                )
                candidate = config.accepted_provider_renewal_candidate
                if candidate["generation"] != generation:
                    raise CatalogError("NIM provider renewal generation label differs")
                await self._verify_provider_renewal_checkpoints(
                    config=config,
                    candidate=candidate,
                    generation_record=generation_record,
                )
                await self._verify_live_installation_snapshot(config=config)
                self._active_provider_generation = generation
                accepted = True
                break
            except CatalogError as error:
                last_error = error
        if not accepted:
            raise CatalogError(
                "no complete monotonic NIM provider generation is available"
            ) from last_error
        name = policy.get("name")
        if not isinstance(name, str) or not name:
            raise CatalogError("NIM admission policy name is absent")
        try:
            response = await self.client.get(
                "/apis/admissionregistration.k8s.io/v1/validatingwebhookconfigurations/"
                + quote(name, safe=""),
                headers=self._headers(),
            )
        except (OSError, httpx.HTTPError) as error:
            raise CatalogError("NIM admission policy lookup failed") from error
        if response.status_code != 200 or len(response.content) > 1024 * 1024:
            raise CatalogError("NIM admission policy is not installed")
        try:
            value = response.json()
        except ValueError as error:
            raise CatalogError("NIM admission policy lookup returned invalid JSON") from error
        webhooks = value.get("webhooks") if isinstance(value, Mapping) else None
        if not isinstance(webhooks, list) or len(webhooks) != 1 or not isinstance(webhooks[0], Mapping):
            raise CatalogError("NIM admission policy webhook inventory differs")
        webhook = webhooks[0]
        client_config = webhook.get("clientConfig")
        service = client_config.get("service") if isinstance(client_config, Mapping) else None
        namespace_selector = webhook.get("namespaceSelector")
        labels = (
            namespace_selector.get("matchLabels")
            if isinstance(namespace_selector, Mapping)
            else None
        )
        rules = webhook.get("rules")
        if not isinstance(rules, list):
            raise CatalogError("NIM admission policy rules are absent")
        resources: list[str] = []
        operations: set[str] = set()
        for rule in rules:
            if not isinstance(rule, Mapping) or rule.get("scope") != "Namespaced":
                raise CatalogError("NIM admission policy rule scope differs")
            groups = rule.get("apiGroups")
            versions = rule.get("apiVersions")
            names = rule.get("resources")
            rule_operations = rule.get("operations")
            if (
                not isinstance(groups, list)
                or len(groups) != 1
                or not isinstance(versions, list)
                or len(versions) != 1
                or not isinstance(names, list)
                or not isinstance(rule_operations, list)
            ):
                raise CatalogError("NIM admission policy rule shape differs")
            operations.update(str(operation) for operation in rule_operations)
            prefix = f"{groups[0]}/{versions[0]}" if groups[0] else str(versions[0])
            resources.extend(f"{prefix}/{name}" for name in names)
        ca_bundle = client_config.get("caBundle") if isinstance(client_config, Mapping) else None
        try:
            ca_sha256 = hashlib.sha256(base64.b64decode(ca_bundle, validate=True)).hexdigest()
        except (TypeError, ValueError, binascii.Error) as error:
            raise CatalogError("NIM admission policy CA bundle is invalid") from error
        security_boundary = policy.get("security_boundary")
        if not isinstance(security_boundary, Mapping) or set(security_boundary) != {
            "name", "policy_uid", "policy_resource_version", "binding_uid",
            "binding_resource_version", "subject_sha256", "authorization_id",
            "cluster_uid", "provider_authorization_id", "owner_lookup_namespaces",
            "installation_receipt_authorization_id", "principal_epoch", "provider_renewal",
        }:
            raise CatalogError("NIM external security boundary identity is absent")
        boundary_projections: list[Mapping[str, Any]] = []
        for resource, uid_field, version_field in (
            ("validatingadmissionpolicies", "policy_uid", "policy_resource_version"),
            ("validatingadmissionpolicybindings", "binding_uid", "binding_resource_version"),
        ):
            try:
                boundary_response = await self.client.get(
                    "/apis/admissionregistration.k8s.io/v1/"
                    + resource
                    + "/"
                    + quote(str(security_boundary["name"]), safe=""),
                    headers=self._headers(),
                )
            except (OSError, httpx.HTTPError) as error:
                raise CatalogError("NIM external security boundary lookup failed") from error
            if boundary_response.status_code != 200 or len(boundary_response.content) > 1024 * 1024:
                raise CatalogError("NIM external security boundary is not installed")
            try:
                boundary = boundary_response.json()
            except ValueError as error:
                raise CatalogError("NIM external security boundary response is invalid") from error
            boundary_metadata = boundary.get("metadata") if isinstance(boundary, Mapping) else None
            boundary_labels = boundary_metadata.get("labels") if isinstance(boundary_metadata, Mapping) else None
            if (
                not isinstance(boundary_metadata, Mapping)
                or boundary_metadata.get("name") != security_boundary["name"]
                or boundary_metadata.get("uid") != security_boundary[uid_field]
                or boundary_metadata.get("resourceVersion") != security_boundary[version_field]
                or not isinstance(boundary_labels, Mapping)
                or boundary_labels.get("fs2-serve.nebius.ai/immutable-security-boundary") != "true"
            ):
                raise CatalogError("NIM external security boundary live identity differs")
            boundary_projections.append(
                {
                    "api_version": boundary.get("apiVersion"),
                    "kind": boundary.get("kind"),
                    "metadata": {
                        "name": boundary_metadata.get("name"),
                        "uid": boundary_metadata.get("uid"),
                        "resource_version": boundary_metadata.get("resourceVersion"),
                        "labels": dict(boundary_labels),
                    },
                    "spec": boundary.get("spec"),
                }
            )
        live_boundary_subject = {
            "schema": BOUNDARY_SCHEMA,
            "owner": "platform-security",
            "custody": {
                "schema": PROVIDER_CUSTODY_SCHEMA,
                "cluster_uid": security_boundary["cluster_uid"],
                "enforcement": "external-provider-iam-preventive",
                "principal_epoch": security_boundary["principal_epoch"],
                "security_principal": security_boundary["principal_epoch"][
                    "active_principal"
                ],
                "workload_release_principal": "system:serviceaccount:fs2-system:fs2-release-automation",
                "workload_release_admissionregistration_writes": False,
                "workload_release_protected_backend_writes": False,
                "owner_lookup_namespaces": security_boundary[
                    "owner_lookup_namespaces"
                ],
                "provider_renewal": security_boundary["provider_renewal"],
            },
            "objects": boundary_projections,
        }
        if hashlib.sha256(canonical_bytes(live_boundary_subject)).hexdigest() != security_boundary[
            "subject_sha256"
        ]:
            raise CatalogError("NIM external security boundary live projection differs")
        observed = {
            "schema": POLICY_SCHEMA,
            "name": value.get("metadata", {}).get("name") if isinstance(value.get("metadata"), Mapping) else None,
            "namespace": labels.get("kubernetes.io/metadata.name") if isinstance(labels, Mapping) else None,
            "failure_policy": webhook.get("failurePolicy"),
            "match_policy": webhook.get("matchPolicy"),
            "side_effects": webhook.get("sideEffects"),
            "timeout_seconds": webhook.get("timeoutSeconds"),
            "admission_review_versions": webhook.get("admissionReviewVersions"),
            "operations": sorted(operations),
            "resources": sorted(resources),
            "service": {
                "namespace": service.get("namespace") if isinstance(service, Mapping) else None,
                "name": service.get("name") if isinstance(service, Mapping) else None,
                "path": service.get("path") if isinstance(service, Mapping) else None,
                "port": service.get("port") if isinstance(service, Mapping) else None,
            },
            "ca_bundle_sha256": ca_sha256,
            "tls": dict(policy["tls"]),
            "owner_resolution": "live-read-through-exact-uid-chain",
            "owner_lookup_namespaces": list(policy["owner_lookup_namespaces"]),
            "network_policy": dict(policy["network_policy"]),
            "root_enrollment": {
                "namespace": "fs2-system",
                "name_prefix": "fs2-nim-root-",
                "storage_kind": "immutable-configmap-create-once",
                "reconciler": "persisted-root-readback",
                "reconcile_interval_seconds": 30,
                "admission_behavior": "verify-existing-deny-until-enrolled",
            },
            "security_boundary": dict(security_boundary),
        }
        expected = dict(policy)
        expected["operations"] = sorted(expected.get("operations", []))
        expected["resources"] = sorted(expected.get("resources", []))
        if observed != expected:
            raise CatalogError("installed NIM admission policy differs from its signed digest")

    async def verify_backend_activation(self, config: NimAdmissionConfig) -> None:
        """Run phase-three HA attribution after local Pod readiness is possible."""

        await self._verify_live_installation_snapshot(
            config=config, require_backend_activation=True
        )

    async def enroll_root(
        self,
        root: Mapping[str, Any],
        *,
        model_id: str,
        subject_sha256: str,
        policy: Mapping[str, Any],
    ) -> None:
        """Atomically bind one signed envelope to the first persisted root UID."""

        metadata = root.get("metadata")
        enrollment = policy.get("root_enrollment")
        if (
            root.get("apiVersion") != "apps.nvidia.com/v1alpha1"
            or root.get("kind") not in {"NIMCache", "NIMService"}
            or not isinstance(metadata, Mapping)
            or metadata.get("namespace", "fs2-models") != "fs2-models"
            or metadata.get("name") != model_id
            or not isinstance(metadata.get("uid"), str)
            or re.fullmatch(
                r"[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}",
                metadata["uid"],
            )
            is None
            or not isinstance(enrollment, Mapping)
            or enrollment != {
                "namespace": "fs2-system",
                "name_prefix": "fs2-nim-root-",
                "storage_kind": "immutable-configmap-create-once",
                "reconciler": "persisted-root-readback",
                "reconcile_interval_seconds": 30,
                "admission_behavior": "verify-existing-deny-until-enrolled",
            }
            or len(subject_sha256) != 64
            or any(character not in "0123456789abcdef" for character in subject_sha256)
        ):
            raise CatalogError("NIM root enrollment identity is incomplete")
        name = _root_enrollment_name(
            subject_sha256=subject_sha256,
            root_uid=str(metadata["uid"]),
        )
        namespace = str(enrollment["namespace"])
        expected_data = {
            "schema": "fs2-serve.nebius.ai/nim-root-enrollment/v1",
            "resource_kind": str(root["kind"]),
            "model_id": model_id,
            "root_uid": str(metadata["uid"]),
            "subject_sha256": subject_sha256,
        }
        body = {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {
                "name": name,
                "namespace": namespace,
                "labels": {ROOT_ENROLLMENT_LABEL: "true"},
            },
            "immutable": True,
            "data": expected_data,
        }
        try:
            response = await self.client.post(
                f"/api/v1/namespaces/{quote(namespace, safe='')}/configmaps",
                headers={**self._headers(), "Content-Type": "application/json"},
                content=canonical_bytes(body),
            )
            if response.status_code == 409:
                response = await self.client.get(
                    f"/api/v1/namespaces/{quote(namespace, safe='')}/configmaps/{quote(name, safe='')}",
                    headers=self._headers(),
                )
        except (OSError, httpx.HTTPError) as error:
            raise CatalogError("NIM root enrollment request failed") from error
        if response.status_code not in {200, 201} or len(response.content) > 1024 * 1024:
            raise CatalogError("NIM root enrollment could not be committed")
        try:
            persisted = response.json()
        except ValueError as error:
            raise CatalogError("NIM root enrollment response is invalid") from error
        persisted_metadata = persisted.get("metadata") if isinstance(persisted, Mapping) else None
        if (
            not isinstance(persisted, Mapping)
            or persisted.get("apiVersion") != "v1"
            or persisted.get("kind") != "ConfigMap"
            or persisted.get("immutable") is not True
            or persisted.get("data") != expected_data
            or not isinstance(persisted_metadata, Mapping)
            or persisted_metadata.get("name") != name
            or persisted_metadata.get("namespace") != namespace
            or persisted_metadata.get("labels") != {ROOT_ENROLLMENT_LABEL: "true"}
        ):
            raise CatalogError("persisted NIM root enrollment differs")

    async def verify_root_enrollment(
        self,
        root: Mapping[str, Any],
        *,
        model_id: str,
        subject_sha256: str,
        policy: Mapping[str, Any],
    ) -> None:
        """Require the reconciler's immutable UID binding without mutating admission state."""

        enrollment = policy.get("root_enrollment")
        metadata = root.get("metadata")
        if not isinstance(enrollment, Mapping) or not isinstance(metadata, Mapping):
            raise CatalogError("NIM root enrollment policy or identity is absent")
        name = _root_enrollment_name(
            subject_sha256=subject_sha256,
            root_uid=str(metadata.get("uid", "")),
        )
        namespace = str(enrollment.get("namespace", ""))
        expected_data = {
            "schema": "fs2-serve.nebius.ai/nim-root-enrollment/v1",
            "resource_kind": str(root.get("kind", "")),
            "model_id": model_id,
            "root_uid": str(metadata.get("uid", "")),
            "subject_sha256": subject_sha256,
        }
        try:
            response = await self.client.get(
                f"/api/v1/namespaces/{quote(namespace, safe='')}/configmaps/{quote(name, safe='')}",
                headers=self._headers(),
            )
        except (OSError, httpx.HTTPError) as error:
            raise CatalogError("NIM root enrollment lookup failed") from error
        if response.status_code != 200 or len(response.content) > 1024 * 1024:
            raise CatalogError("NIM root is not atomically enrolled yet")
        try:
            persisted = response.json()
        except ValueError as error:
            raise CatalogError("NIM root enrollment response is invalid") from error
        persisted_metadata = persisted.get("metadata") if isinstance(persisted, Mapping) else None
        if (
            not isinstance(persisted, Mapping)
            or persisted.get("apiVersion") != "v1"
            or persisted.get("kind") != "ConfigMap"
            or persisted.get("immutable") is not True
            or persisted.get("data") != expected_data
            or not isinstance(persisted_metadata, Mapping)
            or persisted_metadata.get("name") != name
            or persisted_metadata.get("namespace") != namespace
            or persisted_metadata.get("labels") != {ROOT_ENROLLMENT_LABEL: "true"}
        ):
            raise CatalogError("persisted NIM root enrollment differs")

    async def _list_roots(self, *, kind: str, namespace: str) -> list[Mapping[str, Any]]:
        resource = {"NIMCache": "nimcaches", "NIMService": "nimservices"}.get(kind)
        if resource is None:
            raise CatalogError("NIM root reconciler received an unsupported kind")
        path = (
            "/apis/apps.nvidia.com/v1alpha1/namespaces/"
            f"{quote(namespace, safe='')}/{resource}"
        )
        roots: list[Mapping[str, Any]] = []
        continuation: str | None = None
        for _ in range(16):
            params = {"limit": "500"}
            if continuation is not None:
                params["continue"] = continuation
            try:
                response = await self.client.get(path, params=params, headers=self._headers())
            except (OSError, httpx.HTTPError) as error:
                raise CatalogError("NIM root inventory lookup failed") from error
            if response.status_code != 200 or len(response.content) > 16 * 1024 * 1024:
                raise CatalogError("NIM root inventory is unavailable")
            try:
                listing = response.json()
            except ValueError as error:
                raise CatalogError("NIM root inventory returned invalid JSON") from error
            items = listing.get("items") if isinstance(listing, Mapping) else None
            metadata = listing.get("metadata") if isinstance(listing, Mapping) else None
            if not isinstance(items, list) or not isinstance(metadata, Mapping):
                raise CatalogError("NIM root inventory shape differs")
            if any(not isinstance(item, Mapping) for item in items):
                raise CatalogError("NIM root inventory contains a non-object")
            roots.extend(items)
            next_token = metadata.get("continue", "")
            if not isinstance(next_token, str):
                raise CatalogError("NIM root inventory continuation token differs")
            if not next_token:
                return roots
            continuation = next_token
        raise CatalogError("NIM root inventory exceeds its bounded pagination")

    async def reconcile_root_enrollments(
        self,
        *,
        config: "NimAdmissionConfig",
        catalog: Catalog,
    ) -> None:
        """Read persisted roots, validate signed bytes, then create UID bindings once."""

        await self.verify_admission_policy(config)
        expected = {
            (kind, model_id): envelope
            for kind, model_id, envelope in config.entries_by_digest.values()
        }
        for kind in ("NIMCache", "NIMService"):
            for root in await self._list_roots(kind=kind, namespace=config.namespace):
                metadata = root.get("metadata")
                model_id = metadata.get("name") if isinstance(metadata, Mapping) else None
                envelope = expected.get((kind, str(model_id)))
                if envelope is None:
                    raise CatalogError("persisted NIM root is outside the signed inventory")
                subject_sha256 = validate_persisted_nim_operator_root(
                    root,
                    security_envelope=envelope,
                    trusted_attestors=config.trusted_attestors,
                    security_session_id=config.security_session_id,
                    resource_kind=kind,
                    record=catalog.model(str(model_id)),
                )
                await self.enroll_root(
                    root,
                    model_id=str(model_id),
                    subject_sha256=subject_sha256,
                    policy=config.admission_policy,
                )

    async def owner_chain(
        self,
        value: Mapping[str, Any],
        *,
        root_kind: str,
        namespace: str,
    ) -> list[Mapping[str, Any]]:
        chain: list[Mapping[str, Any]] = []
        child = value
        for _ in range(4):
            metadata = child.get("metadata")
            references = metadata.get("ownerReferences") if isinstance(metadata, Mapping) else None
            if not isinstance(references, list) or len(references) != 1 or not isinstance(references[0], Mapping):
                raise CatalogError("NIM descendant lacks one persisted owner edge")
            reference = references[0]
            owner = await self.get(
                api_version=str(reference.get("apiVersion", "")),
                kind=str(reference.get("kind", "")),
                namespace=namespace,
                name=str(reference.get("name", "")),
                expected_uid=str(reference.get("uid", "")),
            )
            chain.append(owner)
            if owner.get("kind") == root_kind:
                return chain
            child = owner
        raise CatalogError("NIM owner graph exceeds its signed depth")

    async def owner_chain_to_nim(
        self,
        value: Mapping[str, Any],
        *,
        namespace: str,
    ) -> list[Mapping[str, Any]]:
        """Classify an owner chain from persisted UID edges, never Pod hints.

        The returned list ends at either an exact NIM root or an exact
        ownerless non-NIM controller. Missing, multiple, malformed, unknown,
        or overlong edges are never a negative classification and fail closed.
        """

        chain: list[Mapping[str, Any]] = []
        child = value
        for _ in range(4):
            metadata = child.get("metadata")
            references = (
                metadata.get("ownerReferences") if isinstance(metadata, Mapping) else None
            )
            if references is None or references == []:
                if chain:
                    return chain
                raise CatalogError("NIM descendant ownership cannot classify the admitted object")
            if not isinstance(references, list) or len(references) != 1 or not isinstance(references[0], Mapping):
                raise CatalogError("NIM descendant ownership is not conclusively attributable")
            reference = references[0]
            owner = await self.get(
                api_version=str(reference.get("apiVersion", "")),
                kind=str(reference.get("kind", "")),
                namespace=namespace,
                name=str(reference.get("name", "")),
                expected_uid=str(reference.get("uid", "")),
            )
            chain.append(owner)
            if owner.get("kind") in {"NIMCache", "NIMService"}:
                return chain
            child = owner
        raise CatalogError("NIM descendant owner graph exceeds its bounded classification depth")

    async def actor_chain(
        self,
        user_info: Mapping[str, Any],
        identity: Mapping[str, Any],
    ) -> list[Mapping[str, Any]]:
        if identity.get("kind") == "kubernetes-control-plane":
            return []
        extra = user_info.get("extra")
        names = extra.get("authentication.kubernetes.io/pod-name") if isinstance(extra, Mapping) else None
        uids = extra.get("authentication.kubernetes.io/pod-uid") if isinstance(extra, Mapping) else None
        if (
            not isinstance(names, list)
            or len(names) != 1
            or not isinstance(names[0], str)
            or not isinstance(uids, list)
            or len(uids) != 1
            or not isinstance(uids[0], str)
        ):
            raise CatalogError("NIM actor token lacks Pod-bound authentication extras")
        pod = await self.get(
            api_version="v1",
            kind="Pod",
            namespace=str(identity["namespace"]),
            name=names[0],
            expected_uid=uids[0],
        )
        owners = await self.owner_chain(
            pod,
            root_kind="Deployment",
            namespace=str(identity["namespace"]),
        )
        if len(owners) != 2:
            raise CatalogError("NIM actor Pod does not resolve through ReplicaSet to Deployment")
        return [pod, *owners]


def create_nim_admission_app(
    *,
    config: NimAdmissionConfig,
    catalog: Catalog,
    resolver: KubernetesNimOwnerResolver,
    tls_identity_verifier: Callable[[], None],
) -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.state.root_enrollment_ready = False
    app.state.admission_activation_ready = False
    reconcile_stop = asyncio.Event()
    reconcile_task: asyncio.Task[None] | None = None

    async def reconcile_once() -> None:
        try:
            tls_identity_verifier()
            await resolver.reconcile_root_enrollments(config=config, catalog=catalog)
        except (CatalogError, OSError, ValueError, KeyError, TypeError):
            app.state.root_enrollment_ready = False
            app.state.admission_activation_ready = False
        else:
            app.state.root_enrollment_ready = True
            try:
                await resolver.verify_backend_activation(config)
            except (CatalogError, OSError, ValueError, KeyError, TypeError):
                # Remain a locally ready endpoint so another replica can join;
                # admission stays fail closed until both exact endpoints are
                # independently attributable on a later reconcile pass.
                app.state.admission_activation_ready = False
            else:
                app.state.admission_activation_ready = True

    async def reconcile_forever() -> None:
        interval = int(config.admission_policy["root_enrollment"]["reconcile_interval_seconds"])
        while not reconcile_stop.is_set():
            await reconcile_once()
            try:
                await asyncio.wait_for(reconcile_stop.wait(), timeout=interval)
            except TimeoutError:
                continue

    @app.on_event("startup")
    async def start_root_enrollment_reconciler() -> None:
        nonlocal reconcile_task
        await reconcile_once()
        reconcile_task = asyncio.create_task(
            reconcile_forever(), name="nim-root-enrollment-reconciler"
        )

    @app.on_event("shutdown")
    async def stop_root_enrollment_reconciler() -> None:
        reconcile_stop.set()
        if reconcile_task is not None:
            await reconcile_task

    @app.get("/livez", include_in_schema=False)
    async def live() -> JSONResponse:
        return JSONResponse({"status": "ok"})

    @app.get("/readyz", include_in_schema=False)
    async def ready() -> JSONResponse:
        if app.state.root_enrollment_ready:
            return JSONResponse({"status": "ready"})
        return JSONResponse({"status": "root-enrollment-unavailable"}, status_code=503)

    @app.get("/activationz", include_in_schema=False)
    async def activation() -> JSONResponse:
        if app.state.admission_activation_ready:
            return JSONResponse({"status": "active", "attributed_endpoints": 2})
        return JSONResponse({"status": "awaiting-attributed-ha"}, status_code=503)

    @app.post("/admit", include_in_schema=False)
    @_admission_deadline(27.0)
    async def admit(request: Request) -> JSONResponse:
        review: object = await request.json()
        uid = ""
        if isinstance(review, Mapping) and isinstance(review.get("request"), Mapping):
            candidate = review["request"].get("uid")
            uid = candidate if isinstance(candidate, str) else ""
        try:
            if not isinstance(review, Mapping):
                raise CatalogError("NIM admission review is not an object")
            if not app.state.root_enrollment_ready:
                raise CatalogError("NIM root enrollment reconciler is not ready")
            admission_request = review.get("request")
            if not isinstance(admission_request, Mapping):
                raise CatalogError("NIM admission request is absent")
            admitted = admission_request.get("object")
            resource = admission_request.get("resource")
            if not isinstance(admitted, Mapping) or not isinstance(resource, Mapping):
                raise CatalogError("NIM admission object or resource is absent")
            descendant_resource = (
                resource.get("group"),
                resource.get("version"),
                resource.get("resource"),
            ) in {
                ("", "v1", "pods"),
                ("apps", "v1", "deployments"),
                ("apps", "v1", "replicasets"),
                ("apps", "v1", "statefulsets"),
                ("batch", "v1", "jobs"),
            }
            pre_resolved_owner_chain: list[Mapping[str, Any]] | None = None
            owner_chain_classified = False
            if descendant_resource:
                metadata = admitted.get("metadata")
                owner_references = (
                    metadata.get("ownerReferences")
                    if isinstance(metadata, Mapping)
                    else None
                )
                if owner_references is not None and owner_references != []:
                    owner_chain_classified = True
                    pre_resolved_owner_chain = await resolver.owner_chain_to_nim(
                        admitted,
                        namespace=config.namespace,
                    )
            if owner_chain_classified:
                assert pre_resolved_owner_chain is not None
                persisted_root = pre_resolved_owner_chain[-1]
                if persisted_root.get("kind") in {"NIMCache", "NIMService"}:
                    selected = config.select_persisted_root(
                        persisted_root, catalog=catalog
                    )
                else:
                    # A non-NIM owner chain is not itself an allow decision.
                    # Run attribution against the admitted bytes so protected
                    # images, identities, or propagated selectors cannot hide
                    # behind an ordinary controller.
                    selected = config.select(review, catalog=catalog)
                    if selected is None:
                        return JSONResponse(
                            {"apiVersion": "admission.k8s.io/v1", "kind": "AdmissionReview", "response": {"uid": uid, "allowed": True}}
                        )
                    if config.allows_non_nim_controller(
                        review=review,
                        owner_chain=pre_resolved_owner_chain,
                        model_id=selected[1],
                    ):
                        return JSONResponse(
                            {"apiVersion": "admission.k8s.io/v1", "kind": "AdmissionReview", "response": {"uid": uid, "allowed": True}}
                        )
                    raise CatalogError(
                        "NIM-attributed descendant has an unsigned non-NIM controller"
                    )
            else:
                selected = config.select(review, catalog=catalog)
            if selected is None:
                return JSONResponse(
                    {"apiVersion": "admission.k8s.io/v1", "kind": "AdmissionReview", "response": {"uid": uid, "allowed": True}}
                )
            if not app.state.admission_activation_ready:
                raise CatalogError("NIM admission has not reached attributed HA activation")
            if (
                resource.get("group") == "apps.nvidia.com"
                and resource.get("resource") in {"nimcaches", "nimservices"}
            ):
                await resolver.verify_apply_fence(review=review, config=config)
            kind, model_id, envelope = selected
            subject = envelope["subject"]
            descendant_contract = next(
                (
                    contract
                    for contract in subject["descendant_resources"].values()
                    if resource
                    == {
                        "group": contract["group"],
                        "version": contract["version"],
                        "resource": contract["resource"],
                    }
                ),
                None,
            )
            actor_identity = subject["actor_identities"][
                descendant_contract["actor_identity"]
                if descendant_contract is not None
                else "custom_resource"
            ]
            actor_chain = await resolver.actor_chain(
                admission_request["userInfo"], actor_identity
            )
            owner_chain: Sequence[Mapping[str, Any]] = []
            if descendant_contract is not None:
                owner_chain = pre_resolved_owner_chain or await resolver.owner_chain(
                    admitted,
                    root_kind=kind,
                    namespace=config.namespace,
                )
            response = validate_nim_operator_admission_review(
                review,
                security_envelope=envelope,
                trusted_attestors=config.trusted_attestors,
                security_session_id=config.security_session_id,
                resource_kind=kind,
                record=catalog.model(model_id),
                resolved_owner_chain=owner_chain,
                resolved_actor_chain=actor_chain,
            )
            root: Mapping[str, Any] | None = None
            if descendant_contract is not None:
                root = owner_chain[-1]
            elif admission_request.get("operation") == "UPDATE":
                root = admitted
            if root is not None:
                await resolver.verify_root_enrollment(
                    root,
                    model_id=model_id,
                    subject_sha256=str(envelope["subject_sha256"]),
                    policy=config.admission_policy,
                )
            return JSONResponse(response)
        except (CatalogError, ValueError, KeyError, TypeError):
            return JSONResponse(
                {
                    "apiVersion": "admission.k8s.io/v1",
                    "kind": "AdmissionReview",
                    "response": {
                        "uid": uid,
                        "allowed": False,
                        "status": {"code": 403, "reason": "Forbidden", "message": "NIM workload differs from its signed admission contract"},
                    },
                }
            )

    return app


async def serve_nim_admission(
    *,
    config_file: Path,
    catalog_dir: Path,
    tls_certificate_file: Path,
    tls_private_key_file: Path,
    tls_ca_file: Path,
    kubernetes_api_url: str,
    kubernetes_token_file: Path,
    kubernetes_ca_file: Path,
    kubernetes_timeout_seconds: float,
    port: int,
    log_level: str,
) -> None:
    catalog = load_catalog(catalog_dir)
    config = NimAdmissionConfig.load(config_file, catalog_dir=catalog_dir)
    verify_mounted_tls_identity(
        policy=config.admission_policy,
        certificate_file=tls_certificate_file,
        private_key_file=tls_private_key_file,
        ca_file=tls_ca_file,
    )
    resolver = KubernetesNimOwnerResolver(
        base_url=kubernetes_api_url,
        token_file=kubernetes_token_file,
        ca_file=kubernetes_ca_file,
        timeout_seconds=kubernetes_timeout_seconds,
    )
    await resolver.verify_admission_policy(config)
    server = uvicorn.Server(
        uvicorn.Config(
            create_nim_admission_app(
                config=config,
                catalog=catalog,
                resolver=resolver,
                tls_identity_verifier=lambda: verify_mounted_tls_identity(
                    policy=config.admission_policy,
                    certificate_file=tls_certificate_file,
                    private_key_file=tls_private_key_file,
                    ca_file=tls_ca_file,
                ),
            ),
            host="0.0.0.0",  # noqa: S104 - cluster-internal TLS Service only
            port=port,
            ssl_certfile=str(tls_certificate_file),
            ssl_keyfile=str(tls_private_key_file),
            log_level=log_level.lower(),
        )
    )
    try:
        await server.serve()
    finally:
        await resolver.close()
