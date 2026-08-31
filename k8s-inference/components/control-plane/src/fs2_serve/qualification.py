"""Authoritative, fail-closed model/runtime qualification projection."""

from __future__ import annotations

import copy
import re
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any

from fs2_serve_catalog.loader import Catalog, CatalogError, strong_sha256

OBSERVATIONS_SCHEMA = "fs2-serve.nebius.ai/model-qualification-observations/v1"
PROJECTION_SCHEMA = "fs2-serve.nebius.ai/model-qualification-projection/v1"
ACTIVATION_AUTHORITY = "versioned-lean-live-route-inventory"
QUALIFICATION_AUTHORITY = "reviewed-retained-evidence"
_RFC3339_SECONDS = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z")
_OBSERVATION_KEYS = {
    "schema",
    "authority",
    "observed_at",
    "evidence",
    "active_variant_bindings",
    "runtime_ready_models",
    "semantic_qualified_models",
    "http_mcp_qualified_models",
    "cold_start_qualified_models",
    "elasticity_qualified_models",
}
_EVIDENCE_KEYS = {
    "audited_catalog_sha256",
    "audited_live_routes_sha256",
    "retained_deployments_sha256",
    "model_discovery_sha256",
    "mcp_discovery_sha256",
    "http_mcp_acceptance_sha256",
    "cold_start_acceptance_sha256",
}
_STATE_KEYS = {
    "registered",
    "route_active",
    "runtime_ready",
    "semantic_qualified",
    "http_mcp_qualified",
    "cold_start_qualified",
    "elasticity_qualified",
}


class QualificationError(ValueError):
    """Qualification input or projection contradicts catalog/runtime truth."""


def _exact(value: object, keys: set[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != keys:
        raise QualificationError(f"{label} fields are invalid")
    return value


def _model_set(value: object, *, allowed: frozenset[str], label: str) -> frozenset[str]:
    if (
        not isinstance(value, list)
        or value != sorted(value)
        or len(value) != len(set(value))
        or not set(value).issubset(allowed)
    ):
        raise QualificationError(f"{label} must be a sorted unique subset of tested models")
    return frozenset(value)


def _evidence(value: object) -> dict[str, str]:
    evidence = _exact(value, _EVIDENCE_KEYS, "qualification evidence")
    try:
        validated = {key: strong_sha256(item, f"qualification evidence {key}") for key, item in evidence.items()}
    except CatalogError as exc:
        raise QualificationError("qualification evidence must contain strong SHA-256 digests") from exc
    return dict(sorted(validated.items()))


def _utc_second(value: object) -> str:
    if not isinstance(value, str) or _RFC3339_SECONDS.fullmatch(value) is None:
        raise QualificationError("qualification observation time is invalid")
    try:
        parsed = datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError as exc:
        raise QualificationError("qualification observation time is invalid") from exc
    if f"{parsed.isoformat(timespec='seconds')}Z" != value:
        raise QualificationError("qualification observation time is invalid")
    return value


def _route_map(catalog: Catalog, routes: Sequence[Mapping[str, Any]]) -> dict[str, Mapping[str, Any]]:
    expected = frozenset(catalog.tested_model_ids)
    mapped: dict[str, Mapping[str, Any]] = {}
    for route in routes:
        model_id = route.get("model_id")
        if not isinstance(model_id, str) or model_id in mapped:
            raise QualificationError("qualification routes contain a missing or duplicate model ID")
        mapped[model_id] = route
    if frozenset(mapped) != expected:
        raise QualificationError("qualification routes differ from the complete tested model universe")
    return mapped


def _active_variant_bindings(value: object, *, allowed: frozenset[str]) -> dict[str, str]:
    if not isinstance(value, dict):
        raise QualificationError("active variant bindings must be an object")
    bindings: dict[str, str] = {}
    for model_id, variant_id in value.items():
        if (
            not isinstance(model_id, str)
            or model_id not in allowed
            or not isinstance(variant_id, str)
            or not variant_id
        ):
            raise QualificationError("active variant binding identity is invalid")
        bindings[model_id] = variant_id
    return dict(sorted(bindings.items()))


def _runtime_origin(catalog: Catalog, model_id: str, variant_id: object) -> dict[str, Any]:
    if variant_id is not None:
        if not isinstance(variant_id, str):
            raise QualificationError(f"{model_id}: variant ID is invalid")
        try:
            variant = catalog.model_variant(variant_id)
            fallback, profile = catalog.fallback_for_variant(variant_id)
        except CatalogError as exc:
            raise QualificationError(f"{model_id}: variant is not in the canonical graph") from exc
        value = variant.to_dict()
        if (
            variant.base_model_id != model_id
            or variant.exposed_model_id != model_id
            or variant.runtime_architecture != profile
            or fallback.relationship != variant.relationship
        ):
            raise QualificationError(f"{model_id}: active variant relationship is contradictory")
        return {
            "kind": "independent-runtime",
            "variant_id": variant_id,
            "source_kind": value["source"]["kind"],
            "repository": value["source"]["repository"],
            "relationship": value["relationship"]["kind"],
            "nim_artifact_parity": value["relationship"]["nim_artifact_parity"],
        }

    record = catalog.model(model_id).to_dict()
    runtime_kind = record["runtime"]["kind"]
    source = record["model"]["source"]
    is_nim = runtime_kind == "nim" and source["kind"] == "ngc-nim"
    return {
        "kind": "nvidia-nim" if is_nim else "independent-runtime",
        "variant_id": None,
        "source_kind": source["kind"],
        "repository": source["repository"],
        "relationship": "canonical-nim-package" if is_nim else "canonical-runtime",
        "nim_artifact_parity": "verified" if is_nim else "not-applicable",
    }


def _policy(catalog: Catalog, model_id: str) -> dict[str, Any]:
    record = catalog.model(model_id).to_dict()
    value = record["interface"]["policy"]
    return {
        "license_id": record["model"]["source"]["license"]["id"],
        "non_clinical": value["non_clinical"],
        "commercial_use": value["commercial_use"],
    }


def build_qualification_projection(
    catalog: Catalog,
    routes: Sequence[Mapping[str, Any]],
    raw_observations: object,
) -> dict[str, Any]:
    """Join reviewed state flags to exact catalog and active-route identities."""

    route_by_model = _route_map(catalog, routes)
    expected = frozenset(route_by_model)
    observations = _exact(raw_observations, _OBSERVATION_KEYS, "qualification observations")
    if observations["schema"] != OBSERVATIONS_SCHEMA:
        raise QualificationError("qualification observation schema is unsupported")
    if observations["authority"] != QUALIFICATION_AUTHORITY:
        raise QualificationError("qualification observations name the wrong authority")
    observed_at = _utc_second(observations["observed_at"])
    evidence = _evidence(observations["evidence"])
    active_variant_bindings = _active_variant_bindings(observations["active_variant_bindings"], allowed=expected)
    routed_variant_bindings = {
        model_id: route["variant_id"] for model_id, route in route_by_model.items() if route["variant_id"] is not None
    }
    if routed_variant_bindings != active_variant_bindings:
        raise QualificationError("reviewed active variant bindings differ from the live route inventory")
    states = {
        field.removesuffix("_models"): _model_set(observations[field], allowed=expected, label=field)
        for field in (
            "runtime_ready_models",
            "semantic_qualified_models",
            "http_mcp_qualified_models",
            "cold_start_qualified_models",
            "elasticity_qualified_models",
        )
    }

    rows: list[dict[str, Any]] = []
    for model_id in sorted(route_by_model):
        route = route_by_model[model_id]
        service = route["service"]
        rows.append(
            {
                "model_id": model_id,
                "variant_id": route["variant_id"],
                "active_runtime": {
                    "model_revision": route["model_revision"],
                    "runtime_image_digest": route["runtime_image_digest"],
                    "service": {
                        "namespace": service["namespace"],
                        "name": service["name"],
                        "port": service["port"],
                    },
                },
                "runtime_origin": _runtime_origin(catalog, model_id, route["variant_id"]),
                "states": {
                    "registered": True,
                    "route_active": True,
                    "runtime_ready": model_id in states["runtime_ready"],
                    "semantic_qualified": model_id in states["semantic_qualified"],
                    "http_mcp_qualified": model_id in states["http_mcp_qualified"],
                    "cold_start_qualified": model_id in states["cold_start_qualified"],
                    "elasticity_qualified": model_id in states["elasticity_qualified"],
                },
                "policy": _policy(catalog, model_id),
                "evidence": dict(evidence),
            }
        )
    projection = {
        "schema": PROJECTION_SCHEMA,
        "activation_authority": ACTIVATION_AUTHORITY,
        "qualification_authority": QUALIFICATION_AUTHORITY,
        "observed_at": observed_at,
        "rows": rows,
    }
    validate_qualification_projection(catalog, routes, projection)
    return projection


def validate_qualification_projection(
    catalog: Catalog,
    routes: Sequence[Mapping[str, Any]],
    raw_projection: object,
) -> dict[str, Mapping[str, Any]]:
    """Validate every row against the active route and canonical source graph."""

    route_by_model = _route_map(catalog, routes)
    projection = _exact(
        raw_projection,
        {"schema", "activation_authority", "qualification_authority", "observed_at", "rows"},
        "qualification projection",
    )
    if projection["schema"] != PROJECTION_SCHEMA:
        raise QualificationError("qualification projection schema is unsupported")
    if projection["activation_authority"] != ACTIVATION_AUTHORITY:
        raise QualificationError("qualification projection contradicts route authority")
    if projection["qualification_authority"] != QUALIFICATION_AUTHORITY:
        raise QualificationError("qualification projection contradicts evidence authority")
    try:
        _utc_second(projection["observed_at"])
    except QualificationError as exc:
        raise QualificationError("qualification projection time is invalid") from exc
    rows = projection["rows"]
    if not isinstance(rows, list) or len(rows) != len(route_by_model):
        raise QualificationError("qualification projection must contain one row per tested model")

    indexed: dict[str, Mapping[str, Any]] = {}
    common_evidence: dict[str, str] | None = None
    for row in rows:
        item = _exact(
            row,
            {
                "model_id",
                "variant_id",
                "active_runtime",
                "runtime_origin",
                "states",
                "policy",
                "evidence",
            },
            "qualification row",
        )
        model_id = item["model_id"]
        if not isinstance(model_id, str) or model_id in indexed or model_id not in route_by_model:
            raise QualificationError("qualification row model identity is missing, duplicate, or extra")
        route = route_by_model[model_id]
        if item["variant_id"] != route["variant_id"]:
            raise QualificationError(f"{model_id}: qualification variant drift")
        runtime = _exact(
            item["active_runtime"],
            {"model_revision", "runtime_image_digest", "service"},
            f"{model_id} active runtime",
        )
        expected_runtime = {
            "model_revision": route["model_revision"],
            "runtime_image_digest": route["runtime_image_digest"],
            "service": route["service"],
        }
        if runtime != expected_runtime:
            raise QualificationError(f"{model_id}: qualification runtime or service drift")
        if item["runtime_origin"] != _runtime_origin(catalog, model_id, route["variant_id"]):
            raise QualificationError(f"{model_id}: qualification runtime origin drift")
        states = _exact(item["states"], _STATE_KEYS, f"{model_id} qualification states")
        if any(not isinstance(value, bool) for value in states.values()):
            raise QualificationError(f"{model_id}: qualification states must be independent booleans")
        if states["registered"] is not True or states["route_active"] is not True:
            raise QualificationError(f"{model_id}: projected active route must be registered and active")
        if item["policy"] != _policy(catalog, model_id):
            raise QualificationError(f"{model_id}: qualification policy drift")
        row_evidence = _evidence(item["evidence"])
        if common_evidence is None:
            common_evidence = row_evidence
        elif row_evidence != common_evidence:
            raise QualificationError("qualification rows disagree on retained evidence")
        indexed[model_id] = copy.deepcopy(item)
    if tuple(indexed) != tuple(sorted(route_by_model)):
        raise QualificationError("qualification rows must be canonically sorted and complete")
    return indexed
