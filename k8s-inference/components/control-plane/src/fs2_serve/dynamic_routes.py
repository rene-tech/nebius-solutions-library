"""Atomic binding of controller-observed ModelDeployments to gateway routes.

The Kubernetes controller owns workloads and reports an exact Service UID and
projection digest.  This module owns no Kubernetes or database client; it only
turns a complete, current publication snapshot into one immutable registry
overlay.  Managed models are withdrawn from any legacy static route before a
dynamic route is considered, so disabling a ModelDeployment cannot revive an
older Terraform route.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from types import MappingProxyType
from typing import Any, Literal

from fs2_serve_catalog.consumer import ActivationBinding, GatewayCatalog, GatewayModel, ServingBinding
from pydantic import AwareDatetime, Field

from .model_deployment import Visibility, canonical_digest
from .model_deployment_publication import (
    DynamicModelPublication,
    DynamicPublicationSnapshot,
    withdraw_invalid_dynamic_publications,
)
from .models import StrictModel


class DynamicRouteError(ValueError):
    """A publication snapshot cannot be projected without ambiguity."""


@dataclass(frozen=True)
class DynamicRoutePolicy:
    tenant_id: str
    visibility: Visibility
    allowed_principal_ids: frozenset[str]
    open_ai: bool
    mcp: bool
    runtime_ready: bool
    deployment_namespace: str
    deployment_name: str
    revision: int
    etag: str
    max_queue_seconds: int
    publication: DynamicModelPublication
    valid_until: datetime


class DynamicDispatchSnapshot(StrictModel):
    """Immutable route material persisted beside an admitted operation."""

    schema_version: Literal["fs2-serve.nebius.ai/dynamic-dispatch-snapshot/v1"] = (
        "fs2-serve.nebius.ai/dynamic-dispatch-snapshot/v1"
    )
    publication: DynamicModelPublication
    valid_until: AwareDatetime
    max_attempts: int = Field(ge=1, le=100)
    max_gpu_seconds_per_attempt: float = Field(gt=0)
    retry_base_seconds: float = Field(gt=0)


@dataclass(frozen=True)
class BoundDynamicRoutes:
    catalog: GatewayCatalog
    policies: dict[str, DynamicRoutePolicy]
    aliases: dict[str, str]
    managed_model_ids: frozenset[str]


@dataclass(frozen=True)
class DynamicRouteRejection:
    namespace: str
    name: str
    model_ref: str
    reason: Literal[
        "canonical-binding-invalid",
        "canonical-identity-conflict",
        "openai-identity-conflict",
        "mcp-identity-conflict",
    ]


@dataclass(frozen=True)
class IsolatedDynamicRoutes:
    snapshot: DynamicPublicationSnapshot
    bound: BoundDynamicRoutes
    rejections: tuple[DynamicRouteRejection, ...]


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _disabled_activation(scale_contract_digest: str) -> ActivationBinding:
    # KEDA observes durable queued/activating/running operations.  The gateway
    # therefore admits a cold dynamic route and polls its normal readiness
    # contract; it does not create a second activation intent or mutate pods.
    return ActivationBinding(
        enabled=False,
        scale_contract_digest=scale_contract_digest,
        controller_namespace="",
        controller_deployment_name="",
        controller_deployment_uid=None,
        controller_pod_name=None,
        controller_pod_uid=None,
        controller_pod_owner_deployment_uid=None,
        controller_service_account_name="",
        controller_service_account_uid=None,
        controller_leader_lease_name="",
        controller_leader_lease_uid=None,
        controller_leader_lease_resource_version=None,
        controller_leader_lease_holder_identity=None,
        controller_leader_lease_renew_time=None,
        controller_leader_lease_duration_seconds=None,
        controller_leader_role_namespace="",
        controller_leader_role_name="",
        controller_target_role_namespace="",
        controller_target_role_name="",
        submitter_service_account_name="",
        submitter_service_account_uid=None,
        submitter_deployment_name="",
        submitter_deployment_uid=None,
        submitter_pod_name=None,
        submitter_pod_uid=None,
        submitter_pod_owner_deployment_uid=None,
        submitter_database_role="",
        claim_owner_database_role="",
        database_grants_sha256="",
        activation_store_sha256="",
        activation_store_ddl_sha256="",
        submitter_database_secret=None,
        claim_owner_database_secret=None,
        controller_identity_sha256=None,
        controller_auth_class="keda-durable-operation-metric",
        intent_interface_sha256="",
        target_api_version=None,
        target_kind=None,
        target_namespace=None,
        target_name=None,
        target_uid=None,
        target_template_identity_sha256=None,
        zero_to_ready_receipt_digest=None,
        return_to_zero_receipt_digest=None,
    )


def _runtime_digest(image: str) -> str:
    marker = "@sha256:"
    if marker not in image:
        raise DynamicRouteError("dynamic runtime image is not digest-pinned")
    digest = f"sha256:{image.rsplit(marker, 1)[1]}"
    if len(digest) != 71 or any(character not in "0123456789abcdef" for character in digest[7:]):
        raise DynamicRouteError("dynamic runtime image digest is invalid")
    return digest


def dynamic_route_policy(
    publication: DynamicModelPublication,
    *,
    valid_until: datetime,
) -> DynamicRoutePolicy:
    return DynamicRoutePolicy(
        tenant_id=publication.tenant_id,
        visibility=publication.visibility,
        allowed_principal_ids=frozenset(publication.allowed_principal_ids),
        open_ai=publication.open_ai,
        mcp=publication.mcp,
        runtime_ready=publication.runtime_ready,
        deployment_namespace=publication.namespace,
        deployment_name=publication.name,
        revision=publication.revision,
        etag=publication.etag,
        max_queue_seconds=publication.max_queue_seconds,
        publication=publication.model_copy(deep=True),
        valid_until=valid_until,
    )


def bind_dynamic_publication(
    base: GatewayModel,
    publication: DynamicModelPublication,
    *,
    valid_until: datetime,
) -> GatewayModel:
    """Bind one complete publication to its retained canonical model."""
    if base.execution_mode != "http":
        raise DynamicRouteError("dynamic publication requires a canonical HTTP model")
    if base.support_state != "qualified":
        raise DynamicRouteError("dynamic publication requires canonical model qualification")
    if base.license_state != "verified" or base.entitlement_state not in {"verified", "not-required"}:
        raise DynamicRouteError("dynamic publication requires verified license and entitlement policy")
    if (
        base.model_revision is None
        or base.runtime_image_digest is None
        or not base.semantic_contract
        or not base.protocols
        or not base.policy_operations
        or set(base.endpoints) != set(base.protocols)
        or any(not value for value in base.endpoints.values())
    ):
        raise DynamicRouteError("dynamic publication requires a complete canonical runtime contract")
    if publication.artifact_revision != base.model_revision:
        raise DynamicRouteError("dynamic artifact revision differs from retained qualification")
    if publication.open_ai and not any(protocol.startswith("openai-") for protocol in base.protocols):
        raise DynamicRouteError("dynamic OpenAI exposure exceeds canonical protocol qualification")
    if publication.mcp and not base.mcp_discoverable:
        raise DynamicRouteError("dynamic MCP exposure exceeds canonical discovery policy")
    runtime_digest = _runtime_digest(publication.runtime_image)
    if base.binding is not None:
        if (
            base.binding.artifact_manifest_digest is not None
            and base.binding.artifact_manifest_digest != publication.artifact_manifest_digest.removeprefix("sha256:")
        ):
            raise DynamicRouteError("dynamic artifact differs from retained qualification")
        if (
            base.binding.backend_runtime_image_digest is not None
            and base.binding.backend_runtime_image_digest != runtime_digest
        ):
            raise DynamicRouteError("dynamic runtime image differs from retained qualification")
    endpoint = publication.endpoint
    endpoint_subject = {
        "namespace": endpoint.namespace,
        "name": endpoint.service_name,
        "port": endpoint.service_port,
        "uid": endpoint.uid,
        "digest": endpoint.digest,
    }
    route_subject: dict[str, Any] = {
        "schema": "fs2-serve.nebius.ai/dynamic-route/v1",
        "model_id": publication.model_ref,
        "tenant_id": publication.tenant_id,
        "revision": publication.revision,
        "etag": publication.etag,
        "artifact_revision": publication.artifact_revision,
        "artifact_manifest_digest": publication.artifact_manifest_digest,
        "runtime_profile": publication.runtime_profile,
        "runtime_image_digest": runtime_digest,
        "pool_ref": publication.admitted_pool_ref,
        "max_queue_seconds": publication.max_queue_seconds,
        "endpoint": endpoint_subject,
        "open_ai": publication.open_ai,
        "aliases": publication.open_ai_aliases,
        "mcp": publication.mcp,
        "mcp_tool_name": publication.mcp_tool_name,
        "policy_ref": publication.policy_ref,
    }
    tool_name = publication.mcp_tool_name or f"model_{_digest(publication.model_ref)[:16]}"
    binding = ServingBinding(
        model_id=publication.model_ref,
        binding_digest=_digest(route_subject),
        model_digest=_digest(base.to_dict()),
        enabled=True,
        ready=publication.runtime_ready,
        valid_until=valid_until.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z"),
        execution_mode=base.execution_mode,
        backend_namespace=endpoint.namespace,
        backend_service_name=endpoint.service_name,
        backend_port=endpoint.service_port,
        service_origin=(
            f"http://{endpoint.service_name}.{endpoint.namespace}.svc.cluster.local:{endpoint.service_port}"
        ),
        activation=_disabled_activation(base.scale_contract.digest),
        backend_class="local-kubernetes",
        backend_region=None,
        backend_gpu_class=base.gpu_class,
        backend_runtime_image_digest=runtime_digest,
        backend_endpoint_identity_sha256=_digest(endpoint_subject),
        backend_trust_bundle_sha256=None,
        backend_credential_requirement_id=None,
        gateway_class="fs2-serve-gateway",
        gateway_namespace="fs2-system",
        gateway_service_name="fs2-serve-control-plane",
        gateway_service_uid="dynamic-model-control-plane",
        gateway_port=8080,
        gateway_identity_sha256=_digest({"namespace": "fs2-system", "name": "fs2-serve-control-plane", "port": 8080}),
        gateway_auth_class="scoped-api-key",
        protocols=base.protocols,
        endpoints=MappingProxyType(dict(base.endpoints)),
        operations=base.policy_operations,
        mcp_tool_name=tool_name,
        mcp_description=f"Invoke {base.display_name} through the managed fs2-serve route.",
        mcp_enabled=publication.mcp,
        artifact_manifest_digest=publication.artifact_manifest_digest,
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
        evidence_session_id=f"modeldeployment:{publication.namespace}/{publication.name}:{publication.revision}",
    )
    return replace(
        base,
        # Bind admitted operations to the complete desired-state revision, not
        # only to the underlying model artifact revision.  A policy, endpoint,
        # runtime, or scaling update therefore invalidates stale queue claims.
        model_revision=f"dynamic:{publication.etag}",
        runtime_image_digest=runtime_digest,
        gpu_allocation_count=publication.accelerators_per_replica,
        # Runtime readiness is not semantic/artifact qualification. Preserve
        # the canonical catalog decision; the Terraform envelope separately
        # narrows the exact artifact/runtime/accelerator tuple.
        support_state=base.support_state,
        routable=True,
        mcp_invocable=publication.mcp,
        binding=binding,
    )


def bind_dynamic_publications(
    base: GatewayCatalog,
    snapshot: DynamicPublicationSnapshot,
    *,
    valid_until: datetime,
) -> BoundDynamicRoutes:
    """Replace managed static routes and bind every unambiguous observation."""

    if valid_until.tzinfo is None:
        raise DynamicRouteError("dynamic route validity deadline must be timezone-aware")
    models = dict(base.models)
    managed_claims = frozenset(item.model_ref for item in snapshot.assessments)
    unknown = sorted(
        publication.model_ref for publication in snapshot.publications if publication.model_ref not in models
    )
    if unknown:
        raise DynamicRouteError("dynamic snapshot contains an unknown canonical model")
    managed = frozenset(model_id for model_id in managed_claims if model_id in models)
    for model_id in managed:
        models[model_id] = replace(models[model_id], routable=False, mcp_invocable=False, binding=None)

    policies: dict[str, DynamicRoutePolicy] = {}
    aliases: dict[str, str] = {}
    mcp_tool_owners: dict[str, str] = {}
    for model_id, model in base.models.items():
        if model_id in managed or not model.routable or not model.mcp_invocable or model.binding is None:
            continue
        if not model.binding.mcp_enabled:
            continue
        for protocol in model.protocols:
            tool_name = f"{model.binding.mcp_tool_name}_{protocol.replace('-', '_')}"
            if tool_name in mcp_tool_owners:
                raise DynamicRouteError("canonical MCP tool identity is ambiguous")
            mcp_tool_owners[tool_name] = model_id
    for publication in snapshot.publications:
        model_id = publication.model_ref
        if model_id in policies:
            raise DynamicRouteError("dynamic snapshot binds one canonical model more than once")
        if publication.mcp and publication.mcp_tool_name is not None:
            for protocol in base.models[model_id].protocols:
                tool_name = f"{publication.mcp_tool_name}_{protocol.replace('-', '_')}"
                if tool_name in mcp_tool_owners:
                    raise DynamicRouteError("dynamic MCP tool collides with another active model")
                mcp_tool_owners[tool_name] = model_id
        models[model_id] = bind_dynamic_publication(base.models[model_id], publication, valid_until=valid_until)
        policies[model_id] = dynamic_route_policy(publication, valid_until=valid_until)
        for alias in publication.open_ai_aliases:
            if alias in models or alias in aliases:
                raise DynamicRouteError("dynamic OpenAI alias collides with another catalog identity")
            aliases[alias] = model_id

    catalog = replace(base, models=MappingProxyType(dict(sorted(models.items()))))
    return BoundDynamicRoutes(
        catalog=catalog,
        policies=dict(sorted(policies.items())),
        aliases=dict(sorted(aliases.items())),
        managed_model_ids=managed,
    )


def bind_dynamic_publications_isolated(
    base: GatewayCatalog,
    snapshot: DynamicPublicationSnapshot,
    *,
    valid_until: datetime,
) -> IsolatedDynamicRoutes:
    """Bind independent models while withdrawing only exact invalid owners.

    Structural snapshot ambiguity remains a global error. Once every
    publication has one exact assessment identity, canonical binding and
    cross-route identity failures are attributable to bounded model owners and
    can be withdrawn without taking unrelated dynamic routes offline.
    """

    if valid_until.tzinfo is None:
        raise DynamicRouteError("dynamic route validity deadline must be timezone-aware")
    assessment_identities = [(item.namespace, item.name) for item in snapshot.assessments]
    if len(assessment_identities) != len(set(assessment_identities)):
        raise DynamicRouteError("dynamic snapshot assessment identity is ambiguous")
    publications_by_identity: dict[tuple[str, str], DynamicModelPublication] = {}
    for publication in snapshot.publications:
        identity = (publication.namespace, publication.name)
        if identity in publications_by_identity:
            raise DynamicRouteError("dynamic snapshot publication identity is ambiguous")
        publications_by_identity[identity] = publication
    assessed_publications = {
        (item.namespace, item.name): item.publication for item in snapshot.assessments if item.publication is not None
    }
    if publications_by_identity != assessed_publications:
        raise DynamicRouteError("dynamic snapshot publications differ from their assessments")
    payload = {
        "schema_version": snapshot.schema_version,
        "assessments": [item.model_dump(mode="json") for item in snapshot.assessments],
        "publications": [item.model_dump(mode="json") for item in snapshot.publications],
    }
    if snapshot.digest != canonical_digest(payload):
        raise DynamicRouteError("dynamic snapshot digest differs from its inventory")

    invalid: dict[tuple[str, str], DynamicRouteRejection] = {}

    def reject(
        publication: DynamicModelPublication,
        reason: Literal[
            "canonical-binding-invalid",
            "canonical-identity-conflict",
            "openai-identity-conflict",
            "mcp-identity-conflict",
        ],
    ) -> None:
        identity = (publication.namespace, publication.name)
        invalid.setdefault(
            identity,
            DynamicRouteRejection(
                namespace=publication.namespace,
                name=publication.name,
                model_ref=publication.model_ref,
                reason=reason,
            ),
        )

    model_owners: dict[str, list[DynamicModelPublication]] = {}
    for publication in publications_by_identity.values():
        model_owners.setdefault(publication.model_ref, []).append(publication)
    for owners in model_owners.values():
        if len(owners) > 1:
            for publication in owners:
                reject(publication, "canonical-identity-conflict")

    ordered = sorted(
        snapshot.publications,
        key=lambda item: (item.namespace, item.name, item.model_ref),
    )
    for publication in ordered:
        base_model = base.models.get(publication.model_ref)
        if base_model is None:
            reject(publication, "canonical-binding-invalid")
            continue
        try:
            bind_dynamic_publication(base_model, publication, valid_until=valid_until)
        except (DynamicRouteError, KeyError, ValueError):
            reject(publication, "canonical-binding-invalid")
            continue
        if any(alias in base.models for alias in publication.open_ai_aliases):
            reject(publication, "openai-identity-conflict")

    candidates = [publication for publication in ordered if (publication.namespace, publication.name) not in invalid]
    alias_owners: dict[str, list[DynamicModelPublication]] = {}
    for publication in candidates:
        for alias in publication.open_ai_aliases:
            alias_owners.setdefault(alias, []).append(publication)
    for owners in alias_owners.values():
        if len(owners) > 1:
            for publication in owners:
                reject(publication, "openai-identity-conflict")

    managed_model_ids = {item.model_ref for item in snapshot.assessments}
    mcp_tool_owners: dict[str, str] = {}
    for model_id, model in base.models.items():
        if model_id in managed_model_ids or not model.routable or not model.mcp_invocable or model.binding is None:
            continue
        if not model.binding.mcp_enabled:
            continue
        for protocol in model.protocols:
            tool_name = f"{model.binding.mcp_tool_name}_{protocol.replace('-', '_')}"
            if tool_name in mcp_tool_owners:
                raise DynamicRouteError("canonical MCP tool identity is ambiguous")
            mcp_tool_owners[tool_name] = model_id

    dynamic_tool_owners: dict[str, list[DynamicModelPublication]] = {}
    for publication in candidates:
        if (publication.namespace, publication.name) in invalid or not publication.mcp:
            continue
        assert publication.mcp_tool_name is not None
        base_model = base.models[publication.model_ref]
        for protocol in base_model.protocols:
            tool_name = f"{publication.mcp_tool_name}_{protocol.replace('-', '_')}"
            if tool_name in mcp_tool_owners:
                reject(publication, "mcp-identity-conflict")
            dynamic_tool_owners.setdefault(tool_name, []).append(publication)
    for owners in dynamic_tool_owners.values():
        active = [publication for publication in owners if (publication.namespace, publication.name) not in invalid]
        if len(active) > 1:
            for publication in active:
                reject(publication, "mcp-identity-conflict")

    rejected_identities = frozenset(invalid)
    isolated_snapshot = (
        withdraw_invalid_dynamic_publications(snapshot, rejected_identities) if rejected_identities else snapshot
    )
    bound = bind_dynamic_publications(base, isolated_snapshot, valid_until=valid_until)
    return IsolatedDynamicRoutes(
        snapshot=isolated_snapshot,
        bound=bound,
        rejections=tuple(invalid[identity] for identity in sorted(invalid)),
    )
