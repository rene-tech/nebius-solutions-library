#!/usr/bin/env python3
"""Typed gateway consumer API over the canonical catalog and route overlay."""

from __future__ import annotations

import copy
import hashlib
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping

from .artifacts import canonical_bytes
from .evidence import validate_federated_route_evidence, validate_route_evidence
from .loader import (
    Catalog,
    CatalogError,
    ModelRecord,
    ScaleContract,
    _boolean,
    _enum,
    _exact,
    _list,
    _load_json,
    _optional_digest,
    _text,
    canonical_content_uri,
    canonical_http_path,
    execution_identity,
    load_catalog,
    resource_placement_identity,
    strong_sha256,
)


SERVING_BINDINGS_SCHEMA = "fs2-serve.nebius.ai/serving-bindings/v16"
GATEWAY_CONSUMER_SCHEMA = "fs2-serve.nebius.ai/gateway-catalog/v6"
GATEWAY_MODEL_SCHEMA = "fs2-serve.nebius.ai/gateway-model/v6"
PROTOCOLS = {
    "openai-chat",
    "openai-completions",
    "openai-embeddings",
    "openai-images",
    "native",
}

ACTIVATION_RECEIPT_DIGEST_FIELDS = (
    "acquisition_receipt_digest",
    "prerequisite_receipt_digest",
    "target_node_canary_digest",
    "placement_receipt_digest",
    "prepared_qualification_digest",
    "new_node_qualification_digest",
    "semantic_evidence_digest",
    "readiness_evidence_digest",
    "backend_evidence_digest",
    "federated_qualification_digest",
)


def activation_intent_binding_digest(value: Mapping[str, Any]) -> str:
    """Hash the immutable activation subject without receipt-digest cycles."""

    subject = copy.deepcopy(dict(value))
    activation = subject.get("activation")
    qualification = subject.get("qualification")
    if not isinstance(activation, dict) or not isinstance(qualification, dict):
        raise CatalogError("activation intent binding subject is incomplete")
    if (
        "valid_until" not in subject
        or "zero_to_ready_receipt_digest" not in activation
        or "return_to_zero_receipt_digest" not in activation
        or any(field not in qualification for field in ACTIVATION_RECEIPT_DIGEST_FIELDS)
    ):
        raise CatalogError("activation intent binding subject lacks canonical fields")
    # Expiry is derived from the reopened receipt set and is revalidated on
    # every claim/dispatch; it is not an independent intent identity input.
    subject["valid_until"] = None
    activation["zero_to_ready_receipt_digest"] = None
    activation["return_to_zero_receipt_digest"] = None
    for field in ACTIVATION_RECEIPT_DIGEST_FIELDS:
        qualification[field] = None
    return hashlib.sha256(canonical_bytes(subject)).hexdigest()


INTERNAL_DESCRIPTION_TOKEN = re.compile(
    r"(?:https?://|\.svc(?:\.cluster\.local)?\b|cluster\.local\b|"
    r"/internal(?:/|\b)|activation[_ -]?url|service[_ -]?origin)",
    re.IGNORECASE,
)
K8S_SERVICE_UID = re.compile(r"^[0-9a-f]{8}-[0-9a-f-]{27,}$")


def _public_description(value: Any) -> str:
    description = _text(value, "mcp.description")
    assert description is not None
    if len(description) > 512:
        raise CatalogError("MCP description exceeds the public projection bound")
    if INTERNAL_DESCRIPTION_TOKEN.search(description):
        raise CatalogError(
            "MCP description contains a private route or activation identity"
        )
    return description


def _backend_identity(
    model_id: str,
    execution_mode: str,
    service: Mapping[str, Any],
    backend_value: Mapping[str, Any],
    catalog: Catalog,
    record: ModelRecord,
) -> dict[str, Any]:
    namespace = _text(service["namespace"], "service.namespace")
    name = _text(service["name"], "service.name")
    port = service["port"]
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        raise CatalogError("service.port is outside the TCP port range")
    if execution_mode != "http":
        raise CatalogError(
            "batch records have no serving Service until a separately qualified "
            "batch controller contract is published"
        )
    expected_namespace, expected_name, expected_port = "fs2-models", model_id, 8000
    if (namespace, name, port) != (expected_namespace, expected_name, expected_port):
        raise CatalogError(
            "serving backend differs from the model-owned canonical Service"
        )
    expected_origin = f"http://{name}.{namespace}.svc.cluster.local:{port}"
    origin = _text(service["origin"], "service.origin")
    if origin != expected_origin:
        raise CatalogError(
            "service.origin differs from its namespace/name/port identity"
        )
    service_identity = {
        "namespace": namespace,
        "service_name": name,
        "port": port,
        "origin": origin,
    }
    backend = _exact(
        backend_value,
        {
            "class",
            "inventory_model_id",
            "region",
            "gpu_class",
            "runtime_image_digest",
            "endpoint_identity_sha256",
            "trust_bundle_sha256",
            "credential_requirement_id",
        },
        "backend",
    )
    backend_class = _enum(
        backend["class"],
        {"local-kubernetes", "federated-kserve-nim", "federated-serverless"},
        "backend.class",
    )
    inventory_model_id = _text(
        backend["inventory_model_id"], "backend.inventory_model_id", nullable=True
    )
    region = _text(backend["region"], "backend.region", nullable=True)
    gpu_class = _text(backend["gpu_class"], "backend.gpu_class")
    runtime_image_digest = _text(
        backend["runtime_image_digest"], "backend.runtime_image_digest"
    )
    endpoint_identity = _optional_digest(
        backend["endpoint_identity_sha256"], "backend.endpoint_identity_sha256"
    )
    trust_bundle = _optional_digest(
        backend["trust_bundle_sha256"], "backend.trust_bundle_sha256"
    )
    credential_id = _text(
        backend["credential_requirement_id"],
        "backend.credential_requirement_id",
        nullable=True,
    )
    record_value = record.to_dict()
    if runtime_image_digest != record_value["runtime"]["image"]["digest"]:
        raise CatalogError(
            "backend runtime image digest differs from the exact model record"
        )
    if backend_class == "local-kubernetes":
        expected_endpoint = hashlib.sha256(
            canonical_bytes(service_identity)
        ).hexdigest()
        if (
            inventory_model_id is not None
            or region != "us-north1"
            or gpu_class != record_value["resources"]["gpu"]["class"]
            or endpoint_identity != expected_endpoint
            or trust_bundle is None
            or credential_id is not None
        ):
            raise CatalogError(
                "local backend identity differs from the canonical Kubernetes subject"
            )
    else:
        inventory = catalog.federated_backend(model_id)
        if inventory is None or inventory_model_id != model_id:
            raise CatalogError(
                "federated binding lacks its exact catalog inventory subject"
            )
        expected = inventory.to_dict()
        if (
            backend_class != expected["backend_class"]
            or region != expected["region"]
            or gpu_class != expected["gpu_class"]
            or runtime_image_digest != expected["runtime_image_digest"]
            or endpoint_identity != expected["endpoint_identity_sha256"]
            or trust_bundle != expected["trust_bundle_sha256"]
            or credential_id != expected["credential_requirement_id"]
        ):
            raise CatalogError(
                "federated binding differs from the exact inventory subject"
            )
    return {
        **service_identity,
        "class": backend_class,
        "inventory_model_id": inventory_model_id,
        "region": region,
        "gpu_class": gpu_class,
        "runtime_image_digest": runtime_image_digest,
        "endpoint_identity_sha256": endpoint_identity,
        "trust_bundle_sha256": trust_bundle,
        "credential_requirement_id": credential_id,
    }


@dataclass(frozen=True)
class ServingBinding:
    """One validated, model-digest-bound gateway serving overlay entry."""

    model_id: str
    binding_digest: str
    model_digest: str
    enabled: bool
    ready: bool
    valid_until: str | None
    execution_mode: str
    backend_namespace: str
    backend_service_name: str
    backend_port: int
    service_origin: str
    activation: "ActivationBinding"
    backend_class: str
    backend_region: str | None
    backend_gpu_class: str
    backend_runtime_image_digest: str
    backend_endpoint_identity_sha256: str | None
    backend_trust_bundle_sha256: str | None
    backend_credential_requirement_id: str | None
    gateway_class: str
    gateway_namespace: str
    gateway_service_name: str
    gateway_service_uid: str
    gateway_port: int
    gateway_identity_sha256: str
    gateway_auth_class: str
    protocols: tuple[str, ...]
    endpoints: Mapping[str, str]
    operations: tuple[str, ...]
    mcp_tool_name: str
    mcp_description: str
    mcp_enabled: bool
    artifact_manifest_digest: str | None
    artifact_uri: str | None
    storage_mode: str | None
    acquisition_receipt_digest: str | None
    prerequisite_receipt_digest: str | None
    target_node_canary_digest: str | None
    placement_receipt_digest: str | None
    runtime_tuple_digest: str | None
    prepared_qualification_digest: str | None
    new_node_qualification_digest: str | None
    semantic_evidence_digest: str | None
    readiness_evidence_digest: str | None
    backend_evidence_digest: str | None
    federated_qualification_digest: str | None
    evidence_session_id: str | None

    def valid_at(self, when: datetime | None = None) -> bool:
        """Let long-lived consumers fail closed once signed route evidence expires."""

        if not self.enabled or self.valid_until is None:
            return False
        now = when or datetime.now(timezone.utc)
        if now.tzinfo is None:
            raise CatalogError("serving-binding validity check must be timezone-aware")
        expires = datetime.fromisoformat(self.valid_until[:-1] + "+00:00")
        return now.astimezone(timezone.utc).replace(microsecond=0) < expires


@dataclass(frozen=True)
class ActivationBinding:
    """Validated durable-intent mutation binding; never a public route field."""

    enabled: bool
    scale_contract_digest: str
    controller_namespace: str
    controller_deployment_name: str
    controller_deployment_uid: str | None
    controller_pod_name: str | None
    controller_pod_uid: str | None
    controller_pod_owner_deployment_uid: str | None
    controller_service_account_name: str
    controller_service_account_uid: str | None
    controller_leader_lease_name: str
    controller_leader_lease_uid: str | None
    controller_leader_lease_resource_version: str | None
    controller_leader_lease_holder_identity: str | None
    controller_leader_lease_renew_time: str | None
    controller_leader_lease_duration_seconds: int | None
    controller_leader_role_namespace: str
    controller_leader_role_name: str
    controller_target_role_namespace: str
    controller_target_role_name: str
    submitter_service_account_name: str
    submitter_service_account_uid: str | None
    submitter_deployment_name: str
    submitter_deployment_uid: str | None
    submitter_pod_name: str | None
    submitter_pod_uid: str | None
    submitter_pod_owner_deployment_uid: str | None
    submitter_database_role: str
    claim_owner_database_role: str
    database_grants_sha256: str
    activation_store_sha256: str
    activation_store_ddl_sha256: str
    submitter_database_secret: Mapping[str, Any] | None
    claim_owner_database_secret: Mapping[str, Any] | None
    controller_identity_sha256: str | None
    controller_auth_class: str
    intent_interface_sha256: str
    target_api_version: str | None
    target_kind: str | None
    target_namespace: str | None
    target_name: str | None
    target_uid: str | None
    target_template_identity_sha256: str | None
    zero_to_ready_receipt_digest: str | None
    return_to_zero_receipt_digest: str | None


@dataclass(frozen=True)
class ServingBindings:
    """Validated route overlay bound to exactly one canonical catalog digest."""

    catalog_digest: str
    bindings: Mapping[str, ServingBinding]

    def get(self, model_id: str) -> ServingBinding | None:
        return self.bindings.get(model_id)


@dataclass(frozen=True)
class GatewayModel:
    """Stable typed view consumed by the gateway; no duplicated base schema."""

    model_id: str
    display_name: str
    family: str
    model_revision: str | None
    runtime_kind: str
    runtime_image_digest: str | None
    gpu_class: str
    gpu_allocation_count: int
    resource_placement_identity_sha256: str
    semantic_contract: str
    execution_mode: str
    protocols: tuple[str, ...]
    endpoints: Mapping[str, str]
    license_id: str
    license_state: str
    entitlement_state: str
    non_clinical: bool
    commercial_use: str
    policy_operations: tuple[str, ...]
    mcp_discoverable: bool
    mcp_invocable: bool
    support_state: str
    routable: bool
    binding: ServingBinding | None
    scale_contract: ScaleContract
    qualification: Mapping[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        value: dict[str, Any] = {
            "schema": GATEWAY_MODEL_SCHEMA,
            "model_id": self.model_id,
            "display_name": self.display_name,
            "family": self.family,
            "model_revision": self.model_revision,
            "runtime_kind": self.runtime_kind,
            "runtime_image_digest": self.runtime_image_digest,
            "gpu_class": self.gpu_class,
            "gpu_allocation_count": self.gpu_allocation_count,
            "resource_placement_identity_sha256": self.resource_placement_identity_sha256,
            "semantic_contract": self.semantic_contract,
            "execution_mode": self.execution_mode,
            "protocols": list(self.protocols),
            "policy": {
                "license_id": self.license_id,
                "license_state": self.license_state,
                "entitlement_state": self.entitlement_state,
                "non_clinical": self.non_clinical,
                "commercial_use": self.commercial_use,
                "operations": list(self.policy_operations),
            },
            "mcp": {
                "discoverable": self.mcp_discoverable,
                "invocable": self.mcp_invocable,
            },
            "support_state": self.support_state,
            "routable": self.routable,
            "qualification": (
                None if self.qualification is None else copy.deepcopy(dict(self.qualification))
            ),
            "serving": None,
        }
        if self.binding is not None:
            value["serving"] = {
                "ready": self.binding.ready,
                "valid_until": self.binding.valid_until,
                "execution_mode": self.binding.execution_mode,
                "storage_mode": self.binding.storage_mode,
                "protocols": list(self.binding.protocols),
                "endpoints": dict(self.binding.endpoints),
                "operations": list(self.binding.operations),
                "backend": {
                    "class": self.binding.backend_class,
                    "region": self.binding.backend_region,
                    "gpu_class": self.binding.backend_gpu_class,
                    "runtime_image_digest": self.binding.backend_runtime_image_digest,
                },
                "mcp": {
                    "enabled": self.binding.mcp_enabled,
                    "tool_name": self.binding.mcp_tool_name,
                    "description": self.binding.mcp_description,
                },
            }
        return copy.deepcopy(value)


@dataclass(frozen=True)
class GatewayCatalog:
    """The only routing-capable catalog view."""

    catalog_version: str
    catalog_digest: str
    models: Mapping[str, GatewayModel]

    def model(self, model_id: str) -> GatewayModel:
        try:
            return self.models[model_id]
        except KeyError as exc:
            raise CatalogError(f"unknown model ID: {model_id}") from exc

    def routable_model_ids(self) -> tuple[str, ...]:
        return tuple(
            sorted(
                model_id for model_id, model in self.models.items() if model.routable
            )
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": GATEWAY_CONSUMER_SCHEMA,
            "catalog_version": self.catalog_version,
            "catalog_digest": self.catalog_digest,
            "models": [self.models[key].to_dict() for key in sorted(self.models)],
        }


def _validate_activation_binding(
    model_id: str,
    value: Any,
    scale_contract: ScaleContract,
    *,
    route_enabled: bool,
    backend_class: str,
    validation_time: datetime | None,
) -> tuple[ActivationBinding, dict[str, Any]]:
    item = _exact(
        value,
        {
            "enabled",
            "scale_contract_digest",
            "controller",
            "target",
            "zero_to_ready_receipt_digest",
            "return_to_zero_receipt_digest",
        },
        f"bindings.{model_id}.activation",
    )
    enabled = _boolean(item["enabled"], "activation.enabled")
    scale_digest = strong_sha256(
        item["scale_contract_digest"], "activation scale contract digest"
    )
    if scale_digest != scale_contract.digest:
        raise CatalogError("activation binding names another model scale contract")
    controller = _exact(
        item["controller"],
        {
            "class",
            "namespace",
            "deployment_name",
            "deployment_uid",
            "pod_name",
            "pod_uid",
            "pod_owner_deployment_uid",
            "service_account_name",
            "service_account_uid",
            "leader_lease_name",
            "leader_lease_uid",
            "leader_lease_resource_version",
            "leader_lease_holder_identity",
            "leader_lease_renew_time",
            "leader_lease_duration_seconds",
            "leader_role_namespace",
            "leader_role_name",
            "target_role_namespace",
            "target_role_name",
            "submitter_service_account_name",
            "submitter_service_account_uid",
            "submitter_deployment_name",
            "submitter_deployment_uid",
            "submitter_pod_name",
            "submitter_pod_uid",
            "submitter_pod_owner_deployment_uid",
            "submitter_database_role",
            "claim_owner_database_role",
            "submitter_database_secret",
            "claim_owner_database_secret",
            "database_grants_sha256",
            "activation_store_sha256",
            "activation_store_ddl_sha256",
            "identity_sha256",
            "auth_class",
            "intent_interface_sha256",
        },
        "activation controller",
    )
    expected_controller = {
        "class": "fs2-model-activation-controller",
        "namespace": "fs2-system",
        "deployment_name": "fs2-serve-control-plane-activation",
        "service_account_name": "fs2-model-activation-controller",
        "leader_lease_name": "fs2-serve-activation-controller",
        "leader_role_namespace": "fs2-system",
        "leader_role_name": "fs2-serve-control-plane-activation-leader",
        "target_role_namespace": "fs2-models",
        "target_role_name": "fs2-serve-control-plane-activation-targets",
        "submitter_service_account_name": "fs2-serve-control-plane",
        "submitter_deployment_name": "fs2-serve-control-plane",
        "submitter_database_role": "fs2_activation_submitter",
        "claim_owner_database_role": "fs2_activation_claim_owner",
        "auth_class": ("postgres-role-grants-plus-projected-ksa-lease-operation-fence"),
    }
    if any(
        controller[key] != expected for key, expected in expected_controller.items()
    ):
        raise CatalogError("activation binding names a foreign controller")
    deployment_uid = _text(
        controller["deployment_uid"],
        "activation controller Deployment UID",
        nullable=True,
    )
    controller_pod_name = _text(
        controller["pod_name"], "activation controller Pod name", nullable=True
    )
    controller_pod_uid = _text(
        controller["pod_uid"], "activation controller Pod UID", nullable=True
    )
    controller_pod_owner_deployment_uid = _text(
        controller["pod_owner_deployment_uid"],
        "activation controller Pod owner Deployment UID",
        nullable=True,
    )
    service_account_uid = _text(
        controller["service_account_uid"],
        "activation controller ServiceAccount UID",
        nullable=True,
    )
    leader_lease_uid = _text(
        controller["leader_lease_uid"],
        "activation controller leader Lease UID",
        nullable=True,
    )
    leader_lease_resource_version = _text(
        controller["leader_lease_resource_version"],
        "activation controller leader Lease resourceVersion",
        nullable=True,
    )
    leader_lease_holder_identity = _text(
        controller["leader_lease_holder_identity"],
        "activation controller leader Lease holderIdentity",
        nullable=True,
    )
    leader_lease_renew_time = _text(
        controller["leader_lease_renew_time"],
        "activation controller leader Lease renewTime",
        nullable=True,
    )
    leader_lease_duration_seconds = controller["leader_lease_duration_seconds"]
    submitter_service_account_uid = _text(
        controller["submitter_service_account_uid"],
        "activation submitter ServiceAccount UID",
        nullable=True,
    )
    submitter_deployment_uid = _text(
        controller["submitter_deployment_uid"],
        "activation submitter Deployment UID",
        nullable=True,
    )
    submitter_pod_name = _text(
        controller["submitter_pod_name"], "activation submitter Pod name", nullable=True
    )
    submitter_pod_uid = _text(
        controller["submitter_pod_uid"], "activation submitter Pod UID", nullable=True
    )
    submitter_pod_owner_deployment_uid = _text(
        controller["submitter_pod_owner_deployment_uid"],
        "activation submitter Pod owner Deployment UID",
        nullable=True,
    )
    identity = _optional_digest(
        controller["identity_sha256"], "activation controller identity"
    )
    intent_interface = scale_contract.to_dict()["controller_boundary"][
        "activation_intent_interface"
    ]
    expected_intent_interface_digest = hashlib.sha256(
        canonical_bytes(intent_interface)
    ).hexdigest()
    intent_interface_digest = strong_sha256(
        controller["intent_interface_sha256"], "activation intent interface"
    )
    if intent_interface_digest != expected_intent_interface_digest:
        raise CatalogError(
            "activation binding names another PostgreSQL intent interface"
        )
    expected_grants_digest = hashlib.sha256(
        canonical_bytes(intent_interface["database_principals"])
    ).hexdigest()
    database_grants_digest = strong_sha256(
        controller["database_grants_sha256"], "activation database grants"
    )
    if database_grants_digest != expected_grants_digest:
        raise CatalogError("activation binding database roles/grants differ")
    activation_store = scale_contract.to_dict()["controller_boundary"][
        "activation_store"
    ]
    expected_store_digest = hashlib.sha256(
        canonical_bytes(activation_store)
    ).hexdigest()
    activation_store_digest = strong_sha256(
        controller["activation_store_sha256"], "activation store contract"
    )
    if activation_store_digest != expected_store_digest:
        raise CatalogError(
            "activation binding names another executable PostgreSQL store"
        )
    activation_store_ddl_digest = strong_sha256(
        controller["activation_store_ddl_sha256"], "activation store DDL"
    )
    if activation_store_ddl_digest != activation_store["ddl"]["sha256"]:
        raise CatalogError("activation binding names another activation store DDL")
    database_secrets: dict[str, Mapping[str, Any] | None] = {}
    for key, expected_name in (
        ("submitter_database_secret", "fs2-activation-submitter-db"),
        ("claim_owner_database_secret", "fs2-activation-claim-owner-db"),
    ):
        raw_secret = controller[key]
        if raw_secret is None:
            database_secrets[key] = None
            continue
        secret = _exact(
            raw_secret,
            {"namespace", "name", "uid", "resource_version", "type", "key_set"},
            f"activation {key}",
        )
        if (
            secret["namespace"] != "fs2-system"
            or secret["name"] != expected_name
            or secret["type"] != "Opaque"
            or secret["key_set"] != ["dsn"]
        ):
            raise CatalogError("activation database Secret contract differs")
        database_secrets[key] = copy.deepcopy(secret)
    target_value = item["target"]
    contract_target = scale_contract.to_dict()["target"]
    target_subject: dict[str, Any] | None = None
    target_api_version: str | None = None
    target_kind: str | None = None
    target_namespace: str | None = None
    target_name: str | None = None
    target_uid: str | None = None
    target_template: str | None = None
    if contract_target is None:
        if target_value is not None:
            raise CatalogError("disabled model activation cannot invent a target")
    else:
        target = _exact(
            target_value,
            {
                "api_version",
                "kind",
                "namespace",
                "name",
                "uid",
                "resource_version",
                "observed_generation",
                "template_identity_sha256",
            },
            "activation target",
        )
        for key in (
            "api_version",
            "kind",
            "namespace",
            "name",
            "template_identity_sha256",
        ):
            if target[key] != contract_target[key]:
                raise CatalogError(
                    "activation target differs from the immutable scale contract"
                )
        target_api_version = target["api_version"]
        target_kind = target["kind"]
        target_namespace = target["namespace"]
        target_name = target["name"]
        target_uid = _text(target["uid"], "activation target UID", nullable=True)
        resource_version = _text(
            target["resource_version"],
            "activation target resource version",
            nullable=True,
        )
        observed_generation = target["observed_generation"]
        if observed_generation is not None and (
            isinstance(observed_generation, bool)
            or not isinstance(observed_generation, int)
            or observed_generation <= 0
        ):
            raise CatalogError("activation target observed generation is invalid")
        target_template = strong_sha256(
            target["template_identity_sha256"], "activation target template identity"
        )
        if target_uid is not None:
            target_subject = {
                "api_version": target_api_version,
                "kind": target_kind,
                "namespace": target_namespace,
                "name": target_name,
                "uid": target_uid,
                "resource_version": resource_version,
                "observed_generation": observed_generation,
                "template_identity_sha256": target_template,
            }
    zero_receipt = _optional_digest(
        item["zero_to_ready_receipt_digest"], "activation zero-to-ready receipt"
    )
    return_receipt = _optional_digest(
        item["return_to_zero_receipt_digest"], "activation return-to-zero receipt"
    )
    controller_subject: dict[str, Any] | None = None
    leader_lease_valid_until: str | None = None
    if enabled:
        if (
            not route_enabled
            or backend_class != "local-kubernetes"
            or scale_contract.activation_mode != "replica-scale"
        ):
            raise CatalogError(
                "activation controller is valid only for an enabled local replica route"
            )
        if (
            deployment_uid is None
            or controller_pod_name is None
            or controller_pod_uid is None
            or controller_pod_owner_deployment_uid is None
            or service_account_uid is None
            or leader_lease_uid is None
            or leader_lease_resource_version is None
            or leader_lease_holder_identity is None
            or leader_lease_renew_time is None
            or leader_lease_duration_seconds != 30
            or submitter_service_account_uid is None
            or submitter_deployment_uid is None
            or submitter_pod_name is None
            or submitter_pod_uid is None
            or submitter_pod_owner_deployment_uid is None
            or K8S_SERVICE_UID.fullmatch(deployment_uid) is None
            or K8S_SERVICE_UID.fullmatch(controller_pod_uid) is None
            or controller_pod_owner_deployment_uid != deployment_uid
            or K8S_SERVICE_UID.fullmatch(service_account_uid) is None
            or K8S_SERVICE_UID.fullmatch(leader_lease_uid) is None
            or K8S_SERVICE_UID.fullmatch(submitter_service_account_uid) is None
            or K8S_SERVICE_UID.fullmatch(submitter_deployment_uid) is None
            or K8S_SERVICE_UID.fullmatch(submitter_pod_uid) is None
            or submitter_pod_owner_deployment_uid != submitter_deployment_uid
            or database_secrets["submitter_database_secret"] is None
            or database_secrets["claim_owner_database_secret"] is None
            or identity is None
            or target_subject is None
            or target_uid is None
            or K8S_SERVICE_UID.fullmatch(target_uid) is None
            or target_subject["resource_version"] is None
            or target_subject["observed_generation"] is None
            or zero_receipt is None
            or return_receipt is None
        ):
            raise CatalogError(
                "enabled activation lacks exact controller, target, or lifecycle receipts"
            )
        for secret in database_secrets.values():
            assert secret is not None
            if (
                K8S_SERVICE_UID.fullmatch(str(secret["uid"])) is None
                or not isinstance(secret["resource_version"], str)
                or not secret["resource_version"]
            ):
                raise CatalogError(
                    "enabled activation lacks server-observed database Secrets"
                )
        try:
            renew_time = datetime.fromisoformat(leader_lease_renew_time[:-1] + "+00:00")
        except (ValueError, TypeError) as exc:
            raise CatalogError(
                "activation leader Lease renewTime is not RFC3339 UTC"
            ) from exc
        if (
            not leader_lease_renew_time.endswith("Z")
            or renew_time.tzinfo != timezone.utc
            or renew_time.microsecond
        ):
            raise CatalogError("activation leader Lease renewTime is not exact UTC")
        lease_expires = renew_time + timedelta(seconds=leader_lease_duration_seconds)
        now = validation_time or datetime.now(timezone.utc)
        if now.tzinfo is None:
            raise CatalogError("activation validation time must be timezone-aware")
        if lease_expires <= now.astimezone(timezone.utc):
            raise CatalogError("activation controller leader Lease is expired")
        leader_lease_valid_until = lease_expires.strftime("%Y-%m-%dT%H:%M:%SZ")
        controller_identity_subject = {
            **expected_controller,
            "deployment_uid": deployment_uid,
            "pod_name": controller_pod_name,
            "pod_uid": controller_pod_uid,
            "pod_owner_deployment_uid": controller_pod_owner_deployment_uid,
            "service_account_uid": service_account_uid,
            "leader_lease_uid": leader_lease_uid,
            "leader_lease_resource_version": leader_lease_resource_version,
            "leader_lease_holder_identity": leader_lease_holder_identity,
            "leader_lease_renew_time": leader_lease_renew_time,
            "leader_lease_duration_seconds": leader_lease_duration_seconds,
            "submitter_service_account_uid": submitter_service_account_uid,
            "submitter_deployment_uid": submitter_deployment_uid,
            "submitter_pod_name": submitter_pod_name,
            "submitter_pod_uid": submitter_pod_uid,
            "submitter_pod_owner_deployment_uid": (submitter_pod_owner_deployment_uid),
            "submitter_database_secret": database_secrets["submitter_database_secret"],
            "claim_owner_database_secret": database_secrets[
                "claim_owner_database_secret"
            ],
            "database_grants_sha256": database_grants_digest,
            "intent_interface_sha256": intent_interface_digest,
            "activation_store_sha256": activation_store_digest,
            "activation_store_ddl_sha256": activation_store_ddl_digest,
        }
        if (
            identity
            != hashlib.sha256(canonical_bytes(controller_identity_subject)).hexdigest()
        ):
            raise CatalogError(
                "activation controller identity differs from its exact subject"
            )
        controller_subject = {
            **controller_identity_subject,
            "identity_sha256": identity,
        }
    else:
        if (
            route_enabled
            and backend_class == "local-kubernetes"
            and scale_contract.activation_mode == "replica-scale"
        ):
            raise CatalogError("enabled local route lacks an activation controller")
        if any(
            value is not None
            for value in (
                deployment_uid,
                controller_pod_name,
                controller_pod_uid,
                controller_pod_owner_deployment_uid,
                service_account_uid,
                leader_lease_uid,
                leader_lease_resource_version,
                leader_lease_holder_identity,
                leader_lease_renew_time,
                leader_lease_duration_seconds,
                submitter_service_account_uid,
                submitter_deployment_uid,
                submitter_pod_name,
                submitter_pod_uid,
                submitter_pod_owner_deployment_uid,
                database_secrets["submitter_database_secret"],
                database_secrets["claim_owner_database_secret"],
                identity,
                target_uid,
                zero_receipt,
                return_receipt,
            )
        ):
            raise CatalogError(
                "disabled activation cannot imply a live controller or target"
            )
        if target_value is not None and any(
            target_value[key] is not None
            for key in ("resource_version", "observed_generation")
        ):
            raise CatalogError(
                "disabled activation cannot imply a live target generation"
            )
    binding = ActivationBinding(
        enabled=enabled,
        scale_contract_digest=scale_digest,
        controller_namespace=controller["namespace"],
        controller_deployment_name=controller["deployment_name"],
        controller_deployment_uid=deployment_uid,
        controller_pod_name=controller_pod_name,
        controller_pod_uid=controller_pod_uid,
        controller_pod_owner_deployment_uid=controller_pod_owner_deployment_uid,
        controller_service_account_name=controller["service_account_name"],
        controller_service_account_uid=service_account_uid,
        controller_leader_lease_name=controller["leader_lease_name"],
        controller_leader_lease_uid=leader_lease_uid,
        controller_leader_lease_resource_version=leader_lease_resource_version,
        controller_leader_lease_holder_identity=leader_lease_holder_identity,
        controller_leader_lease_renew_time=leader_lease_renew_time,
        controller_leader_lease_duration_seconds=leader_lease_duration_seconds,
        controller_leader_role_namespace=controller["leader_role_namespace"],
        controller_leader_role_name=controller["leader_role_name"],
        controller_target_role_namespace=controller["target_role_namespace"],
        controller_target_role_name=controller["target_role_name"],
        submitter_service_account_name=controller["submitter_service_account_name"],
        submitter_service_account_uid=submitter_service_account_uid,
        submitter_deployment_name=controller["submitter_deployment_name"],
        submitter_deployment_uid=submitter_deployment_uid,
        submitter_pod_name=submitter_pod_name,
        submitter_pod_uid=submitter_pod_uid,
        submitter_pod_owner_deployment_uid=submitter_pod_owner_deployment_uid,
        submitter_database_role=controller["submitter_database_role"],
        claim_owner_database_role=controller["claim_owner_database_role"],
        database_grants_sha256=database_grants_digest,
        activation_store_sha256=activation_store_digest,
        activation_store_ddl_sha256=activation_store_ddl_digest,
        submitter_database_secret=database_secrets["submitter_database_secret"],
        claim_owner_database_secret=database_secrets["claim_owner_database_secret"],
        controller_identity_sha256=identity,
        controller_auth_class=controller["auth_class"],
        intent_interface_sha256=intent_interface_digest,
        target_api_version=target_api_version,
        target_kind=target_kind,
        target_namespace=target_namespace,
        target_name=target_name,
        target_uid=target_uid,
        target_template_identity_sha256=target_template,
        zero_to_ready_receipt_digest=zero_receipt,
        return_to_zero_receipt_digest=return_receipt,
    )
    return binding, {
        "controller_receipt_subject": controller_subject,
        "target_receipt_subject": target_subject,
        "zero_to_ready_receipt_digest": zero_receipt,
        "return_to_zero_receipt_digest": return_receipt,
        "leader_lease_valid_until": leader_lease_valid_until,
    }


def _validate_binding(
    model_id: str,
    value: Any,
    catalog: Catalog,
    record: ModelRecord,
    *,
    evidence_root: Path | str | None,
    trusted_attestors: Mapping[str, str] | None,
    validation_time: datetime | None,
) -> ServingBinding:
    item = _exact(
        value,
        {
            "model_digest",
            "enabled",
            "ready",
            "valid_until",
            "service",
            "backend",
            "gateway",
            "activation",
            "policy",
            "mcp",
            "qualification",
        },
        f"bindings.{model_id}",
    )
    # Receipt digests are reopened independently and some lifecycle receipts
    # reference this value, so the durable intent binds their immutable
    # subjects rather than creating a self-referential hash cycle.
    binding_digest = activation_intent_binding_digest(item)
    model_digest = _optional_digest(
        item["model_digest"], f"bindings.{model_id}.model_digest"
    )
    assert model_digest is not None
    if model_digest != record.digest:
        raise CatalogError(f"serving binding model digest mismatch: {model_id}")
    enabled = _boolean(item["enabled"], f"bindings.{model_id}.enabled")
    ready = _boolean(item["ready"], f"bindings.{model_id}.ready")
    valid_until = _text(
        item["valid_until"], f"bindings.{model_id}.valid_until", nullable=True
    )
    if valid_until is not None:
        try:
            parsed_valid_until = datetime.fromisoformat(valid_until[:-1] + "+00:00")
        except (ValueError, TypeError) as exc:
            raise CatalogError(
                "serving binding valid_until is not RFC3339 UTC"
            ) from exc
        if (
            not valid_until.endswith("Z")
            or parsed_valid_until.tzinfo != timezone.utc
            or parsed_valid_until.microsecond
        ):
            raise CatalogError("serving binding valid_until must use whole UTC seconds")

    service = _exact(
        item["service"],
        {
            "execution_mode",
            "namespace",
            "name",
            "port",
            "origin",
            "protocols",
            "endpoints",
        },
        f"bindings.{model_id}.service",
    )
    record_value = record.to_dict()
    execution_mode = _enum(
        service["execution_mode"], {"http", "batch"}, "service.execution_mode"
    )
    if execution_mode != record_value["interface"]["execution_mode"]:
        raise CatalogError(
            "serving binding execution mode differs from the base catalog"
        )
    backend = _backend_identity(
        model_id, execution_mode, service, item["backend"], catalog, record
    )
    scale_contract = catalog.scale_contract(model_id)
    activation, activation_evidence = _validate_activation_binding(
        model_id,
        item["activation"],
        scale_contract,
        route_enabled=enabled,
        backend_class=backend["class"],
        validation_time=validation_time,
    )
    activation_evidence["binding_digest"] = binding_digest
    gateway = _exact(
        item["gateway"],
        {
            "class",
            "namespace",
            "service_name",
            "service_uid",
            "port",
            "identity_sha256",
            "auth_class",
            "route_id",
        },
        f"bindings.{model_id}.gateway",
    )
    gateway_class = _enum(gateway["class"], {"fs2-serve-gateway"}, "gateway.class")
    gateway_namespace = _text(gateway["namespace"], "gateway namespace")
    gateway_service_name = _text(gateway["service_name"], "gateway service name")
    gateway_service_uid = _text(gateway["service_uid"], "gateway service UID")
    gateway_port = gateway["port"]
    if (
        gateway_namespace != "fs2-system"
        or gateway_service_name != "fs2-serve-control-plane"
        or gateway_service_uid is None
        or K8S_SERVICE_UID.fullmatch(gateway_service_uid) is None
        or isinstance(gateway_port, bool)
        or gateway_port != 8080
    ):
        raise CatalogError(
            "gateway does not identify the canonical live Kubernetes Service"
        )
    gateway_service_subject = {
        "class": gateway_class,
        "namespace": gateway_namespace,
        "service_name": gateway_service_name,
        "service_uid": gateway_service_uid,
        "port": gateway_port,
    }
    gateway_identity_sha256 = strong_sha256(
        gateway["identity_sha256"], "gateway identity"
    )
    if (
        gateway_identity_sha256
        != hashlib.sha256(canonical_bytes(gateway_service_subject)).hexdigest()
    ):
        raise CatalogError(
            "gateway identity digest differs from its exact Service subject"
        )
    gateway_auth_class = _enum(
        gateway["auth_class"], {"scoped-api-key"}, "gateway auth class"
    )
    if gateway["route_id"] != model_id:
        raise CatalogError("gateway route identity differs from the model binding")
    origin = backend["origin"]
    protocols = tuple(_list(service["protocols"], "service.protocols", nonempty=True))
    if tuple(sorted(protocols)) != protocols or len(protocols) != len(set(protocols)):
        raise CatalogError("service protocols must be sorted and unique")
    if any(protocol not in PROTOCOLS for protocol in protocols):
        raise CatalogError("serving binding uses an unknown protocol")
    if list(protocols) != record_value["interface"]["protocols"]:
        raise CatalogError("serving binding protocols differ from the base catalog")
    endpoints = service["endpoints"]
    if not isinstance(endpoints, dict) or set(endpoints) != set(protocols):
        raise CatalogError("serving endpoints must exactly cover protocols")
    for protocol, path in endpoints.items():
        canonical_http_path(path, f"service.endpoints.{protocol}")
    if endpoints != record_value["interface"]["endpoints"]:
        raise CatalogError("serving binding endpoints differ from the base catalog")

    policy = _exact(item["policy"], {"operations"}, f"bindings.{model_id}.policy")
    operations = tuple(_list(policy["operations"], "policy.operations", nonempty=True))
    if tuple(sorted(operations)) != operations or len(operations) != len(
        set(operations)
    ):
        raise CatalogError("policy operations must be sorted and unique")
    for operation in operations:
        if not isinstance(operation, str) or not operation or len(operation) > 64:
            raise CatalogError("policy operation is invalid")
    if list(operations) != record_value["interface"]["policy"]["operations"]:
        raise CatalogError("serving binding operations differ from the base catalog")

    mcp = _exact(
        item["mcp"], {"enabled", "tool_name", "description"}, f"bindings.{model_id}.mcp"
    )
    mcp_enabled = _boolean(mcp["enabled"], "mcp.enabled")
    tool_name = _text(mcp["tool_name"], "mcp.tool_name")
    description = _public_description(mcp["description"])
    assert tool_name is not None and description is not None
    if not tool_name.replace("_", "").isalnum() or not tool_name[0].islower():
        raise CatalogError("MCP tool name is not canonical")

    qualification = _exact(
        item["qualification"],
        {
            "artifact_manifest_digest",
            "artifact_uri",
            "storage_mode",
            "acquisition_receipt_digest",
            "prerequisite_receipt_digest",
            "target_node_canary_digest",
            "placement_receipt_digest",
            "runtime_tuple_digest",
            "prepared_qualification_digest",
            "new_node_qualification_digest",
            "semantic_evidence_digest",
            "readiness_evidence_digest",
            "backend_evidence_digest",
            "federated_qualification_digest",
            "evidence_session_id",
        },
        f"bindings.{model_id}.qualification",
    )
    artifact = _optional_digest(
        qualification["artifact_manifest_digest"],
        "qualification.artifact_manifest_digest",
    )
    artifact_uri = _text(
        qualification["artifact_uri"], "qualification.artifact_uri", nullable=True
    )
    storage_mode = qualification["storage_mode"]
    if storage_mode is not None:
        storage_mode = _enum(
            storage_mode,
            {"provider-block-pvc", "sfs-pvc", "local-nvme", "nimcache-pvc"},
            "qualification.storage_mode",
        )
    acquisition_receipt = _optional_digest(
        qualification["acquisition_receipt_digest"],
        "qualification.acquisition_receipt_digest",
    )
    prerequisite_receipt = _optional_digest(
        qualification["prerequisite_receipt_digest"],
        "qualification.prerequisite_receipt_digest",
    )
    target_node_canary = _optional_digest(
        qualification["target_node_canary_digest"],
        "qualification.target_node_canary_digest",
    )
    placement_receipt = _optional_digest(
        qualification["placement_receipt_digest"],
        "qualification.placement_receipt_digest",
    )
    runtime_tuple = _optional_digest(
        qualification["runtime_tuple_digest"], "qualification.runtime_tuple_digest"
    )
    prepared_qualification = _optional_digest(
        qualification["prepared_qualification_digest"],
        "qualification.prepared_qualification_digest",
    )
    new_node_qualification = _optional_digest(
        qualification["new_node_qualification_digest"],
        "qualification.new_node_qualification_digest",
    )
    semantic = _optional_digest(
        qualification["semantic_evidence_digest"],
        "qualification.semantic_evidence_digest",
    )
    readiness = _optional_digest(
        qualification["readiness_evidence_digest"],
        "qualification.readiness_evidence_digest",
    )
    backend_evidence = _optional_digest(
        qualification["backend_evidence_digest"],
        "qualification.backend_evidence_digest",
    )
    federated_qualification = _optional_digest(
        qualification["federated_qualification_digest"],
        "qualification.federated_qualification_digest",
    )
    evidence_session_id = _optional_digest(
        qualification["evidence_session_id"], "qualification.evidence_session_id"
    )
    base_mcp_invocable = bool(record_value["interface"]["mcp"]["invocable"])
    verified_valid_until: str | None = None
    if mcp_enabled and (
        not enabled
        or not ready
        or not record.route_exposed
        or record_value["support"]["state"] != "qualified"
        or not base_mcp_invocable
    ):
        raise CatalogError(
            "MCP invocation requires an enabled routable base-invocable binding"
        )
    if enabled:
        if not ready:
            raise CatalogError("enabled serving binding must be ready")
        if not record.route_exposed or record_value["support"]["state"] != "qualified":
            raise CatalogError(
                "enabled serving binding requires qualified route-exposed support"
            )
        semantic_requests = catalog.semantic_request_contract(model_id)
        if semantic_requests.state != "qualified":
            raise CatalogError(
                "enabled serving binding lacks a qualified canonical request contract"
            )
        if backend["class"] != "local-kubernetes":
            inventory = catalog.federated_backend(model_id)
            if inventory is None or inventory.route_state != "qualified":
                raise CatalogError(
                    "federated serving requires a qualified exact inventory subject"
                )
        if artifact_uri is None or artifact is None:
            raise CatalogError(
                "enabled serving binding requires a content-addressed artifact URI"
            )
        if backend_evidence is None:
            raise CatalogError(
                "enabled serving binding requires signed backend identity evidence"
            )
        if evidence_session_id is None:
            raise CatalogError(
                "enabled serving binding requires a fresh evidence session"
            )
        if evidence_root is None:
            raise CatalogError(
                "enabled serving binding requires an immutable evidence root"
            )
        if not trusted_attestors:
            raise CatalogError(
                "enabled serving binding requires trusted attestor public keys"
            )
        if backend["class"] == "local-kubernetes":
            if federated_qualification is not None:
                raise CatalogError("local serving cannot claim federated qualification")
            if storage_mode is None:
                raise CatalogError(
                    "enabled local binding requires an explicit storage mode"
                )
            placement = record_value["resources"]["gpu"]["placement"]
            if placement is None:
                raise CatalogError(
                    "enabled local binding requires a reviewed node placement"
                )
            capability = {
                "provider-block-pvc": "provider-block-pvc-qualified",
                "sfs-pvc": "sfs-conventional-qualified",
                "local-nvme": "node-local-pv-pvc-qualified",
                "nimcache-pvc": "sfs-conventional-qualified",
            }[storage_mode]
            if capability not in placement["cache_capabilities"]:
                raise CatalogError(
                    "binding storage mode is not admitted by the model placement"
                )
            if storage_mode == "local-nvme":
                raise CatalogError(
                    "serving-bindings v16 keeps local-PV/PVC routing gated-unimplemented"
                )
            if (
                storage_mode == "nimcache-pvc"
                and record_value["runtime"]["kind"] != "nim"
            ):
                raise CatalogError("non-NIM binding cannot claim NIMCache placement")
            if (
                storage_mode != "nimcache-pvc"
                and record_value["runtime"]["kind"] == "nim"
            ):
                raise CatalogError("NIM binding must use its NIMCache placement")
            content_digest = artifact_uri.rsplit("/", 1)[-1]
            plan = catalog.acquisition_plan(model_id)
            canonical_content_uri(
                artifact_uri,
                model_id=model_id,
                content_digest=strong_sha256(
                    content_digest, "artifact URI content digest"
                ),
                scheme=(
                    "nvme"
                    if storage_mode == "local-nvme"
                    else "pvc"
                    if storage_mode == "provider-block-pvc"
                    else "sfs"
                ),
            )
            if (
                storage_mode
                in {
                    "provider-block-pvc",
                    "local-nvme",
                    "nimcache-pvc",
                }
                and placement_receipt is None
            ):
                raise CatalogError(
                    "enabled serving binding requires a placement receipt"
                )
            if storage_mode == "sfs-pvc" and placement_receipt is not None:
                raise CatalogError(
                    "SFS binding must use its acquisition receipt as placement"
                )
            if acquisition_receipt is None:
                raise CatalogError(
                    "enabled serving binding requires an artifact acquisition receipt"
                )
            if prerequisite_receipt is None:
                raise CatalogError(
                    "enabled serving binding requires a prerequisite observation receipt"
                )
            if runtime_tuple is None:
                raise CatalogError(
                    "enabled serving binding requires B300 runtime-tuple evidence"
                )
            if prepared_qualification is None or new_node_qualification is None:
                raise CatalogError(
                    "enabled serving binding requires separate prepared/new-node B300 cohorts"
                )
            if semantic is None:
                raise CatalogError(
                    "enabled serving binding requires semantic qualification evidence"
                )
            if readiness is None:
                raise CatalogError(
                    "enabled serving binding requires readiness evidence"
                )
            verified_valid_until = validate_route_evidence(
                catalog,
                record,
                plan,
                qualification,
                evidence_root,
                backend_identity=backend,
                gateway_identity={
                    "class": gateway_class,
                    "namespace": gateway_namespace,
                    "service_name": gateway_service_name,
                    "service_uid": gateway_service_uid,
                    "port": gateway_port,
                    "identity_sha256": gateway_identity_sha256,
                    "auth_class": gateway_auth_class,
                    "route_id": model_id,
                },
                evidence_session_id=evidence_session_id,
                trusted_attestors=trusted_attestors,
                validation_time=validation_time,
                activation=activation_evidence,
            )
        else:
            if storage_mode is not None:
                raise CatalogError(
                    "federated binding cannot claim local storage placement"
                )
            local_digests = (
                acquisition_receipt,
                prerequisite_receipt,
                target_node_canary,
                placement_receipt,
                runtime_tuple,
                prepared_qualification,
                new_node_qualification,
                semantic,
                readiness,
            )
            if any(value is not None for value in local_digests):
                raise CatalogError(
                    "federated serving cannot mix local B300 qualification receipts"
                )
            if federated_qualification is None:
                raise CatalogError(
                    "federated serving requires signed artifact/readiness/semantic qualification"
                )
            verified_valid_until = validate_federated_route_evidence(
                record,
                qualification,
                evidence_root,
                backend_identity=backend,
                gateway_identity={
                    "class": gateway_class,
                    "namespace": gateway_namespace,
                    "service_name": gateway_service_name,
                    "service_uid": gateway_service_uid,
                    "port": gateway_port,
                    "identity_sha256": gateway_identity_sha256,
                    "auth_class": gateway_auth_class,
                    "route_id": model_id,
                },
                semantic_request_contract=semantic_requests,
                evidence_session_id=evidence_session_id,
                trusted_attestors=trusted_attestors,
                validation_time=validation_time,
            )
        leader_lease_valid_until = activation_evidence["leader_lease_valid_until"]
        if leader_lease_valid_until is not None:
            verified_valid_until = min(verified_valid_until, leader_lease_valid_until)
        if valid_until is None or valid_until != verified_valid_until:
            raise CatalogError(
                "serving binding valid_until must equal the earliest signed evidence expiry or live controller Lease expiry"
            )
    elif (
        ready
        or valid_until is not None
        or artifact_uri is not None
        or storage_mode is not None
        or any(
            digest is not None
            for digest in (
                artifact,
                acquisition_receipt,
                prerequisite_receipt,
                target_node_canary,
                placement_receipt,
                runtime_tuple,
                prepared_qualification,
                new_node_qualification,
                semantic,
                readiness,
                backend_evidence,
                federated_qualification,
                evidence_session_id,
            )
        )
    ):
        raise CatalogError("disabled binding cannot imply partial qualification")

    return ServingBinding(
        model_id=model_id,
        binding_digest=binding_digest,
        model_digest=model_digest,
        enabled=enabled,
        ready=ready,
        valid_until=valid_until,
        execution_mode=execution_mode,
        backend_namespace=backend["namespace"],
        backend_service_name=backend["service_name"],
        backend_port=backend["port"],
        service_origin=origin,
        activation=activation,
        backend_class=backend["class"],
        backend_region=backend["region"],
        backend_gpu_class=backend["gpu_class"],
        backend_runtime_image_digest=backend["runtime_image_digest"],
        backend_endpoint_identity_sha256=backend["endpoint_identity_sha256"],
        backend_trust_bundle_sha256=backend["trust_bundle_sha256"],
        backend_credential_requirement_id=backend["credential_requirement_id"],
        gateway_class=gateway_class,
        gateway_namespace=gateway_namespace,
        gateway_service_name=gateway_service_name,
        gateway_service_uid=gateway_service_uid,
        gateway_port=gateway_port,
        gateway_identity_sha256=gateway_identity_sha256,
        gateway_auth_class=gateway_auth_class,
        protocols=protocols,
        endpoints=MappingProxyType(dict(sorted(endpoints.items()))),
        operations=operations,
        mcp_tool_name=tool_name,
        mcp_description=description,
        mcp_enabled=mcp_enabled,
        artifact_manifest_digest=artifact,
        artifact_uri=artifact_uri,
        acquisition_receipt_digest=acquisition_receipt,
        prerequisite_receipt_digest=prerequisite_receipt,
        target_node_canary_digest=target_node_canary,
        storage_mode=storage_mode,
        placement_receipt_digest=placement_receipt,
        runtime_tuple_digest=runtime_tuple,
        prepared_qualification_digest=prepared_qualification,
        new_node_qualification_digest=new_node_qualification,
        semantic_evidence_digest=semantic,
        readiness_evidence_digest=readiness,
        backend_evidence_digest=backend_evidence,
        federated_qualification_digest=federated_qualification,
        evidence_session_id=evidence_session_id,
    )


def load_serving_bindings(
    path: Path | str,
    catalog: Catalog,
    *,
    evidence_root: Path | str | None = None,
    trusted_attestors: Mapping[str, str] | None = None,
    validation_time: datetime | None = None,
) -> ServingBindings:
    """Load one exact overlay and bind it to catalog/model content digests."""

    value = _exact(
        _load_json(Path(path)), {"schema", "catalog_digest", "bindings"}, "bindings"
    )
    if value["schema"] != SERVING_BINDINGS_SCHEMA:
        raise CatalogError("unsupported serving bindings schema")
    catalog_digest = _optional_digest(
        value["catalog_digest"], "bindings.catalog_digest"
    )
    assert catalog_digest is not None
    if catalog_digest != catalog.digest:
        raise CatalogError("serving bindings catalog digest mismatch")
    raw_bindings = value["bindings"]
    if not isinstance(raw_bindings, dict):
        raise CatalogError("bindings must be keyed by model_id")
    bindings: dict[str, ServingBinding] = {}
    tool_names: set[str] = set()
    for model_id in sorted(raw_bindings):
        if model_id not in catalog.records:
            raise CatalogError(f"serving binding names unknown model: {model_id}")
        binding = _validate_binding(
            model_id,
            raw_bindings[model_id],
            catalog,
            catalog.model(model_id),
            evidence_root=evidence_root,
            trusted_attestors=trusted_attestors,
            validation_time=validation_time,
        )
        if binding.mcp_tool_name in tool_names:
            raise CatalogError(f"duplicate MCP tool name: {binding.mcp_tool_name}")
        tool_names.add(binding.mcp_tool_name)
        bindings[model_id] = binding
    return ServingBindings(
        catalog_digest=catalog_digest, bindings=MappingProxyType(bindings)
    )


def bind_gateway_catalog(catalog: Catalog, bindings: ServingBindings) -> GatewayCatalog:
    """Create the immutable routing view after all cross-contract checks pass."""

    if bindings.catalog_digest != catalog.digest:
        raise CatalogError("serving bindings do not belong to this catalog")
    models: dict[str, GatewayModel] = {}
    for model_id, record in catalog.records.items():
        value = record.to_dict()
        binding = bindings.get(model_id)
        models[model_id] = GatewayModel(
            model_id=model_id,
            display_name=value["model"]["display_name"],
            family=value["model"]["family"],
            model_revision=value["model"]["source"]["revision"],
            runtime_kind=value["runtime"]["kind"],
            runtime_image_digest=value["runtime"]["image"]["digest"],
            gpu_class=value["resources"]["gpu"]["class"],
            gpu_allocation_count=value["resources"]["gpu"]["count"],
            resource_placement_identity_sha256=resource_placement_identity(value),
            semantic_contract=value["semantic_validator"]["contract"],
            execution_mode=value["interface"]["execution_mode"],
            protocols=tuple(value["interface"]["protocols"]),
            endpoints=MappingProxyType(dict(value["interface"]["endpoints"])),
            license_id=value["model"]["source"]["license"]["id"],
            license_state=value["model"]["source"]["license"]["state"],
            entitlement_state=value["model"]["source"]["entitlement"]["state"],
            non_clinical=value["interface"]["policy"]["non_clinical"],
            commercial_use=value["interface"]["policy"]["commercial_use"],
            policy_operations=tuple(value["interface"]["policy"]["operations"]),
            mcp_discoverable=value["interface"]["mcp"]["discoverable"],
            mcp_invocable=bool(
                value["interface"]["mcp"]["invocable"]
                and binding is not None
                and binding.enabled
                and binding.mcp_enabled
            ),
            support_state=value["support"]["state"],
            routable=bool(binding is not None and binding.enabled),
            binding=binding,
            scale_contract=catalog.scale_contract(model_id),
        )
    return GatewayCatalog(
        catalog_version=catalog.version,
        catalog_digest=catalog.digest,
        models=MappingProxyType(dict(sorted(models.items()))),
    )


def load_gateway_catalog(
    catalog_root: Path | str,
    bindings_path: Path | str,
    *,
    repo_root: Path | str | None = None,
    evidence_root: Path | str | None = None,
    trusted_attestors: Mapping[str, str] | None = None,
    validation_time: datetime | None = None,
) -> GatewayCatalog:
    """Stable gateway entry point: canonical base plus a separate overlay."""

    catalog = load_catalog(catalog_root, repo_root=repo_root)
    return bind_gateway_catalog(
        catalog,
        load_serving_bindings(
            bindings_path,
            catalog,
            evidence_root=evidence_root,
            trusted_attestors=trusted_attestors,
            validation_time=validation_time,
        ),
    )


def contract_fixture() -> dict[str, Any]:
    """Small stable fixture for cross-repository gateway contract tests."""

    return {
        "schema": "fs2-serve.nebius.ai/gateway-consumer-fixture/v14",
        "base_model_schema": "fs2-serve.nebius.ai/model/v1",
        "model_variants_schema": "fs2-serve.nebius.ai/model-variants/v4",
        "model_qualification_projection_schema": (
            "fs2-serve.nebius.ai/model-qualification-projection/v1"
        ),
        "model_variant_supply_receipt_schema": (
            "fs2-serve.nebius.ai/model-variant-supply-receipt/v5"
        ),
        "model_variant_supply_object_schema": (
            "fs2-serve.nebius.ai/model-variant-supply-object/v1"
        ),
        "model_variant_license_artifact_schema": "fs2-serve.nebius.ai/model-variant-license-artifact/v1",
        "model_variant_attestor_policy_schema": "fs2-serve.nebius.ai/model-variant-attestor-policy/v1",
        "model_variant_qualification_receipt_schema": (
            "fs2-serve.nebius.ai/model-variant-qualification-receipt/v5"
        ),
        "model_variant_promotions_schema": (
            "fs2-serve.nebius.ai/model-variant-promotions/v4"
        ),
        "model_variant_runtime_tuple_schema": (
            "fs2-serve.nebius.ai/model-variant-runtime-tuple/v1"
        ),
        "model_variant_semantic_receipt_schema": (
            "fs2-serve.nebius.ai/model-variant-semantic-receipt/v2"
        ),
        "model_variant_cohort_schema": "fs2-serve.nebius.ai/model-variant-cohort/v3",
        "model_variant_cold_boundary_receipt_schema": "fs2-serve.nebius.ai/model-variant-cold-boundary-receipt/v1",
        "model_variant_review_receipt_schema": (
            "fs2-serve.nebius.ai/model-variant-review-receipt/v4"
        ),
        "model_variant_backend_readiness_receipt_schema": (
            "fs2-serve.nebius.ai/model-variant-backend-readiness-receipt/v2"
        ),
        "model_variant_kubernetes_observation_schema": "fs2-serve.nebius.ai/model-variant-kubernetes-observation/v1",
        "model_variant_preemption_receipt_schema": "fs2-serve.nebius.ai/model-variant-preemption-receipt/v1",
        "model_variant_lifecycle_receipt_schema": (
            "fs2-serve.nebius.ai/model-variant-lifecycle-receipt/v1"
        ),
        "model_variant_attestor_policy": "one-enabled-canonical-raw-key-principal-per-exact-role-and-group/v1",
        "model_variant_loader": (
            "fs2_serve_catalog.variant_promotions.load_variant_gateway_catalog"
        ),
        "serving_bindings_schema": SERVING_BINDINGS_SCHEMA,
        "backend_capability_schema": "fs2-serve.nebius.ai/backend-capability/v6",
        "protected_storage_class_receipt_schema": "fs2-serve.nebius.ai/protected-storage-class-receipt/v1",
        "provider_block_writer_admission_schema": "fs2-serve.nebius.ai/provider-block-writer-admission/v2",
        "provider_block_pvc_lifecycle_schema": "fs2-serve.nebius.ai/provider-block-pvc-lifecycle-receipt/v4",
        "acquisition_helper_image_admission_schema": "fs2-serve.nebius.ai/acquisition-helper-image-admission/v1",
        "artifact_acquisition_receipt_schema": "fs2-serve.nebius.ai/artifact-acquisition-receipt/v4",
        "artifact_acquisition_worker_result_schema": "fs2-serve.nebius.ai/artifact-acquisition-worker-result/v1",
        "b300_runtime_tuple_schema": "fs2-serve.nebius.ai/b300-runtime-tuple/v5",
        "qualification_cohort_schema": "fs2-serve.nebius.ai/qualification-cohort/v4",
        "scale_contracts_schema": "fs2-serve.nebius.ai/scale-contracts/v6",
        "model_scale_contract_schema": "fs2-serve.nebius.ai/model-scale-contract/v5",
        "activation_intent_schema": "fs2-serve.nebius.ai/postgres-activation-intent/v3",
        "activation_store_schema": "fs2-serve.nebius.ai/postgres-activation-store/v1",
        "replica_field_ownership_receipt_schema": "fs2-serve.nebius.ai/replica-field-ownership-receipt/v1",
        "runtime_startup_receipt_schema": "fs2-serve.nebius.ai/runtime-startup-receipt/v1",
        "zero_to_ready_receipt_schema": "fs2-serve.nebius.ai/zero-to-ready-receipt/v5",
        "return_to_zero_receipt_schema": "fs2-serve.nebius.ai/return-to-zero-receipt/v5",
        "runtime_prerequisites_schema": "fs2-serve.nebius.ai/runtime-prerequisites/v4",
        "observed_prerequisites_schema": "fs2-serve.nebius.ai/observed-prerequisites/v4",
        "ngc_credential_materialization_schema": "fs2-serve.nebius.ai/ngc-credential-materialization/v3",
        "runtime_prerequisite_receipt_schema": "fs2-serve.nebius.ai/runtime-prerequisite-receipt/v4",
        "optional_eso_eligibility_receipt_schema": "fs2-serve.nebius.ai/external-secrets-provider-build-eligibility-receipt/v1",
        "activation_boundary": "gateway-postgres-intent-writer-no-kubernetes-mutation",
        "activation_consumer": {
            "scale_contract_field": "GatewayModel.scale_contract",
            "binding_field": "GatewayModel.binding.activation",
            "intent_interface_field": (
                "GatewayModel.binding.activation.intent_interface_sha256"
            ),
            "intent_interface_digest_source": (
                "sha256(canonical GatewayModel.scale_contract.controller_boundary."
                "activation_intent_interface)"
            ),
            "intent_submission": "PostgresStore.ensure_activation_intent",
            "intent_status_read": "PostgresStore.get_activation_intent",
            "controller_transport": "postgresql-durable-row",
            "controller_subject_fields": [
                "class",
                "namespace",
                "deployment_name",
                "deployment_uid",
                "pod_name",
                "pod_uid",
                "pod_owner_deployment_uid",
                "service_account_name",
                "service_account_uid",
                "leader_lease_name",
                "leader_lease_uid",
                "leader_lease_resource_version",
                "leader_lease_holder_identity",
                "leader_lease_renew_time",
                "leader_lease_duration_seconds",
                "leader_role_namespace",
                "leader_role_name",
                "target_role_namespace",
                "target_role_name",
                "submitter_service_account_name",
                "submitter_service_account_uid",
                "submitter_deployment_name",
                "submitter_deployment_uid",
                "submitter_pod_name",
                "submitter_pod_uid",
                "submitter_pod_owner_deployment_uid",
                "submitter_database_role",
                "claim_owner_database_role",
                "submitter_database_secret",
                "claim_owner_database_secret",
                "database_grants_sha256",
                "activation_store_sha256",
                "activation_store_ddl_sha256",
                "auth_class",
                "intent_interface_sha256",
            ],
            "validity_preflight": "GatewayModel.binding.valid_at(now)",
            "controller_loads_target_and_bounds_from": (
                "GatewayModel.scale_contract.digest"
            ),
            "gateway_kubernetes_mutation": "forbidden",
            "public_projection_omits": [
                "activation-controller",
                "activation-intent-store",
                "activation-target",
            ],
        },
        "semantic_request_contracts_schema": (
            "fs2-serve.nebius.ai/semantic-request-contracts/v1"
        ),
        "semantic_receipt_schema": "fs2-serve.nebius.ai/semantic-receipt/v3",
        "semantic_validation_schema": (
            "fs2-serve.nebius.ai/semantic-validation-result/v3"
        ),
        "federated_qualification_schema": (
            "fs2-serve.nebius.ai/federated-qualification-receipt/v2"
        ),
        "gateway_consumer_schema": GATEWAY_CONSUMER_SCHEMA,
        "gateway_model_schema": GATEWAY_MODEL_SCHEMA,
        "loader": "fs2_serve_catalog.consumer.load_gateway_catalog",
        "routing_authority": (
            "canonical-base-plus-serving-binding-overlay; model variants additionally "
            "require signed-live-variant-promotion"
        ),
        "qualification_projection": {
            "field": "GatewayModel.qualification",
            "activation_authority": "versioned-lean-live-route-inventory",
            "qualification_authority": "reviewed-retained-evidence",
            "snapshot_semantics": (
                "observed_at-reviewed-evidence; current readiness remains a live adapter state"
            ),
            "dimensions": [
                "registered",
                "route_active",
                "runtime_ready",
                "semantic_qualified",
                "http_mcp_qualified",
                "cold_start_qualified",
                "elasticity_qualified",
            ],
        },
        "variant_consumer": {
            "candidate_lookup": "Catalog.model_variant/Catalog.variants_for",
            "static_route_authority": "none",
            "live_loader": (
                "fs2_serve_catalog.variant_promotions.load_variant_gateway_catalog"
            ),
            "live_route_authority": "signed-live-evidence-only",
            "promotion_boundary": (
                "canonical-model-plus-enabled-ready-fresh-serving-binding-plus-api-observed-"
                "backend-plus-scale-contract-plus-role-pinned-signed-live-variant-promotion"
            ),
            "capability_alternative_identity": "must-not-equal-reference-model-id",
        },
        "serving_binding_validity_contract": (
            "valid-until-equals-minimum-signed-evidence-or-live-controller-lease-expiry"
        ),
        "authoritative_qwen_revision": "b968826d9c46dd6066d109eabc6255188de91218",
        "required_promotion_gates": [
            "workload-gpu-allocation-separated-from-node-placement-and-cache-capability",
            "variant-exact-or-distinct-capability-relationship",
            "variant-exact-repository-revision-build-materials-and-immutable-image",
            "variant-full-per-file-artifact-manifest-and-revision-bound-license-bytes",
            "variant-exact-canonical-vendor-nim-baseline-evidence",
            "variant-portable-or-blackwell-sm103-supply-receipt",
            "variant-b300-repeated-kernel-two-semantic-quality-lifecycle-qualification-receipt",
            "variant-separate-cold-n3-and-warm-n10-failure-complete-cohorts",
            "variant-global-attempt-id-chronology-and-node-gpu-nonoverlap",
            "variant-custodied-raw-cosign-dsse-slsa-spdx-scan-and-lifecycle-subjects",
            "variant-single-descriptor-root-dirfd-no-follow-filesystem-custody",
            "variant-one-enabled-canonical-raw-key-principal-per-exact-role-and-group",
            "variant-api-observed-service-endpointslice-pod-node-gpu-probe-chain",
            "variant-per-cold-attempt-zero-pod-absence-new-process-cache-boundary",
            "variant-api-event-backed-preemption-old-fence-and-distinct-replacement",
            "capability-alternative-has-distinct-canonical-base-record",
            "explicit-provider-block-sfs-or-exact-node-local-placement-with-separate-cohorts",
            "signed-server-observed-provider-block-storageclass-uid-resourceversion-exact-spec",
            "signed-protected-storageclass-receipt-required-before-pvc-render",
            "api-controller-lease-sole-writer-admission-and-closed-handoff",
            "complete-unpaginated-api-observed-empty-pvc-mount-set-before-writer-create",
            "controller-only-writer-job-create-rbac-and-admission-fence",
            "provider-block-deterministic-nonroot-ext4-fresh-write-proof",
            "catalog-owned-signed-helper-image-no-caller-override",
            "server-observed-job-pod-uid-and-owner-join",
            "post-job-uid-precondition-and-replacement-safe-cleanup",
            "exact-hf-weight-per-file-sha256-manifest-when-required-by-base",
            "content-addressed-artifact-staged-on-selected-backend",
            "runtime-argv-consumes-mounted-content-with-canonical-served-model-alias",
            "api-observed-deny-egress-before-mounted-content-process-start",
            "local-artifact-acquired-or-federated-artifact-attested",
            "local-runtime-prerequisites-observed-value-suppressed",
            "precreated-ngc-secrets-server-observed-uid-resourceversion-type-key-set",
            "optional-eso-disabled-until-reviewed-eligible-provider-build-receipt",
            "local-ngc-target-node-pull-runtime-canary",
            "b300-qualified-or-qualified-exact-sm90-backend",
            "entitlement-satisfied",
            "immutable-image-and-model",
            "license-verified",
            "semantic-qualified",
            "canonical-two-request-payload-and-licensed-asset-contract",
            "signed-validator-result-bound-to-gateway-transport-readiness",
            "local-prepared-and-new-node-cohorts-or-federated-fresh-readiness",
            "ready-binding",
            "exact-local-worker-tuple-or-federated-runtime-identity",
            "signed-fresh-nonreplayable-attestations",
            "signed-backend-class-region-trust-identity-digest",
            "mode-exclusive-local-or-federated-qualification-receipts",
            "exact-artifact-runtime-semantic-backend-subjects",
            "support-qualified-route-exposed",
            "exact-least-privilege-scale-contract",
            "postgres-submitter-claim-owner-role-grants-and-live-lease-fence",
            "packaged-postgres-ddl-db-clock-cas-idempotency-and-monotonic-model-fence",
            "server-observed-precreated-postgres-secret-pod-ksa-and-lease-identities",
            "zero-bootstrap-activation-owned-replicas-with-gitops-ignore-differences",
            "signed-zero-to-ready-and-return-to-zero-lifecycle",
        ],
    }


def identity_map(catalog: Catalog) -> dict[str, Any]:
    """Return the stable golden identity projection used by review and CI."""

    models: dict[str, Any] = {}
    for model_id in sorted(catalog.records):
        value = catalog.model(model_id).to_dict()
        plan = catalog.acquisition_plan(model_id).to_dict()
        compatibility = dict(catalog.compatibility_audit[model_id])
        semantic_requests = catalog.semantic_request_contract(model_id)
        scale_contract = catalog.scale_contract(model_id)
        projection = {
            "source_kind": value["model"]["source"]["kind"],
            "source_repository": value["model"]["source"]["repository"],
            "source_revision": value["model"]["source"]["revision"],
            "runtime_kind": value["runtime"]["kind"],
            "runtime_image_digest": value["runtime"]["image"]["digest"],
            "runtime_command_sha256": hashlib.sha256(
                canonical_bytes(value["runtime"]["command"])
            ).hexdigest(),
            "execution_identity_sha256": execution_identity(value),
            "execution_mode": value["interface"]["execution_mode"],
            "protocols": value["interface"]["protocols"],
            "endpoints": value["interface"]["endpoints"],
            "gpu_allocation_count": value["resources"]["gpu"]["count"],
            "gpu_allocation_topology": value["resources"]["gpu"]["topology"],
            "resource_placement_identity_sha256": resource_placement_identity(value),
            "gpu_placement": value["resources"]["gpu"]["placement"],
            "b300_state": value["resources"]["gpu"]["b300_state"],
            "compatibility_route_backend_policy": compatibility["route_backend_policy"],
            "acquisition_method": plan["method"],
            "acquisition_repository": plan["repository"],
            "acquisition_revision": plan["revision"],
            "acquisition_prerequisite_ids": plan["required_prerequisites"],
            "acquisition_promotion_policy": plan["promotion_policy"],
            "artifact_kind": value["cache"]["artifact"]["kind"],
            "artifact_manifest_digest": value["cache"]["artifact"]["manifest_digest"],
            "semantic_contract": value["semantic_validator"]["contract"],
            "semantic_source_sha256": value["semantic_validator"]["source_sha256"],
            "semantic_fixture_sha256": value["semantic_validator"]["fixture_sha256"],
            "semantic_identity_sha256": hashlib.sha256(
                canonical_bytes(value["semantic_validator"])
            ).hexdigest(),
            "semantic_request_state": semantic_requests.state,
            "semantic_request_contract_sha256": semantic_requests.digest,
            "semantic_request_asset_set_sha256": semantic_requests.asset_set_digest,
            "scale_contract_sha256": scale_contract.digest,
            "scale_resource_placement_identity_sha256": scale_contract.to_dict()[
                "resource_placement_identity_sha256"
            ],
            "scale_activation_mode": scale_contract.activation_mode,
            "scale_target_template_sha256": (
                None
                if scale_contract.to_dict()["target"] is None
                else scale_contract.to_dict()["target"]["template_identity_sha256"]
            ),
            "semantic_request_ids": list(semantic_requests.request_ids),
            "semantic_request_sha256": list(semantic_requests.request_sha256),
        }
        if value["cache"]["artifact"].get("qualification_gate") is not None:
            artifact = value["cache"]["artifact"]
            historical = artifact["historical_inventory"]
            expected = artifact["expected_identity"]
            projection.update(
                {
                    "artifact_qualification_gate": artifact["qualification_gate"],
                    "expected_artifact_state": expected["state"],
                    "expected_artifact_source_url": expected["source_url"],
                    "expected_artifact_source_revision": expected["source_revision"],
                    "expected_artifact_file_count": expected["file_count"],
                    "expected_artifact_expanded_bytes": expected["expanded_bytes"],
                    "expected_artifact_content_digest": expected["content_digest"],
                    "expected_artifact_manifest_digest": expected["manifest_digest"],
                    "expected_artifact_payload_policy": expected["payload_policy"],
                    "expected_artifact_file_identity_set_sha256": hashlib.sha256(
                        canonical_bytes(expected["files"])
                    ).hexdigest(),
                    "historical_weight_inventory_identity_sha256": historical[
                        "identity_sha256"
                    ],
                    "historical_weight_inventory_file_count": historical["file_count"],
                    "historical_weight_inventory_logical_bytes": historical[
                        "logical_bytes"
                    ],
                    "historical_weight_inventory_per_file_sha256_complete": historical[
                        "per_file_sha256_complete"
                    ],
                }
            )
        source_review = plan.get("source_review")
        qualification_priority = plan.get("qualification_priority")
        if source_review is not None:
            projection.update(
                {
                    "source_license_id": value["model"]["source"]["license"]["id"],
                    "source_license_state": value["model"]["source"]["license"][
                        "state"
                    ],
                    "source_license_artifact_url": source_review["license_file_url"],
                    "source_license_artifact_sha256": source_review[
                        "license_file_sha256"
                    ],
                    "source_review_metadata_sha256": source_review["metadata_sha256"],
                }
            )
        if qualification_priority is not None:
            projection.update(
                {
                    "qualification_priority_rank": qualification_priority["rank"],
                    "qualification_priority_state": qualification_priority["state"],
                    "qualification_priority_startup_mechanism": qualification_priority[
                        "startup_mechanism"
                    ],
                }
            )
        models[model_id] = projection
    return {"schema": "fs2-serve.nebius.ai/golden-identities/v1", "models": models}
