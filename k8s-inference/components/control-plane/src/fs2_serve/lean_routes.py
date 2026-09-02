"""Small, fail-closed route overlay for already-Ready retained workloads.

This is intentionally narrower than the signed promotion machinery.  It grants
no Kubernetes mutation authority and may only bind canonical catalog records
to retained digest-pinned Services in the fs2-models namespace. The
``ephemeral-emptydir`` storage value is deliberately local to this overlay: it
truthfully records that the already-Ready worker localizes weights into an
ephemeral Kubernetes ``emptyDir`` without broadening the promoted catalog's
durable-storage vocabulary.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from types import MappingProxyType
from typing import Any

from fs2_serve_catalog.consumer import ActivationBinding, GatewayCatalog, ServingBinding
from fs2_serve_catalog.loader import Catalog, CatalogError

from .qualification import QualificationError, validate_qualification_projection

LEAN_ROUTES_SCHEMA = "fs2-serve.nebius.ai/lean-routes/v4"
QUALIFIED_LEAN_ROUTES_SCHEMA = "fs2-serve.nebius.ai/lean-routes/v3"
LEGACY_LEAN_ROUTES_SCHEMA = "fs2-serve.nebius.ai/lean-routes/v2"
_DIGEST = re.compile(r"sha256:[a-f0-9]{64}")
_DNS_LABEL = re.compile(r"[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?")
_MODEL_REVISION = re.compile(r"[a-f0-9]{40}|[a-f0-9]{64}|sha256:[a-f0-9]{64}")
_TOOL_NAME = re.compile(r"[a-z][a-z0-9_]{0,63}")
_REGION = re.compile(r"[a-z][a-z0-9-]{1,31}[a-z0-9]")
_GPU_CLASS = re.compile(r"[a-z0-9][a-z0-9-]{1,126}[a-z0-9]")
_POOL_ID = re.compile(r"[a-z0-9][a-z0-9-]{1,126}[a-z0-9]")
_STORAGE_MODES = {"provider-block-pvc", "sfs-pvc", "local-nvme", "ephemeral-emptydir"}
_MAX_BYTES = 64 * 1024


class LeanRouteError(ValueError):
    """The retained hot-route file is malformed or exceeds its authority."""


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")


def _sha256(value: object) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _exact(value: object, keys: set[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != keys:
        raise LeanRouteError(f"{label} fields are invalid")
    return value


def _load_json(path: Path) -> dict[str, Any]:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise LeanRouteError("lean route file is unavailable") from exc
    if not raw or len(raw) > _MAX_BYTES:
        raise LeanRouteError("lean route file size is invalid")

    def reject_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise LeanRouteError("lean route file contains a duplicate key")
            result[key] = value
        return result

    try:
        value = json.loads(raw, object_pairs_hook=reject_duplicates)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise LeanRouteError("lean route file is not valid JSON") from exc
    if not isinstance(value, dict):
        raise LeanRouteError("lean route document fields are invalid")
    schema = value.get("schema")
    fields = set(value)
    if schema == LEGACY_LEAN_ROUTES_SCHEMA:
        if fields != {"schema", "routes"}:
            raise LeanRouteError("legacy lean route v2 cannot include qualification")
    elif schema == QUALIFIED_LEAN_ROUTES_SCHEMA:
        if fields != {"schema", "routes", "qualification"}:
            raise LeanRouteError("qualified lean route v3 requires qualification")
    elif schema == LEAN_ROUTES_SCHEMA:
        if fields != {"schema", "routes"}:
            raise LeanRouteError("Terraform lean route v4 fields are invalid")
    else:
        raise LeanRouteError("lean route schema is invalid")
    return value


def _disabled_activation(scale_contract_digest: str) -> ActivationBinding:
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
        controller_auth_class="none-static-hot-route",
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


def bind_lean_routes(
    gateway: GatewayCatalog,
    path: Path,
    *,
    catalog: Catalog,
) -> tuple[GatewayCatalog, frozenset[str]]:
    """Overlay exact static-hot Service routes on an otherwise canonical catalog."""

    document = _load_json(path)
    routes = document["routes"]
    if (
        not isinstance(routes, list)
        or len(routes) > len(catalog.tested_model_ids)
        or (not routes and document["schema"] != LEAN_ROUTES_SCHEMA)
    ):
        raise LeanRouteError("lean route count is invalid")
    qualification_rows: dict[str, Mapping[str, Any]] = {}
    qualification_metadata: dict[str, Any] = {}
    if "qualification" in document:
        try:
            qualification_rows = validate_qualification_projection(
                catalog,
                routes,
                document["qualification"],
            )
            qualification_metadata = {
                "qualification_authority": document["qualification"]["qualification_authority"],
                "observed_at": document["qualification"]["observed_at"],
            }
        except QualificationError as exc:
            raise LeanRouteError("lean route qualification projection is invalid") from exc
    elif document["schema"] == LEGACY_LEAN_ROUTES_SCHEMA and len(routes) == len(catalog.tested_model_ids):
        raise LeanRouteError("full-catalog lean routes require the qualification projection")

    models = dict(gateway.models)
    routed: set[str] = set()
    for index, raw_route in enumerate(routes):
        route_fields = {
            "model_id",
            "variant_id",
            "model_revision",
            "runtime_image_digest",
            "service",
            "storage_mode",
            "protocols",
            "operations",
            "mcp",
        }
        if document["schema"] == LEAN_ROUTES_SCHEMA:
            route_fields.add("placement")
        route = _exact(
            raw_route,
            route_fields,
            f"lean route {index}",
        )
        model_id = route["model_id"]
        if not isinstance(model_id, str) or model_id in routed:
            raise LeanRouteError("lean route model identity is invalid or duplicated")
        try:
            base = models[model_id]
        except KeyError as exc:
            raise LeanRouteError("lean route names an unknown canonical model") from exc
        if base.routable:
            raise LeanRouteError("lean route cannot replace an already-routable canonical binding")
        if base.execution_mode != "http":
            raise LeanRouteError("lean route requires a canonical HTTP execution model")
        revision = route["model_revision"]
        if not isinstance(revision, str) or _MODEL_REVISION.fullmatch(revision) is None:
            raise LeanRouteError("lean route model revision is invalid")
        runtime_digest = route["runtime_image_digest"]
        if not isinstance(runtime_digest, str) or _DIGEST.fullmatch(runtime_digest) is None:
            raise LeanRouteError("lean route runtime image is not digest-pinned")
        variant_id = route["variant_id"]
        if variant_id is None:
            if revision != base.model_revision:
                raise LeanRouteError("lean route model revision differs from the canonical catalog")
            if runtime_digest != base.runtime_image_digest:
                raise LeanRouteError("lean route runtime image differs from the canonical catalog")
        else:
            if not isinstance(variant_id, str) or _DNS_LABEL.fullmatch(variant_id) is None:
                raise LeanRouteError("lean route variant identity is invalid")
            try:
                variant = catalog.model_variant(variant_id)
                fallback, profile = catalog.fallback_for_variant(variant_id)
            except CatalogError as exc:
                raise LeanRouteError("lean route variant is not in the canonical fallback graph") from exc
            value = variant.to_dict()
            runtime = value["runtime"]
            source = value["source"]
            promotion = value["promotion"]
            if (
                variant.base_model_id != model_id
                or variant.exposed_model_id != model_id
                or variant.relationship != "exact-model"
                or variant.runtime_architecture != profile
                or fallback.relationship != "exact-model"
                or source["revision"] != revision
                or runtime["build_state"] != "built-attested"
                or not isinstance(runtime["device_capability"], str)
                or not runtime["device_capability"].endswith("-qualified")
                or runtime["image_digest"] != runtime_digest
                or promotion["route_exposed"] is not False
            ):
                raise LeanRouteError("lean route differs from its exact qualified variant")

        service = _exact(route["service"], {"namespace", "name", "port"}, "lean route service")
        namespace = service["namespace"]
        name = service["name"]
        port = service["port"]
        if namespace != "fs2-models" or not isinstance(name, str) or _DNS_LABEL.fullmatch(name) is None:
            raise LeanRouteError("lean route service must be a valid Service in fs2-models")
        if name != model_id and not name.startswith(f"{model_id}-"):
            raise LeanRouteError("lean route service name must be owned by its model ID")
        if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
            raise LeanRouteError("lean route service port is invalid")

        protocols = route["protocols"]
        if (
            not isinstance(protocols, dict)
            or tuple(sorted(protocols)) != tuple(sorted(base.protocols))
            or protocols != dict(base.endpoints)
        ):
            raise LeanRouteError("lean route endpoints differ from the canonical interface")
        operations = route["operations"]
        if not isinstance(operations, list) or tuple(operations) != base.policy_operations:
            raise LeanRouteError("lean route operations differ from the canonical catalog")
        storage_mode = route["storage_mode"]
        if storage_mode not in _STORAGE_MODES:
            raise LeanRouteError("lean route storage mode is invalid")

        mcp = _exact(route["mcp"], {"enabled", "tool_name", "description"}, "lean route MCP")
        if not isinstance(mcp["enabled"], bool) or (mcp["enabled"] and not base.mcp_discoverable):
            raise LeanRouteError("lean route MCP exposure exceeds the canonical discovery policy")
        if not isinstance(mcp["tool_name"], str) or _TOOL_NAME.fullmatch(mcp["tool_name"]) is None:
            raise LeanRouteError("lean route MCP tool name is invalid")
        description = mcp["description"]
        if (
            not isinstance(description, str)
            or not 1 <= len(description) <= 240
            or any(marker in description.lower() for marker in ("http://", "https://", "token", "secret", "credential"))
        ):
            raise LeanRouteError("lean route MCP description is invalid")

        if document["schema"] == LEAN_ROUTES_SCHEMA:
            placement = _exact(
                route["placement"],
                {"region", "accelerator_class", "pool_id"},
                "lean route placement",
            )
            backend_region = placement["region"]
            backend_gpu_class = placement["accelerator_class"]
            pool_id = placement["pool_id"]
            if not isinstance(backend_region, str) or _REGION.fullmatch(backend_region) is None:
                raise LeanRouteError("lean route placement region is invalid")
            if not isinstance(backend_gpu_class, str) or _GPU_CLASS.fullmatch(backend_gpu_class) is None:
                raise LeanRouteError("lean route placement accelerator class is invalid")
            if pool_id is not None and (not isinstance(pool_id, str) or _POOL_ID.fullmatch(pool_id) is None):
                raise LeanRouteError("lean route placement pool ID is invalid")
        else:
            # The reviewed v2/v3 retained-route campaigns ran in us-north1 and
            # predate an explicit deployment projection. Preserve their exact
            # interpretation while requiring all new Terraform routes to carry
            # their resolved region and accelerator class in v4.
            placement = None
            backend_region = "us-north1"
            backend_gpu_class = base.gpu_class

        service_subject = {"namespace": namespace, "name": name, "port": port}
        route_subject = {
            "schema": document["schema"],
            "model_id": model_id,
            "variant_id": variant_id,
            "model_revision": revision,
            "runtime_image_digest": runtime_digest,
            "service": service_subject,
            "storage_mode": storage_mode,
            "protocols": protocols,
            "operations": operations,
            "mcp": mcp,
        }
        if placement is not None:
            route_subject["placement"] = placement
        endpoint_identity = _sha256(service_subject)
        binding = ServingBinding(
            model_id=model_id,
            binding_digest=_sha256(route_subject),
            model_digest=_sha256(base.to_dict()),
            enabled=True,
            ready=True,
            valid_until=None,
            execution_mode=base.execution_mode,
            backend_namespace=namespace,
            backend_service_name=name,
            backend_port=port,
            service_origin=f"http://{name}.{namespace}.svc.cluster.local:{port}",
            activation=_disabled_activation(base.scale_contract.digest),
            backend_class="local-kubernetes",
            backend_region=backend_region,
            backend_gpu_class=backend_gpu_class,
            backend_runtime_image_digest=runtime_digest,
            backend_endpoint_identity_sha256=endpoint_identity,
            backend_trust_bundle_sha256=None,
            backend_credential_requirement_id=None,
            gateway_class="fs2-serve-gateway",
            gateway_namespace="fs2-system",
            gateway_service_name="fs2-serve-control-plane",
            gateway_service_uid="lean-release-runtime-observed",
            gateway_port=8080,
            gateway_identity_sha256=_sha256(
                {"namespace": "fs2-system", "name": "fs2-serve-control-plane", "port": 8080}
            ),
            gateway_auth_class="scoped-api-key",
            protocols=tuple(protocols),
            endpoints=MappingProxyType(dict(protocols)),
            operations=tuple(operations),
            mcp_tool_name=mcp["tool_name"],
            mcp_description=description,
            mcp_enabled=mcp["enabled"],
            artifact_manifest_digest=None,
            artifact_uri=None,
            storage_mode=storage_mode,
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
            evidence_session_id="lean-static-hot-route",
        )
        models[model_id] = replace(
            base,
            model_revision=revision,
            runtime_image_digest=runtime_digest,
            gpu_class=backend_gpu_class,
            support_state="lean-live-verified",
            routable=True,
            mcp_invocable=mcp["enabled"],
            binding=binding,
            qualification=(
                None
                if model_id not in qualification_rows
                else MappingProxyType(
                    {
                        **dict(qualification_rows[model_id]),
                        **qualification_metadata,
                    }
                )
            ),
        )
        routed.add(model_id)
    return replace(gateway, models=MappingProxyType(dict(sorted(models.items())))), frozenset(routed)
