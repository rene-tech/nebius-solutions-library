"""External preventive gateway for the model-network Kubernetes boundary.

This process is deployed on provider-owned hosts, outside the Kubernetes
cluster.  The provider firewall makes the complete member set the cluster
API's only external callers.  A fronting mTLS proxy verifies client certificates,
removes all inbound identity/impersonation headers, and supplies only the
certificate SHA-256 header on this loopback-only ASGI listener.

The gateway is deliberately not a Kubernetes admission controller and cannot
govern in-cluster calls to kubernetes.default.svc.  It denies
mutations to the complete frozen inventory before they reach kube-apiserver and
admits only resourceVersion-CAS PATCH requests to the two retained
operation Leases from their distinct provider principals.  Global custody is
claimed only when the verifier also proves that no Group or ServiceAccount has
RBAC authority to mutate the boundary during the same freeze.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import unquote

import httpx
from cryptography import x509
from cryptography.hazmat.primitives import hashes
from fastapi import FastAPI, Header, Request
from fastapi.responses import JSONResponse, Response

_CERTIFICATE_SHA256 = re.compile(r"^[a-f0-9]{64}$")
_HOLDER = re.compile(r"^[a-z][a-z0-9]{5,31}:[1-9][0-9]*:[a-f0-9]{32}$")
_RESOURCE_NAME = re.compile(r"^[A-Za-z0-9]([-A-Za-z0-9_.]*[A-Za-z0-9])?$")
_LOCK_RESOURCES = frozenset(
    {
        "leases.coordination.k8s.io/fs2-system/fs2-model-network-transition",
        "leases.coordination.k8s.io/fs2-system/fs2-model-network-maintenance",
    }
)
_MUTATING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


class ProviderCustodyError(RuntimeError):
    """A provider-gateway request is outside the frozen custody contract."""


def _is_exact_host_route(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        network = ipaddress.ip_network(value, strict=True)
    except ValueError:
        return False
    return network.prefixlen == network.max_prefixlen


def _mapping(value: object, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ProviderCustodyError(f"{label} is missing or malformed")
    return value


def _is_resource_identity(value: object) -> bool:
    if not isinstance(value, str):
        return False
    parts = value.split("/")
    return (
        len(parts) == 3
        and _RESOURCE_NAME.fullmatch(parts[0]) is not None
        and (
            parts[1] == "_cluster"
            or _RESOURCE_NAME.fullmatch(parts[1]) is not None
        )
        and _RESOURCE_NAME.fullmatch(parts[2]) is not None
    )


def _exact_file(name: str) -> Path:
    path = Path(os.environ.get(name, ""))
    if (
        not path.is_absolute()
        or path.is_symlink()
        or not path.is_file()
        or path.stat().st_mode & 0o077
    ):
        raise ProviderCustodyError(f"{name} must be an absolute mode-0600 regular file")
    return path


@dataclass(frozen=True)
class Principal:
    principal_id: str
    username: str
    groups: tuple[str, ...]
    status_reader: bool


@dataclass(frozen=True)
class GatewayPolicy:
    raw_sha256: str
    value: Mapping[str, Any]
    principals: Mapping[str, Principal]
    frozen_resources: frozenset[str]
    frozen_resource_prefixes: tuple[str, ...]
    member_id: str
    member: Mapping[str, Any]

    @classmethod
    def load(cls) -> GatewayPolicy:
        path = _exact_file("FS2_PROVIDER_CUSTODY_POLICY")
        raw = path.read_bytes()
        expected = os.environ.get("FS2_PROVIDER_CUSTODY_POLICY_SHA256", "")
        digest = hashlib.sha256(raw).hexdigest()
        if _CERTIFICATE_SHA256.fullmatch(expected) is None or digest != expected:
            raise ProviderCustodyError("provider custody policy digest differs from release")
        try:
            value = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ProviderCustodyError("provider custody policy is not JSON") from exc
        if not isinstance(value, Mapping) or value.get("schema") != (
            "fs2-serve.nebius.ai/model-network-provider-gateway-policy/v2"
        ):
            raise ProviderCustodyError("provider custody policy schema is not exact")
        if (
            set(value)
            != {
                "schema",
                "cluster_id",
                "policy_id",
                "policy_revision",
                "upstream_api_url",
                "direct_control_plane_access",
                "gateway_members",
                "provider_inventory_sha256",
                "kubernetes_authorization_sha256",
                "cluster_resource_version",
                "control_plane_allowed_cidrs",
                "principals",
                "mutation_freeze",
                "operation_locks",
            }
            or not isinstance(value.get("cluster_id"), str)
            or re.fullmatch(r"mk8scluster-[a-z0-9]+", value["cluster_id"]) is None
            or not isinstance(value.get("policy_id"), str)
            or not 1 <= len(value["policy_id"]) <= 253
            or not isinstance(value.get("policy_revision"), str)
            or not 1 <= len(value["policy_revision"]) <= 253
        ):
            raise ProviderCustodyError("provider custody policy identity is not exact")
        if value.get("direct_control_plane_access") != (
            "provider-firewall-all-external-paths-plus-zero-in-cluster-authority"
        ):
            raise ProviderCustodyError("provider custody policy does not close direct API access")
        upstream_api_url = value.get("upstream_api_url")
        if (
            not isinstance(upstream_api_url, str)
            or re.fullmatch(r"https://[^/?#]+(?::[0-9]{1,5})?", upstream_api_url) is None
        ):
            raise ProviderCustodyError("provider custody upstream API URL is not exact HTTPS")
        provider_inventory_sha256 = value.get("provider_inventory_sha256")
        kubernetes_authorization_sha256 = value.get(
            "kubernetes_authorization_sha256"
        )
        raw_members = value.get("gateway_members")
        if (
            not isinstance(provider_inventory_sha256, str)
            or _CERTIFICATE_SHA256.fullmatch(provider_inventory_sha256) is None
            or not isinstance(kubernetes_authorization_sha256, str)
            or _CERTIFICATE_SHA256.fullmatch(kubernetes_authorization_sha256)
            is None
            or not isinstance(raw_members, list)
            or not 2 <= len(raw_members) <= 8
            or not all(isinstance(member, Mapping) for member in raw_members)
        ):
            raise ProviderCustodyError("provider custody resource inventory is incomplete")
        member_ids: list[str] = []
        member_cidrs: list[str] = []
        member_urls: list[str] = []
        for member in raw_members:
            if set(member) != {
                "member_id",
                "status_url",
                "host_cidr",
                "server_certificate_sha256",
                "iam_principal_id",
                "instance",
                "security_group",
                "security_rules",
                "access_permits",
            }:
                raise ProviderCustodyError("provider gateway member inventory is not exact")
            member_id = member.get("member_id")
            status_url = member.get("status_url")
            host_cidr = member.get("host_cidr")
            provider_objects = [
                member.get("instance"),
                member.get("security_group"),
                *(member.get("security_rules", []) if isinstance(member.get("security_rules"), list) else []),
                *(member.get("access_permits", []) if isinstance(member.get("access_permits"), list) else []),
            ]
            if (
                not isinstance(member_id, str)
                or re.fullmatch(r"[a-z][a-z0-9-]{2,62}", member_id) is None
                or not isinstance(status_url, str)
                or re.fullmatch(r"https://[^/?#]+/v1/custody/status", status_url) is None
                or not _is_exact_host_route(host_cidr)
                or not isinstance(member.get("server_certificate_sha256"), str)
                or _CERTIFICATE_SHA256.fullmatch(member["server_certificate_sha256"])
                is None
                or not isinstance(member.get("iam_principal_id"), str)
                or not member["iam_principal_id"]
                or not isinstance(member.get("security_rules"), list)
                or not member["security_rules"]
                or not isinstance(member.get("access_permits"), list)
                or not member["access_permits"]
                or not all(
                    isinstance(item, Mapping)
                    and set(item) == {"id", "resource_version", "semantic_sha256"}
                    and isinstance(item.get("id"), str)
                    and bool(item["id"])
                    and isinstance(item.get("resource_version"), int)
                    and not isinstance(item.get("resource_version"), bool)
                    and item["resource_version"] >= 0
                    and isinstance(item.get("semantic_sha256"), str)
                    and _CERTIFICATE_SHA256.fullmatch(item["semantic_sha256"]) is not None
                    for item in provider_objects
                )
            ):
                raise ProviderCustodyError("provider gateway member inventory is malformed")
            member_ids.append(member_id)
            member_urls.append(status_url)
            member_cidrs.append(str(host_cidr))
        if (
            member_ids != sorted(member_ids)
            or len(member_ids) != len(set(member_ids))
            or len(member_urls) != len(set(member_urls))
            or len(member_cidrs) != len(set(member_cidrs))
        ):
            raise ProviderCustodyError("provider gateway members are not unique and sorted")
        local_member_id = os.environ.get("FS2_PROVIDER_CUSTODY_MEMBER_ID", "")
        local_members = [
            member for member in raw_members if member.get("member_id") == local_member_id
        ]
        if len(local_members) != 1:
            raise ProviderCustodyError("this gateway host has no unique provider member identity")
        if (
            not isinstance(value.get("cluster_resource_version"), int)
            or isinstance(value.get("cluster_resource_version"), bool)
            or value["cluster_resource_version"] < 0
        ):
            raise ProviderCustodyError(
                "provider custody policy has no exact cluster resource version"
            )
        cidrs = value.get("control_plane_allowed_cidrs")
        if (
            not isinstance(cidrs, list)
            or len(cidrs) < 2
            or len(cidrs) != len(set(cidrs))
            or cidrs != sorted(cidrs)
            or any(not _is_exact_host_route(cidr) for cidr in cidrs)
            or cidrs != sorted(member_cidrs)
        ):
            raise ProviderCustodyError("gateway egress must be a finite redundant host-route set")
        raw_principals = _mapping(value.get("principals"), "principals")
        principals: dict[str, Principal] = {}
        for certificate_sha256, raw_principal in raw_principals.items():
            item = _mapping(raw_principal, "principal")
            groups = item.get("groups")
            if (
                not isinstance(certificate_sha256, str)
                or _CERTIFICATE_SHA256.fullmatch(certificate_sha256) is None
                or set(item) != {"principal_id", "username", "groups", "status_reader"}
                or not isinstance(item.get("principal_id"), str)
                or not item["principal_id"].startswith("spiffe://")
                or not isinstance(item.get("username"), str)
                or not item["username"]
                or not isinstance(groups, list)
                or not groups
                or not all(isinstance(group, str) and group for group in groups)
                or len(groups) != len(set(groups))
                or not isinstance(item.get("status_reader"), bool)
            ):
                raise ProviderCustodyError("provider custody principal inventory is malformed")
            principals[certificate_sha256] = Principal(
                item["principal_id"],
                item["username"],
                tuple(groups),
                item["status_reader"],
            )
        if (
            len(principals) != 6
            or len({principal.principal_id for principal in principals.values()}) != 6
            or len({principal.username for principal in principals.values()}) != 6
            or sum(principal.status_reader for principal in principals.values()) != 1
        ):
            raise ProviderCustodyError(
                "provider custody requires six distinct principals and one status reader"
            )
        freeze = _mapping(value.get("mutation_freeze"), "mutation_freeze")
        frozen = freeze.get("protected_kubernetes_resources")
        frozen_prefixes = freeze.get("protected_kubernetes_resource_prefixes")
        try:
            active_from = datetime.fromisoformat(
                str(freeze["active_from"]).replace("Z", "+00:00")
            )
            active_until = datetime.fromisoformat(
                str(freeze["active_until"]).replace("Z", "+00:00")
            )
        except (KeyError, ValueError) as exc:
            raise ProviderCustodyError(
                "provider full-inventory freeze time is malformed"
            ) from exc
        if (
            set(freeze)
            != {
                "transaction_id",
                "active_from",
                "active_until",
                "enforcement",
                "allowed_principal_ids",
                "protected_kubernetes_resources",
                "protected_kubernetes_resource_prefixes",
            }
            or not isinstance(freeze.get("transaction_id"), str)
            or _HOLDER.fullmatch(freeze["transaction_id"]) is None
            or freeze.get("enforcement")
            != "deny-all-protected-kubernetes-mutations"
            or freeze.get("allowed_principal_ids") != []
            or not isinstance(frozen, list)
            or len(frozen) < 5
            or len(frozen) > 128
            or not all(isinstance(item, str) for item in frozen)
            or len(frozen) != len(set(frozen))
            or frozen != sorted(frozen)
            or not all(_is_resource_identity(item) for item in frozen)
            or _LOCK_RESOURCES.intersection(frozen)
            or active_from.tzinfo is None
            or active_until.tzinfo is None
            or active_from >= active_until
            or active_until - active_from > timedelta(hours=2)
            or frozen_prefixes
            != [
                "clusterrolebindings.rbac.authorization.k8s.io/_cluster/",
                "clusterroles.rbac.authorization.k8s.io/_cluster/",
                "rolebindings.rbac.authorization.k8s.io/",
                "roles.rbac.authorization.k8s.io/",
            ]
        ):
            raise ProviderCustodyError("provider full-inventory freeze is malformed")
        locks = _mapping(value.get("operation_locks"), "operation_locks")
        lock_values = _mapping(locks.get("locks"), "operation_locks.locks")
        if (
            locks.get("enforcement")
            != "provider-gateway-exact-object-resource-version-cas"
            or locks.get("maximum_lease_duration_seconds") != 7200
            or locks.get("create_allowed") is not False
            or locks.get("delete_allowed") is not False
            or set(lock_values) != _LOCK_RESOURCES
            or any(
                not isinstance(lock, Mapping)
                or set(lock) != {"principal_id", "operations"}
                or lock.get("operations") != ["PATCH"]
                or lock.get("principal_id")
                not in {principal.principal_id for principal in principals.values()}
                for lock in lock_values.values()
            )
        ):
            raise ProviderCustodyError("provider operation-lock policy is malformed")
        return cls(
            digest,
            value,
            principals,
            frozenset(frozen),
            tuple(frozen_prefixes),
            local_member_id,
            local_members[0],
        )


def _resource_identity(path: str, body: bytes, method: str) -> str | None:
    """Map one canonical Kubernetes REST request to group/ns/resource/name."""

    decoded = unquote(path)
    if decoded != path or "//" in decoded or not decoded.startswith("/"):
        raise ProviderCustodyError("Kubernetes API path is not canonical")
    parts = decoded.strip("/").split("/")
    group = ""
    namespace = "_cluster"
    resource = ""
    name: str | None = None
    if len(parts) >= 3 and parts[:2] == ["api", "v1"]:
        cursor = 2
    elif len(parts) >= 4 and parts[0] == "apis":
        group = parts[1]
        cursor = 3
    else:
        return None
    if len(parts) > cursor and parts[cursor] == "namespaces":
        if len(parts) < cursor + 3:
            return None
        namespace = parts[cursor + 1]
        resource = parts[cursor + 2]
        name = parts[cursor + 3] if len(parts) > cursor + 3 else None
    elif len(parts) > cursor:
        resource = parts[cursor]
        name = parts[cursor + 1] if len(parts) > cursor + 1 else None
    if method == "POST" and name is None:
        try:
            document = _mapping(json.loads(body), "mutating Kubernetes request")
            name = _mapping(document.get("metadata"), "request metadata").get("name")
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ProviderCustodyError("mutating Kubernetes body is not exact JSON") from exc
    if (
        not isinstance(resource, str)
        or _RESOURCE_NAME.fullmatch(resource) is None
        or not isinstance(name, str)
        or _RESOURCE_NAME.fullmatch(name) is None
        or not isinstance(namespace, str)
        or (
            namespace != "_cluster"
            and _RESOURCE_NAME.fullmatch(namespace) is None
        )
    ):
        return None
    qualified = f"{resource}{'.' + group if group else ''}"
    return f"{qualified}/{namespace}/{name}"


class ProviderCustodyGateway:
    def __init__(self, policy: GatewayPolicy, client: httpx.AsyncClient | None = None) -> None:
        self.policy = policy
        self.token_file = _exact_file("FS2_PROVIDER_CUSTODY_UPSTREAM_TOKEN")
        self._owns_client = client is None
        self.client = client or httpx.AsyncClient(
            base_url=str(policy.value["upstream_api_url"]).rstrip("/"),
            verify=str(_exact_file("FS2_PROVIDER_CUSTODY_UPSTREAM_CA")),
            timeout=httpx.Timeout(10),
            trust_env=False,
        )

    async def close(self) -> None:
        if self._owns_client:
            await self.client.aclose()

    def principal(self, escaped_certificate: str | None) -> Principal:
        if escaped_certificate is None:
            raise ProviderCustodyError("provider mTLS client identity is absent")
        try:
            certificate = x509.load_pem_x509_certificate(
                unquote(escaped_certificate).encode("ascii")
            )
            certificate_sha256 = certificate.fingerprint(hashes.SHA256()).hex()
        except (ValueError, UnicodeError) as exc:
            raise ProviderCustodyError("provider mTLS client certificate is malformed") from exc
        principal = self.policy.principals.get(certificate_sha256)
        if principal is None:
            raise ProviderCustodyError("provider mTLS client identity is not authorized")
        return principal

    def status(self, principal: Principal, document: Mapping[str, Any]) -> Mapping[str, Any]:
        challenge = document.get("challenge")
        if (
            not principal.status_reader
            or document.get("cluster_id") != self.policy.value.get("cluster_id")
            or not isinstance(challenge, str)
            or re.fullmatch(r"[a-f0-9]{64}", challenge) is None
        ):
            raise ProviderCustodyError("provider custody status request is unauthorized")
        return {
            "schema": "fs2-serve.nebius.ai/model-network-provider-gateway-status/v2",
            "challenge": challenge,
            "cluster_id": self.policy.value["cluster_id"],
            "policy_id": self.policy.value["policy_id"],
            "policy_revision": self.policy.value["policy_revision"],
            "policy_sha256": self.policy.raw_sha256,
            "member_id": self.policy.member_id,
            "member": self.policy.member,
            "provider_inventory_sha256": self.policy.value[
                "provider_inventory_sha256"
            ],
            "kubernetes_authorization_sha256": self.policy.value[
                "kubernetes_authorization_sha256"
            ],
            "gateway_members": self.policy.value["gateway_members"],
            "cluster_resource_version": self.policy.value[
                "cluster_resource_version"
            ],
            "control_plane_allowed_cidrs": self.policy.value[
                "control_plane_allowed_cidrs"
            ],
            "direct_control_plane_access": self.policy.value[
                "direct_control_plane_access"
            ],
            "principal_bindings": {
                certificate_sha256: {
                    "principal_id": principal.principal_id,
                    "username": principal.username,
                    "groups": list(principal.groups),
                    "status_reader": principal.status_reader,
                }
                for certificate_sha256, principal in sorted(
                    self.policy.principals.items()
                )
            },
            "mutation_freeze": self.policy.value["mutation_freeze"],
            "operation_locks": self.policy.value["operation_locks"],
        }

    @staticmethod
    def _validate_lock_json_patch(
        document: object, *, principal: Principal
    ) -> None:
        if not isinstance(document, list) or not document:
            raise ProviderCustodyError("operation Lease JSON Patch is malformed")
        allowed_paths = {
            "/metadata/resourceVersion",
            "/metadata/annotations",
            "/metadata/annotations/fs2-serve.nebius.ai~1network-transition-writer",
            "/metadata/annotations/fs2-serve.nebius.ai~1network-transition-holder",
            "/spec/holderIdentity",
            "/spec/leaseDurationSeconds",
            "/spec/acquireTime",
            "/spec/renewTime",
            "/spec/leaseTransitions",
        }
        resource_version_tests = 0
        holder_tests = 0
        holder_mutations = 0
        for raw_operation in document:
            operation = _mapping(raw_operation, "operation Lease JSON Patch entry")
            if set(operation) != {"op", "path", "value"}:
                raise ProviderCustodyError("operation Lease JSON Patch entry is not exact")
            verb = operation.get("op")
            path = operation.get("path")
            value = operation.get("value")
            if verb not in {"add", "replace", "test"} or path not in allowed_paths:
                raise ProviderCustodyError("operation Lease JSON Patch path is not authorized")
            if path == "/metadata/resourceVersion":
                if verb != "test" or not isinstance(value, str) or not value:
                    raise ProviderCustodyError("operation Lease has no resourceVersion CAS")
                resource_version_tests += 1
            elif path.endswith("network-transition-writer"):
                if verb != "add" or value != principal.username:
                    raise ProviderCustodyError("operation Lease writer annotation is not exact")
            elif path.endswith("network-transition-holder") or path == "/spec/holderIdentity":
                if not isinstance(value, str) or (value and _HOLDER.fullmatch(value) is None):
                    raise ProviderCustodyError("operation Lease holder is malformed")
                if path == "/spec/holderIdentity":
                    holder_tests += verb == "test"
                    holder_mutations += verb in {"add", "replace"}
            elif path == "/metadata/annotations":
                annotations = _mapping(value, "operation Lease annotations")
                holder = annotations.get(
                    "fs2-serve.nebius.ai/network-transition-holder"
                )
                if (
                    set(annotations)
                    != {
                        "fs2-serve.nebius.ai/network-transition-writer",
                        "fs2-serve.nebius.ai/network-transition-holder",
                    }
                    or annotations.get(
                        "fs2-serve.nebius.ai/network-transition-writer"
                    )
                    != principal.username
                    or not isinstance(holder, str)
                    or (holder and _HOLDER.fullmatch(holder) is None)
                ):
                    raise ProviderCustodyError(
                        "operation Lease annotations are outside the lock contract"
                    )
            elif path == "/spec/leaseDurationSeconds":
                if (
                    not isinstance(value, int)
                    or isinstance(value, bool)
                    or not 1 <= value <= 7200
                ):
                    raise ProviderCustodyError("operation Lease duration exceeds policy")
            elif path == "/spec/leaseTransitions" and (
                not isinstance(value, int) or isinstance(value, bool) or value < 0
            ):
                raise ProviderCustodyError("operation Lease transition count is malformed")
            elif path in {"/spec/acquireTime", "/spec/renewTime"} and (
                not isinstance(value, str)
                or re.fullmatch(
                    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9:.]+Z", value
                )
                is None
            ):
                raise ProviderCustodyError("operation Lease timestamp is malformed")
        if resource_version_tests != 1:
            raise ProviderCustodyError(
                "operation Lease requires exactly one resourceVersion CAS test"
            )
        if holder_tests != 1 or holder_mutations != 1:
            raise ProviderCustodyError(
                "operation Lease requires one holder test and one holder mutation"
            )

    async def proxy(
        self,
        principal: Principal,
        method: str,
        path: str,
        query: str,
        body: bytes,
        headers: Mapping[str, str],
    ) -> httpx.Response:
        identity = (
            _resource_identity(path, body, method)
            if method in _MUTATING_METHODS
            else None
        )
        if method in _MUTATING_METHODS and identity is None:
            raise ProviderCustodyError(
                "provider gateway rejects unresolvable or collection-wide mutations"
            )
        now = datetime.now(UTC)
        freeze = _mapping(self.policy.value["mutation_freeze"], "mutation_freeze")
        try:
            active_from = datetime.fromisoformat(
                str(freeze["active_from"]).replace("Z", "+00:00")
            )
            active_until = datetime.fromisoformat(
                str(freeze["active_until"]).replace("Z", "+00:00")
            )
        except (KeyError, ValueError) as exc:
            raise ProviderCustodyError("provider mutation-freeze time is malformed") from exc
        if (
            method in _MUTATING_METHODS
            and identity is not None
            and identity in self.policy.frozen_resources
        ):
            if active_from <= now < active_until:
                raise ProviderCustodyError("provider full-inventory freeze denies this mutation")
            raise ProviderCustodyError(
                "provider custody objects require a separately versioned policy"
            )
        if (
            method in _MUTATING_METHODS
            and identity is not None
            and any(
                identity.startswith(prefix)
                for prefix in self.policy.frozen_resource_prefixes
            )
        ):
            raise ProviderCustodyError(
                "provider custody requires a separately versioned policy for RBAC collection mutation"
            )
        if identity in _LOCK_RESOURCES:
            lock = _mapping(
                _mapping(self.policy.value["operation_locks"], "operation_locks")[
                    "locks"
                ][identity],
                "operation lock",
            )
            if method != "PATCH" or principal.principal_id != lock.get("principal_id"):
                raise ProviderCustodyError(
                    "operation Lease mutation has the wrong method or principal"
                )
            content_type = headers.get("content-type", "").split(";", 1)[0]
            if content_type != "application/json-patch+json":
                raise ProviderCustodyError(
                    "operation Lease mutation must be a resourceVersion-CAS JSON Patch"
                )
            try:
                mutation = json.loads(body)
            except json.JSONDecodeError as exc:
                raise ProviderCustodyError("operation Lease mutation is not JSON") from exc
            self._validate_lock_json_patch(mutation, principal=principal)
        token = self.token_file.read_text(encoding="utf-8").strip()
        if len(token) < 16:
            raise ProviderCustodyError("provider gateway upstream credential is unavailable")
        forwarded_headers = [
            ("authorization", f"Bearer {token}"),
            ("accept", headers.get("accept", "application/json")),
            ("content-type", headers.get("content-type", "application/json")),
            ("impersonate-user", principal.username),
        ]
        for group in principal.groups:
            forwarded_headers.append(("impersonate-group", group))
        target = path + (f"?{query}" if query else "")
        return await self.client.request(method, target, content=body, headers=forwarded_headers)


def create_provider_custody_app(gateway: ProviderCustodyGateway) -> FastAPI:
    app = FastAPI(openapi_url=None, docs_url=None, redoc_url=None)

    @app.on_event("shutdown")
    async def shutdown() -> None:
        await gateway.close()

    @app.post("/v1/custody/status")
    async def status(
        request: Request,
        x_fs2_provider_client_certificate: str | None = Header(default=None),
    ) -> JSONResponse:
        principal = gateway.principal(x_fs2_provider_client_certificate)
        try:
            body = await request.json()
        except (ValueError, json.JSONDecodeError) as exc:
            raise ProviderCustodyError("provider custody status body is malformed") from exc
        return JSONResponse(gateway.status(principal, _mapping(body, "status request")))

    @app.api_route(
        "/{path:path}", methods=["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"]
    )
    async def proxy(
        path: str,
        request: Request,
        x_fs2_provider_client_certificate: str | None = Header(default=None),
    ) -> Response:
        principal = gateway.principal(x_fs2_provider_client_certificate)
        body = await request.body()
        if len(body) > 16 * 1024 * 1024:
            raise ProviderCustodyError("provider gateway request body exceeds 16 MiB")
        response = await gateway.proxy(
            principal,
            request.method,
            "/" + path,
            request.url.query,
            body,
            {key.lower(): value for key, value in request.headers.items()},
        )
        return Response(
            response.content,
            status_code=response.status_code,
            headers={
                key: value
                for key, value in response.headers.items()
                if key.lower() in {"content-type", "etag", "warning", "audit-id"}
            },
        )

    @app.exception_handler(ProviderCustodyError)
    async def rejected(_request: Request, error: ProviderCustodyError) -> JSONResponse:
        return JSONResponse({"error": str(error)}, status_code=403)

    return app


__all__ = [
    "GatewayPolicy",
    "ProviderCustodyError",
    "ProviderCustodyGateway",
    "create_provider_custody_app",
]
