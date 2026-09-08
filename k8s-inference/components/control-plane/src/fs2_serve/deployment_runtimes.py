"""Explicit deployment-selected runtimes, without rewriting archival catalog facts.

The mounted set is selection authority for a runtime candidate, not route
authority. Canonical variants establish exact-model/source identity; dynamic
publications still own serving and the normal route guards remain in force.
"""

from __future__ import annotations

import copy
import hashlib
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from types import MappingProxyType
from typing import Any

from fs2_serve_catalog.consumer import GatewayCatalog, ServingBindings, bind_gateway_catalog
from fs2_serve_catalog.loader import (
    Catalog,
    CatalogError,
    ModelRecord,
    _canonical_bytes,
    _exact,
    _load_json,
    _validate_artifact,
    _validate_entitlement,
    _validate_image,
    _validate_status_binding,
    canonical_http_path,
    strong_sha256,
)
from jsonschema import Draft202012Validator, ValidationError

from .qualification import _EVIDENCE_KEYS, _STATE_KEYS, QualificationError, _policy, _runtime_origin

ENTRY_SCHEMA = "fs2-serve.nebius.ai/deployment-runtime/v1"
SET_SCHEMA = "fs2-serve.nebius.ai/deployment-runtime-set/v1"


class DeploymentRuntimeError(ValueError):
    """Selected deployment runtime contradicts its immutable subject."""


def load_deployment_runtime_entries(path: Path | None) -> dict[str, Any]:
    """Read explicit selections; Registry validates their complete subject graph."""
    if path is None:
        return {}
    # Kubernetes projects ConfigMap keys through a ..data symlink. Resolve the
    # mounted selection before the catalog loader's regular-file check.
    document = _exact(_load_json(path.resolve(strict=True)), {"schema", "models"}, "deployment runtime set")
    if document["schema"] != SET_SCHEMA or not isinstance(document["models"], dict):
        raise DeploymentRuntimeError("deployment runtime set schema is invalid")
    return copy.deepcopy(document["models"])


def deployment_runtime_configuration_identity(entry: Mapping[str, Any]) -> dict[str, Any]:
    """Pure shared identity projection for a validated selected deployment entry.

    Admin bootstrap and the Terraform wrapper use this exact descriptor, not
    the archival NIM acquisition plan. It grants no route or qualification.
    """
    value = entry["record"]
    model_id = entry["model_id"]
    if entry["schema"] != ENTRY_SCHEMA or value["model"]["id"] != model_id:
        raise DeploymentRuntimeError("deployment configuration model identity mismatch")
    source = value["model"]["source"]
    artifact = value["cache"]["artifact"]
    image_digest = value["runtime"]["image"]["digest"]
    strong_sha256(image_digest, "deployment runtime image", image=True)
    strong_sha256(artifact["manifest_digest"], "deployment artifact manifest")
    acquisition = {
        "schema": "fs2-serve.nebius.ai/deployment-runtime-acquisition/v1",
        "model_id": model_id,
        "source": {key: source[key] for key in ("kind", "repository", "revision")},
        "runtime_image_digest": image_digest,
        "artifact": {key: artifact[key] for key in ("kind", "manifest_digest", "expanded_bytes")},
        "cache_owner": value["cache"]["owner"],
    }

    def digest(subject: Any) -> str:
        return hashlib.sha256(_canonical_bytes(subject)).hexdigest()

    gpu_class = value["resources"]["gpu"]["class"]
    return {
        "model_id": model_id,
        "artifact_manifest_sha256": artifact["manifest_digest"],
        "acquisition_contract_sha256": digest(acquisition),
        "provenance_sha256": digest(value),
        "semantic_health_contract_sha256": digest(value["semantic_validator"]),
        "runtime_image_digest": image_digest,
        "model_revision": source["revision"],
        "supported_accelerator_classes": sorted({gpu_class, gpu_class.lower()}),
    }


def _record(value: Any, model_id: str, variant_id: str, catalog: Catalog, schema: dict[str, Any]) -> dict[str, Any]:
    try:
        Draft202012Validator(schema).validate(value)
    except ValidationError as exc:
        raise DeploymentRuntimeError("deployment runtime record violates model/v1 structure") from exc
    record: dict[str, Any] = copy.deepcopy(value)
    variant = catalog.model_variant(variant_id)
    subject = variant.to_dict()
    if (
        model_id not in catalog.records
        or variant.base_model_id != model_id
        or variant.exposed_model_id != model_id
        or variant.relationship != "exact-model"
        or subject["relationship"]["reference_model_id"] != model_id
        or subject["relationship"]["subject_model_id"] != model_id
        or subject["relationship"]["distinct_base_record_required"]
        or record["model"]["id"] != model_id
    ):
        raise DeploymentRuntimeError(
            "deployment runtime must identify one canonical exact-model variant without aliasing"
        )
    source = record["model"]["source"]
    if any(source[key] != subject["source"][key] for key in ("kind", "repository", "revision")):
        raise DeploymentRuntimeError("deployment runtime source differs from exact canonical variant")
    if record["runtime"]["kind"] in {"nim", "unresolved"} or source["kind"] == "ngc-nim":
        raise DeploymentRuntimeError("independent deployment runtime cannot claim NVIDIA NIM origin")
    image_state, _, image_digest = _validate_image(record["runtime"]["image"])
    license_state = _validate_status_binding(source["license"], "deployment runtime license")
    entitlement_state = _validate_entitlement(source["entitlement"])
    artifact_state, _, artifact_kind = _validate_artifact(
        record["cache"]["artifact"], additional_kinds=frozenset({"reference-database", "formula"})
    )
    gpu = record["resources"]["gpu"]
    cpu_runtime = gpu["class"] == "CPU"
    resource_artifact_valid = (
        artifact_kind in {"reference-database", "weights", "formula"}
        and record["cache"]["owner"] == "runtime-image"
        and gpu
        == {
            "class": "CPU",
            "count": 0,
            "topology": "cpu-only",
            "placement": None,
            "b300_state": "not-applicable",
            "alternatives": [],
        }
        if cpu_runtime
        else artifact_kind == "weights"
        and record["cache"]["owner"] in {"fs2-serve-localizer", "runtime-image"}
        and gpu["count"] >= 1
        and gpu["topology"] in {"single-gpu", "single-node-multi-gpu"}
        and gpu["b300_state"] != "not-applicable"
    )
    if (
        image_state != "resolved"
        or image_digest is None
        or license_state != "verified"
        or entitlement_state not in {"verified", "not-required"}
        or artifact_state != "platform-verified"
        or not resource_artifact_valid
    ):
        raise DeploymentRuntimeError(
            "deployment runtime requires exact image, verified policy and a resource-matched artifact"
        )
    strong_sha256(record["cache"]["artifact"]["manifest_digest"], "deployment artifact manifest")
    for field, prefix in (
        ("shared_path", "/mnt/fs2-serve-cache/models/"),
        ("local_path", "/var/lib/fs2-serve/cache/models/"),
    ):
        if record["cache"][field] != prefix + model_id:
            raise DeploymentRuntimeError("deployment runtime cache path aliases another model")
    interface = record["interface"]
    if (
        interface["execution_mode"] != "http"
        or not interface["protocols"]
        or set(interface["endpoints"]) != set(interface["protocols"])
        or not interface["policy"]["operations"]
        or not record["runtime"]["command"]
        or not record["semantic_validator"]["contract"]
        or record["support"]["state"] != "qualified"
        or record["support"]["route_exposed"]
        or interface["mcp"]["invocable"]
        or record["support"]["non_clinical"] != interface["policy"]["non_clinical"]
        or (not cpu_runtime and (gpu["count"] == 1) != (gpu["topology"] == "single-gpu"))
    ):
        raise DeploymentRuntimeError("deployment candidate has an incomplete contract or claims static routing")
    for endpoint in interface["endpoints"].values():
        canonical_http_path(endpoint, "deployment runtime endpoint")
    if interface["readiness"] is None:
        raise DeploymentRuntimeError("deployment runtime requires readiness")
    return record


def deployment_runtime_model_schema(catalog_dir: Path) -> dict[str, Any]:
    """Adapt the archived shape for exact native runtime capabilities only."""
    schema: dict[str, Any] = _load_json(catalog_dir / "schema/model.schema.json")
    # Preserve the archived schema and its qualification receipts. Native
    # records use the same structure, without its historical B300-only lane.
    gpu_schema = schema["properties"]["resources"]["properties"]["gpu"]["properties"]
    gpu_schema["class"] = {"type": "string", "minLength": 1}
    gpu_schema["count"]["minimum"] = 0
    gpu_schema["topology"]["enum"].append("cpu-only")
    gpu_schema["b300_state"]["enum"].append("not-applicable")
    schema["properties"]["cache"]["properties"]["owner"]["enum"].append("runtime-image")
    schema["$defs"]["artifact"]["properties"]["kind"]["enum"].extend(["reference-database", "formula"])
    schema["properties"]["model"]["properties"]["family"]["enum"].append("biological-age")
    return schema


def bind_deployment_runtimes(
    gateway: GatewayCatalog,
    catalog: Catalog,
    bindings: ServingBindings,
    path: Path | None,
    *,
    catalog_dir: Path,
) -> GatewayCatalog:
    """Project only a mounted, explicitly selected set; absent means no change."""
    if path is None:
        return gateway
    try:
        entries = load_deployment_runtime_entries(path)
        schema = deployment_runtime_model_schema(catalog_dir)
        records = dict(catalog.records)
        selected: dict[str, dict[str, Any]] = {}
        for model_id, raw in entries.items():
            item = _exact(
                raw, {"schema", "model_id", "variant_id", "record", "qualification"}, "deployment runtime entry"
            )
            if item["schema"] != ENTRY_SCHEMA or item["model_id"] != model_id:
                raise DeploymentRuntimeError("deployment runtime entry key/schema mismatch")
            base = gateway.model(model_id)
            if base.routable or (base.binding is not None and base.binding.enabled):
                raise DeploymentRuntimeError("deployment runtime cannot replace an already-routable static binding")
            if not isinstance(item["variant_id"], str):
                raise DeploymentRuntimeError("deployment runtime variant ID must be explicit")
            value = _record(item["record"], model_id, item["variant_id"], catalog, schema)
            digest = hashlib.sha256(_canonical_bytes(value)).hexdigest()
            records[model_id] = ModelRecord(model_id, path, digest, value)
            selected[model_id] = item
        projected_catalog = replace(catalog, records=MappingProxyType(records))
        projected = bind_gateway_catalog(projected_catalog, bindings)
        models = dict(gateway.models)
        for model_id, item in selected.items():
            row = _exact(
                item["qualification"],
                {"model_id", "variant_id", "active_runtime", "runtime_origin", "states", "policy", "evidence"},
                "deployment qualification row",
            )
            value = records[model_id].to_dict()
            service = row["active_runtime"].get("service") if isinstance(row["active_runtime"], dict) else None
            if service not in (
                {"namespace": "fs2-models", "name": model_id, "port": 8000},
                {"namespace": "fs2-models", "name": model_id + "-b300", "port": 8000},
            ):
                raise DeploymentRuntimeError("deployment qualification service aliases another model")
            expected_runtime = {
                "model_revision": value["model"]["source"]["revision"],
                "runtime_image_digest": value["runtime"]["image"]["digest"],
                # The retained -b300 suffix is a Service identity, not a GPU
                # declaration. Actual allocation remains in the record.
                "service": service,
            }
            if (
                row["model_id"] != model_id
                or row["variant_id"] != item["variant_id"]
                or row["active_runtime"] != expected_runtime
                or row["runtime_origin"] != _runtime_origin(catalog, model_id, item["variant_id"])
                or row["policy"] != _policy(projected_catalog, model_id)
            ):
                raise DeploymentRuntimeError("deployment qualification runtime/source/policy identity mismatch")
            states = _exact(row["states"], _STATE_KEYS, "deployment qualification states")
            if any(not isinstance(flag, bool) for flag in states.values()) or not all(
                states[key] for key in ("registered", "runtime_ready", "semantic_qualified")
            ):
                raise DeploymentRuntimeError("deployment runtime lacks direct runtime and semantic qualification")
            evidence = _exact(row["evidence"], _EVIDENCE_KEYS, "deployment qualification evidence")
            required_evidence = {"audited_catalog_sha256", "retained_deployments_sha256"}
            for state, fields in {
                "route_active": ("audited_live_routes_sha256", "model_discovery_sha256"),
                "http_mcp_qualified": ("mcp_discovery_sha256", "http_mcp_acceptance_sha256"),
                "cold_start_qualified": ("cold_start_acceptance_sha256",),
                "elasticity_qualified": ("elasticity_acceptance_sha256",),
            }.items():
                if states[state]:
                    required_evidence.update(fields)
            for key, digest in evidence.items():
                if digest is not None or key in required_evidence:
                    strong_sha256(digest, "deployment qualification evidence " + key)
            base = gateway.model(model_id)
            # Keep the canonical binding untouched. This disabled projection
            # pins the guards consulted by future dynamic publications; it is
            # never an enabled historical serving receipt for the new image.
            binding = (
                None
                if base.binding is None
                else replace(
                    base.binding,
                    enabled=False,
                    ready=False,
                    valid_until=None,
                    model_digest=records[model_id].digest,
                    backend_runtime_image_digest=value["runtime"]["image"]["digest"],
                    backend_gpu_class=value["resources"]["gpu"]["class"],
                    artifact_manifest_digest=value["cache"]["artifact"]["manifest_digest"],
                )
            )
            models[model_id] = replace(
                projected.model(model_id),
                binding=binding,
                routable=False,
                mcp_invocable=False,
                qualification=MappingProxyType(
                    {
                        **copy.deepcopy(row),
                        "deployment_runtime": {
                            "record_digest": records[model_id].digest,
                            "artifact_manifest_digest": value["cache"]["artifact"]["manifest_digest"],
                            "runtime_image_digest": value["runtime"]["image"]["digest"],
                            "model_revision": value["model"]["source"]["revision"],
                        },
                    }
                ),
            )
        return replace(gateway, models=MappingProxyType(dict(sorted(models.items()))))
    except (CatalogError, QualificationError, KeyError, TypeError) as exc:
        raise DeploymentRuntimeError("deployment runtime set validation failed") from exc
