"""Authoritative Kubernetes drain evidence for the 0031 schema bridge.

The bridge-ready writer is intentionally read-only in Kubernetes.  It proves
that one exact Deployment generation is fully available on the bridge image,
that no predecessor Pod remains, and binds the API server's observation time
and audit identifiers into the append-only PostgreSQL receipt.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx

from .scientific_artifacts import SchemaBridgeDrainEvidence

_DNS_LABEL = re.compile(r"^[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?$")
_DNS_SUBDOMAIN = re.compile(r"^[a-z0-9](?:[-a-z0-9.]{0,251}[a-z0-9])?$")
_IMAGE_DIGEST = re.compile(r"^[^@\s]+@sha256:[a-f0-9]{64}$")
_IDENTITY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$")
_MAX_RESPONSE_BYTES = 4 * 1024 * 1024
_MAX_RUNTIME_PODS = 256


def _mapping(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _sequence(value: object) -> Sequence[Any]:
    return value if isinstance(value, list) else ()


def _required_integer(value: object, label: str, *, minimum: int = 0) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
        raise RuntimeError(f"Kubernetes {label} is invalid")
    return value


def _api_observed_at(response: httpx.Response) -> datetime:
    raw = response.headers.get("date")
    if raw is None:
        raise RuntimeError("Kubernetes API response date is absent")
    try:
        observed_at = parsedate_to_datetime(raw)
    except (TypeError, ValueError, OverflowError):
        raise RuntimeError("Kubernetes API response date is invalid") from None
    if observed_at.tzinfo is None:
        raise RuntimeError("Kubernetes API response date is not timezone-aware")
    return observed_at.astimezone(UTC)


def _audit_id(response: httpx.Response) -> str:
    audit_id = response.headers.get("audit-id", "")
    if _IDENTITY.fullmatch(audit_id) is None:
        raise RuntimeError("Kubernetes API audit identity is absent or invalid")
    return audit_id


@dataclass(frozen=True)
class _DeploymentSnapshot:
    uid: str
    resource_version: str
    generation: int
    observed_generation: int
    desired_replicas: int
    updated_replicas: int
    ready_replicas: int
    available_replicas: int


@dataclass(frozen=True)
class KubernetesSchemaBridgeReader:
    """Read only the release Deployment and its exact runtime Pod selector."""

    base_url: str
    token_file: Path
    ca_file: Path
    namespace: str
    deployment_name: str
    release_name: str
    bridge_image_ref: str
    predecessor_image_ref: str
    timeout_seconds: float = 10

    def __post_init__(self) -> None:
        parsed = urlsplit(self.base_url)
        if (
            parsed.scheme != "https"
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or parsed.path not in {"", "/"}
        ):
            raise ValueError("schema bridge Kubernetes API URL must be credential-free HTTPS")
        if _DNS_LABEL.fullmatch(self.namespace) is None:
            raise ValueError("schema bridge namespace is invalid")
        if _DNS_SUBDOMAIN.fullmatch(self.deployment_name) is None:
            raise ValueError("schema bridge Deployment name is invalid")
        if _DNS_LABEL.fullmatch(self.release_name) is None:
            raise ValueError("schema bridge release name is invalid")
        if (
            _IMAGE_DIGEST.fullmatch(self.bridge_image_ref) is None
            or _IMAGE_DIGEST.fullmatch(self.predecessor_image_ref) is None
            or self.bridge_image_ref == self.predecessor_image_ref
        ):
            raise ValueError("schema bridge image identities are invalid")

    def _headers(self) -> dict[str, str]:
        try:
            token = self.token_file.read_text(encoding="utf-8").strip()
        except OSError as error:
            raise RuntimeError("schema bridge service-account token is unavailable") from error
        if not 32 <= len(token) <= 16 * 1024:
            raise RuntimeError("schema bridge service-account token is invalid")
        return {"authorization": f"Bearer {token}", "accept": "application/json"}

    async def _get(
        self, client: httpx.AsyncClient, path: str, *, params: Mapping[str, str] | None = None
    ) -> tuple[Mapping[str, Any], httpx.Response]:
        try:
            response = await client.get(path, params=params, headers=self._headers())
        except (OSError, httpx.HTTPError) as error:
            raise RuntimeError("schema bridge Kubernetes read failed") from error
        if response.status_code != 200 or len(response.content) > _MAX_RESPONSE_BYTES:
            raise RuntimeError("schema bridge Kubernetes read was rejected")
        try:
            document = response.json()
        except (json.JSONDecodeError, UnicodeDecodeError, RecursionError):
            raise RuntimeError("schema bridge Kubernetes response is invalid") from None
        return _mapping(document), response

    @staticmethod
    def _deployment(document: Mapping[str, Any]) -> _DeploymentSnapshot:
        metadata = _mapping(document.get("metadata"))
        spec = _mapping(document.get("spec"))
        status = _mapping(document.get("status"))
        uid = metadata.get("uid")
        resource_version = metadata.get("resourceVersion")
        if (
            not isinstance(uid, str)
            or _IDENTITY.fullmatch(uid) is None
            or not isinstance(resource_version, str)
            or _IDENTITY.fullmatch(resource_version) is None
        ):
            raise RuntimeError("schema bridge Deployment identity is invalid")
        unavailable = status.get("unavailableReplicas", 0)
        if _required_integer(unavailable, "Deployment unavailable replicas") != 0:
            raise RuntimeError("schema bridge Deployment still has unavailable replicas")
        return _DeploymentSnapshot(
            uid=uid,
            resource_version=resource_version,
            generation=_required_integer(metadata.get("generation"), "Deployment generation", minimum=1),
            observed_generation=_required_integer(
                status.get("observedGeneration"), "Deployment observed generation", minimum=1
            ),
            desired_replicas=_required_integer(spec.get("replicas"), "Deployment replicas", minimum=1),
            updated_replicas=_required_integer(
                status.get("updatedReplicas", 0), "Deployment updated replicas"
            ),
            ready_replicas=_required_integer(status.get("readyReplicas", 0), "Deployment ready replicas"),
            available_replicas=_required_integer(
                status.get("availableReplicas", 0), "Deployment available replicas"
            ),
        )

    def _pod_set_digest(self, document: Mapping[str, Any], desired: int) -> tuple[int, str]:
        if _mapping(document.get("metadata")).get("continue"):
            raise RuntimeError("schema bridge Pod inventory is paginated")
        pods = _sequence(document.get("items"))
        if not 1 <= len(pods) <= _MAX_RUNTIME_PODS or len(pods) != desired:
            raise RuntimeError("schema bridge runtime Pod count differs from the Deployment")
        identities: list[dict[str, object]] = []
        for raw_pod in pods:
            pod = _mapping(raw_pod)
            metadata = _mapping(pod.get("metadata"))
            spec = _mapping(pod.get("spec"))
            status = _mapping(pod.get("status"))
            name = metadata.get("name")
            uid = metadata.get("uid")
            resource_version = metadata.get("resourceVersion")
            if (
                not isinstance(name, str)
                or _DNS_SUBDOMAIN.fullmatch(name) is None
                or not isinstance(uid, str)
                or _IDENTITY.fullmatch(uid) is None
                or not isinstance(resource_version, str)
                or _IDENTITY.fullmatch(resource_version) is None
                or metadata.get("deletionTimestamp") is not None
                or status.get("phase") != "Running"
            ):
                raise RuntimeError("schema bridge runtime Pod identity or phase is invalid")
            ready = any(
                _mapping(condition).get("type") == "Ready"
                and _mapping(condition).get("status") == "True"
                for condition in _sequence(status.get("conditions"))
            )
            if not ready:
                raise RuntimeError("schema bridge runtime Pod is not Ready")
            images: list[str] = []
            for raw_container in (*_sequence(spec.get("initContainers")), *_sequence(spec.get("containers"))):
                image = _mapping(raw_container).get("image")
                if not isinstance(image, str) or image != self.bridge_image_ref:
                    raise RuntimeError("schema bridge runtime Pod does not use the exact bridge image")
                if image == self.predecessor_image_ref:
                    raise RuntimeError("schema bridge predecessor image is still serving")
                images.append(image)
            if not images:
                raise RuntimeError("schema bridge runtime Pod has no containers")
            identities.append(
                {
                    "images": images,
                    "name": name,
                    "resource_version": resource_version,
                    "uid": uid,
                }
            )
        payload = json.dumps(sorted(identities, key=lambda item: str(item["uid"])), separators=(",", ":"))
        return len(identities), hashlib.sha256(payload.encode("utf-8")).hexdigest()

    async def verify(self) -> SchemaBridgeDrainEvidence:
        deployment_path = (
            f"/apis/apps/v1/namespaces/{self.namespace}/deployments/{self.deployment_name}"
        )
        pod_path = f"/api/v1/namespaces/{self.namespace}/pods"
        selector = (
            "app.kubernetes.io/name=fs2-serve-control-plane,"
            f"app.kubernetes.io/instance={self.release_name},"
            "app.kubernetes.io/component=gateway"
        )
        async with httpx.AsyncClient(
            base_url=self.base_url.rstrip("/"),
            verify=str(self.ca_file),
            timeout=httpx.Timeout(self.timeout_seconds),
            trust_env=False,
        ) as client:
            first_document, _ = await self._get(client, deployment_path)
            first = self._deployment(first_document)
            pods_document, pods_response = await self._get(
                client,
                pod_path,
                params={"labelSelector": selector, "limit": str(_MAX_RUNTIME_PODS)},
            )
            pod_count, pod_set_digest = self._pod_set_digest(pods_document, first.desired_replicas)
            second_document, second_response = await self._get(client, deployment_path)
            second = self._deployment(second_document)
        if first != second:
            raise RuntimeError("schema bridge Deployment changed during the drain proof")
        return SchemaBridgeDrainEvidence(
            deployment_namespace=self.namespace,
            deployment_name=self.deployment_name,
            deployment_uid=second.uid,
            deployment_generation=second.generation,
            deployment_observed_generation=second.observed_generation,
            deployment_desired_replicas=second.desired_replicas,
            deployment_updated_replicas=second.updated_replicas,
            deployment_ready_replicas=second.ready_replicas,
            deployment_available_replicas=second.available_replicas,
            runtime_pod_count=pod_count,
            runtime_pod_set_digest=pod_set_digest,
            kubernetes_audit_id=f"{_audit_id(pods_response)}:{_audit_id(second_response)}",
            kubernetes_observed_at=_api_observed_at(second_response),
        )
