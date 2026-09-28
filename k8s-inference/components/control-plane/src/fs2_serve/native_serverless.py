"""Signed native Serverless qualification routes; no customer-ready promotion.

This is deployment configuration, not archival SM90 qualification evidence.
Only named qualification identities may admit it; separately named identities
may receive catalog metadata only. The native record,
artifact and two-fixture contract must already validate independently. Existing
Ed25519, route expiry, tenant grants and pinned-TLS federation remain in force.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from types import MappingProxyType
from typing import Any, Literal, NoReturn
from urllib.parse import urlsplit
from uuid import UUID

from fs2_serve_catalog.artifacts import canonical_bytes
from fs2_serve_catalog.attestations import verify_signed_attestation
from fs2_serve_catalog.consumer import GatewayCatalog, ServingBinding
from fs2_serve_catalog.loader import Catalog, _load_json, strong_sha256
from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_validator

from .lean_routes import _disabled_activation
from .native_catalog import _artifact

SCHEMA = "fs2-serve.nebius.ai/native-serverless-deployments/v1"
ENTRY_SCHEMA = "fs2-serve.nebius.ai/native-serverless-deployment/v1"
_DNS = r"^[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?$"
_SHA = r"^[a-f0-9]{64}$"


class NativeServerlessError(ValueError):
    pass


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)

    @field_validator("*")
    @classmethod
    def strong_hashes(cls, value: Any, info: ValidationInfo) -> Any:
        name = info.field_name or ""
        if name.endswith(("sha256", "digest")) or name == "session_id":
            strong_sha256(value, name, image=name == "runtime_image_digest")
        return value


class ServiceIdentity(_Strict):
    namespace: str = Field(pattern=_DNS)
    service_name: str = Field(pattern=_DNS)
    service_uid: str
    port: int = Field(ge=1, le=65535)

    @field_validator("service_uid")
    @classmethod
    def uid(cls, value: str) -> str:
        if str(UUID(value)) != value:
            raise ValueError("Service UID is not canonical")
        return value


class EndpointSubject(_Strict):
    provider: Literal["nebius-serverless"]
    project_id: str = Field(pattern=r"^project-[a-z0-9]+$")
    endpoint_id: str = Field(pattern=r"^aiendpoint-[a-z0-9]+$")
    origin: str = Field(max_length=2048)
    runtime_image_digest: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    checkpoint_sha256: str = Field(pattern=_SHA)
    cloud_observation_sha256: str = Field(pattern=_SHA)

    @field_validator("origin")
    @classmethod
    def origin_only(cls, value: str) -> str:
        parsed = urlsplit(value)
        host = parsed.hostname
        if (
            parsed.scheme != "https"
            or parsed.port not in (None, 443)
            or not host
            or parsed.username
            or parsed.password
            or parsed.path
            or parsed.query
            or parsed.fragment
            or parsed.netloc != host
            or any(re.fullmatch(_DNS, item) is None for item in host.split("."))
        ):
            raise ValueError("Serverless origin must be canonical HTTPS without path or credentials")
        return value


class CredentialIdentity(_Strict):
    requirement_id: str = Field(pattern=r"^fs2-models/[a-z0-9][-a-z0-9]{0,61}[a-z0-9]$")
    secret_uid: str
    resource_version: str = Field(min_length=1, max_length=100)
    gateway_secret_uid: str
    observation_sha256: str = Field(pattern=_SHA)

    @field_validator("secret_uid", "gateway_secret_uid")
    @classmethod
    def uid(cls, value: str) -> str:
        return ServiceIdentity.uid(value)


class QualificationAccess(_Strict):
    tenant_id: str = Field(min_length=1, max_length=120)
    principal_id: str = Field(min_length=1, max_length=200)


class Deployment(_Strict):
    model_digest: str = Field(pattern=_SHA)
    variant_id: str = Field(pattern=r"^[a-z0-9][-a-z0-9]{0,126}[a-z0-9]$")
    artifact_manifest_sha256: str = Field(pattern=_SHA)
    discovery_service: ServiceIdentity
    endpoint: EndpointSubject
    credential: CredentialIdentity
    trust_bundle_sha256: str = Field(pattern=_SHA)
    region: str = Field(pattern=r"^[a-z0-9][-a-z0-9]{1,31}$")
    native_fixture_receipt_sha256: str = Field(pattern=_SHA)
    file_live_parity_receipt_sha256: str = Field(pattern=_SHA)
    qualification_access: list[QualificationAccess] = Field(min_length=1, max_length=16)
    discovery_access: list[QualificationAccess] = Field(default_factory=list, max_length=16)
    mcp_tool_name: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    mcp_description: str = Field(min_length=1, max_length=240)
    attestation: dict[str, Any]


class DeploymentSet(_Strict):
    schema_name: Literal["fs2-serve.nebius.ai/native-serverless-deployments/v1"] = Field(alias="schema")
    session_id: str = Field(pattern=_SHA)
    gateway_service: ServiceIdentity
    models: dict[str, Deployment] = Field(min_length=1, max_length=32)


@dataclass(frozen=True)
class QualificationPolicy:
    """Private authorization policy, never serialized in customer metadata."""

    identities: frozenset[tuple[str, str]]
    discovery_identities: frozenset[tuple[str, str]] = frozenset()

    def permits(self, tenant_id: str, principal_id: str) -> bool:
        return (tenant_id, principal_id) in self.identities


def _digest(value: Any) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def validate_worker_checkpoint(model: Any, response: Any, *, file_result: bool = False) -> None:
    """A pinned route must reject wrong/self-reported checkpoint before output."""
    if model.binding.backend_class != "federated-serverless":
        return
    qualification = model.gateway.qualification
    native = qualification.get("native_serverless") if isinstance(qualification, Mapping) else None
    if native is None:
        return
    expected = native["checkpoint_sha256"]
    identity = response.get("runtime_identity") if isinstance(response, dict) else None
    if (
        not isinstance(identity, dict)
        or identity.get("checkpoint_sha256") != expected
        or (file_result and response.get("model_revision") != "sha256:" + expected)
    ):
        raise NativeServerlessError("worker checkpoint differs from signed native deployment")


def signed_subject(model_id: str, document: DeploymentSet, entry: Deployment) -> dict[str, Any]:
    deployment = entry.model_dump(exclude={"attestation"})
    # Preserve the canonical bytes of pre-discovery signatures, including
    # explicitly empty access. Nonempty metadata grants are always signed.
    if not entry.discovery_access:
        deployment.pop("discovery_access")
    return {
        "schema": ENTRY_SCHEMA,
        "model_id": model_id,
        "gateway_service": document.gateway_service.model_dump(),
        "deployment": deployment,
    }


def _load(path: Path) -> DeploymentSet:
    raw = path.read_bytes()
    if not raw or len(raw) > 4 * 1024 * 1024:
        raise NativeServerlessError("native Serverless deployment file size is invalid")

    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        value: dict[str, Any] = {}
        for key, item in items:
            if key in value:
                raise NativeServerlessError("duplicate deployment JSON key")
            value[key] = item
        return value

    def reject_constant(_value: str) -> NoReturn:
        raise NativeServerlessError("nonfinite deployment JSON constant")

    return DeploymentSet.model_validate(json.loads(raw, object_pairs_hook=pairs, parse_constant=reject_constant))


def bind_native_serverless(
    gateway: GatewayCatalog,
    catalog: Catalog,
    path: Path | None,
    *,
    catalog_dir: Path,
    trusted_attestors: Mapping[str, str] | None,
    validation_time: datetime | None,
) -> tuple[GatewayCatalog, Mapping[str, QualificationPolicy]]:
    if path is None:
        return gateway, MappingProxyType({})
    if not trusted_attestors:
        raise NativeServerlessError("native Serverless deployment requires trusted signing authority")
    document = _load(path)
    if document.gateway_service.port != 8080:
        raise NativeServerlessError("native gateway Service must identify control-plane port8080")
    models, policies = dict(gateway.models), {}
    used_endpoints: set[str] = set()
    used_services: set[str] = set()
    used_nonces: set[str] = set()
    tool_names = {row.binding.mcp_tool_name for row in models.values() if row.binding and row.binding.enabled}
    for model_id, entry in document.models.items():
        if model_id not in catalog.records or model_id not in models:
            raise NativeServerlessError("native Serverless deployment names unknown model")
        record, base = catalog.model(model_id), models[model_id]
        value = record.to_dict()
        if (
            record.path.resolve().parent != (catalog_dir / "native").resolve()
            or record.digest != entry.model_digest
            or record.route_exposed
            or base.routable
            or (base.binding is not None and base.binding.enabled)
            or base.runtime_image_digest != value["runtime"]["image"]["digest"]
            or value["runtime"]["kind"] in {"nim", "unresolved"}
            or value["support"]["state"] != "qualified"
            or value["cache"]["owner"] != "runtime-image"
            or value["cache"]["artifact"]["manifest_digest"] != entry.artifact_manifest_sha256
            or value["runtime"]["image"]["digest"] != entry.endpoint.runtime_image_digest
            or value["resources"]["gpu"]["count"] != 1
            or not value["interface"]["mcp"]["discoverable"]
        ):
            raise NativeServerlessError("native model/artifact/image identity differs or is already routed")
        variant = catalog.model_variant(entry.variant_id)
        if (
            variant.base_model_id != model_id
            or variant.exposed_model_id != model_id
            or variant.relationship != "exact-model"
            or variant.to_dict()["source"] != value["model"]["source"]
        ):
            raise NativeServerlessError("native Serverless variant aliases another model")
        declaration = _load_json(record.path)
        manifest = _artifact(record.path, catalog_dir, declaration["artifact_manifest"], value)
        if not any(
            item.get("sha256") == entry.endpoint.checkpoint_sha256 for item in manifest.to_dict()["content"]["files"]
        ):
            raise NativeServerlessError("checkpoint is absent from exact native artifact inventory")
        semantic = catalog.semantic_request_contract(model_id)
        if semantic.state != "qualified" or len(set(semantic.request_sha256)) != 2:
            raise NativeServerlessError("native two-fixture contract is unqualified")
        service = entry.discovery_service
        if (
            service.namespace not in {"fs2-models", document.gateway_service.namespace}
            or service.service_name != model_id
            or service.port != 443
            or service.service_uid in used_services
            or entry.endpoint.endpoint_id in used_endpoints
        ):
            raise NativeServerlessError("native discovery Service or Endpoint aliases another deployment")
        used_services.add(service.service_uid)
        used_endpoints.add(entry.endpoint.endpoint_id)
        access = frozenset((row.tenant_id, row.principal_id) for row in entry.qualification_access)
        if len(access) != len(entry.qualification_access):
            raise NativeServerlessError("duplicate qualification identity")
        discovery_access = frozenset((row.tenant_id, row.principal_id) for row in entry.discovery_access)
        if len(discovery_access) != len(entry.discovery_access):
            raise NativeServerlessError("duplicate discovery identity")
        if entry.mcp_tool_name in tool_names or any(
            item in entry.mcp_description.lower() for item in ("http://", "https://", "token", "secret", "credential")
        ):
            raise NativeServerlessError("native MCP identity duplicates or exposes private routing metadata")
        subject = signed_subject(model_id, document, entry)
        digest = _digest(subject)
        attestation = verify_signed_attestation(
            entry.attestation,
            trusted_attestors=trusted_attestors,
            expected_session_id=document.session_id,
            expected_kind="native-serverless-deployment",
            expected_schema=ENTRY_SCHEMA,
            expected_digest=digest,
            expected_model_id=model_id,
            validation_time=validation_time,
        )
        claims = {
            "deployment_sha256": digest,
            "qualification_only": True,
            "model_digest": record.digest,
            "native_request_contract_sha256": semantic.digest,
        }
        if attestation["claims"] != claims or attestation["nonce"] in used_nonces:
            raise NativeServerlessError("signed deployment claims or nonce differs")
        used_nonces.add(attestation["nonce"])
        tool_names.add(entry.mcp_tool_name)
        gateway_subject = {"class": "fs2-serve-gateway", **document.gateway_service.model_dump()}
        binding = ServingBinding(
            model_id=model_id,
            binding_digest=digest,
            model_digest=record.digest,
            enabled=True,
            ready=False,
            valid_until=attestation["expires_at"],
            execution_mode="http",
            backend_namespace=service.namespace,
            backend_service_name=service.service_name,
            backend_port=443,
            service_origin=f"https://{model_id}.{service.namespace}.svc.cluster.local:443",
            activation=_disabled_activation(base.scale_contract.digest),
            backend_class="federated-serverless",
            backend_region=entry.region,
            backend_gpu_class=base.gpu_class,
            backend_runtime_image_digest=entry.endpoint.runtime_image_digest,
            backend_endpoint_identity_sha256=_digest(entry.endpoint.model_dump()),
            backend_trust_bundle_sha256=entry.trust_bundle_sha256,
            backend_credential_requirement_id=entry.credential.requirement_id,
            gateway_class="fs2-serve-gateway",
            gateway_namespace=document.gateway_service.namespace,
            gateway_service_name=document.gateway_service.service_name,
            gateway_service_uid=document.gateway_service.service_uid,
            gateway_port=8080,
            gateway_identity_sha256=_digest(gateway_subject),
            gateway_auth_class="scoped-api-key",
            protocols=base.protocols,
            endpoints=base.endpoints,
            operations=base.policy_operations,
            mcp_tool_name=entry.mcp_tool_name,
            mcp_description=entry.mcp_description,
            mcp_enabled=True,
            artifact_manifest_digest=manifest.digest,
            artifact_uri=None,
            storage_mode=None,
            acquisition_receipt_digest=None,
            prerequisite_receipt_digest=None,
            target_node_canary_digest=None,
            placement_receipt_digest=None,
            runtime_tuple_digest=None,
            prepared_qualification_digest=None,
            new_node_qualification_digest=None,
            semantic_evidence_digest=None,
            readiness_evidence_digest=None,
            backend_evidence_digest=None,
            federated_qualification_digest=None,
            evidence_session_id=document.session_id,
        )
        policies[model_id] = QualificationPolicy(access, discovery_access)
        models[model_id] = replace(
            base,
            binding=binding,
            routable=True,
            mcp_invocable=True,
            support_state="qualification-only",
            qualification=MappingProxyType(
                {
                    # Input-contract selection must retain the signed exact
                    # native variant; this is identity, not readiness evidence.
                    "variant_id": entry.variant_id,
                    "states": {
                        "registered": True,
                        "route_active": True,
                        "runtime_ready": False,
                        "semantic_qualified": False,
                        "http_mcp_qualified": False,
                        "cold_start_qualified": False,
                        "elasticity_qualified": False,
                    },
                    "native_serverless": {
                        "endpoint_origin": entry.endpoint.origin,
                        "checkpoint_sha256": entry.endpoint.checkpoint_sha256,
                        "deployment_sha256": digest,
                        "qualification_only": True,
                        "clinical_qualified": False,
                        "measured_capacity": None,
                    },
                }
            ),
        )
    return replace(gateway, models=MappingProxyType(dict(sorted(models.items())))), MappingProxyType(policies)
