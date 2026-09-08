"""Add exact native runtimes to the existing catalog, without granting routes.

The archival digest and tested-model cohort remain unchanged: their bindings
and historical receipts are validated before this additive projection. Each
native record, artifact, request contract and variant has its own content
identity. Deployment-selected runtimes and live publications remain the only
way to make these candidates usable through the gateway.
"""

from __future__ import annotations

import copy
import hashlib
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from types import MappingProxyType
from typing import Any

from fs2_serve_catalog.artifacts import ArtifactManifest, artifact_manifest_from_value
from fs2_serve_catalog.loader import (
    MODEL_ID,
    AcquisitionPlan,
    Catalog,
    CatalogError,
    FallbackCandidate,
    ModelRecord,
    ModelVariant,
    ScaleContract,
    SemanticRequestContract,
    _canonical_bytes,
    _exact,
    _load_json,
    _validate_semantic,
    execution_identity,
    resource_placement_identity,
    strong_sha256,
)

from .deployment_runtimes import _record, deployment_runtime_model_schema

NATIVE_SCHEMA = "fs2-serve.nebius.ai/native-catalog-model/v1"


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _identity(value: Any, label: str) -> str:
    if not isinstance(value, str) or MODEL_ID.fullmatch(value) is None:
        raise CatalogError(f"native {label} is not a canonical identity")
    return value


def _artifact(path: Path, catalog_dir: Path, reference: Any, record: Mapping[str, Any]) -> ArtifactManifest:
    reference = _exact(reference, {"path", "sha256"}, "native artifact reference")
    relative = reference["path"]
    if not isinstance(relative, str) or not relative or Path(relative).is_absolute():
        raise CatalogError("native artifact path must be relative to its declaration")
    manifest_path = path.parent / relative
    if not manifest_path.resolve().is_relative_to(catalog_dir.resolve()):
        raise CatalogError("native artifact path escapes the catalog")
    value = _load_json(manifest_path)
    expected = strong_sha256(reference["sha256"], "native artifact reference")
    if _digest(value) != expected:
        raise CatalogError("native artifact reference digest mismatch")
    # Formula source files use exactly the existing file-inventory envelope.
    # Reuse its checksum/path/license validation, then restore the truthful
    # kind and identity. No weights manifest is written, published or returned.
    if value.get("kind") == "formula":
        envelope = artifact_manifest_from_value({**value, "kind": "weights"})
        manifest = replace(envelope, kind="formula", digest=expected, _value=copy.deepcopy(value))
    else:
        manifest = artifact_manifest_from_value(value)
    source = record["model"]["source"]
    artifact = record["cache"]["artifact"]
    if (
        manifest.model_id != record["model"]["id"]
        or manifest.kind != artifact["kind"]
        or manifest.source_revision != source["revision"]
        or manifest.digest != artifact["manifest_digest"]
        or manifest.expanded_bytes != artifact["expanded_bytes"]
        or manifest.license_id != source["license"]["id"]
        or manifest.license_state != source["license"]["state"]
        or manifest.entitlement_state != source["entitlement"]["state"]
        or manifest.owner != record["cache"]["owner"]
        or manifest.retention != "retained-platform"
    ):
        raise CatalogError("native artifact differs from its exact model/source contract")
    return replace(manifest, path=manifest_path.resolve())


def _semantic(record: ModelRecord, value: Any) -> SemanticRequestContract:
    item = _exact(
        value,
        {"state", "blocker", "serialization", "invocation", "requests", "assets"},
        "native semantic request contract",
    )
    model = record.to_dict()
    interface = model["interface"]
    if (
        item["state"] != "qualified"
        or item["blocker"] is not None
        or item["serialization"]
        not in {
            "sha256-canonical-json-newline/v1",
            "sha256-canonical-json-no-newline/v1",
            "sha256-json-compact-no-newline/v1",
        }
        or len(interface["protocols"]) != 1
        or len(interface["policy"]["operations"]) != 1
    ):
        raise CatalogError("native runtime requires a qualified exact request contract")
    protocol = interface["protocols"][0]
    if item["invocation"] != {
        "operation": interface["policy"]["operations"][0],
        "protocol": protocol,
        "method": "POST",
        "endpoint": interface["endpoints"][protocol],
    }:
        raise CatalogError("native semantic invocation differs from its model interface")
    requests = item["requests"]
    if not isinstance(requests, list) or len(requests) != 2:
        raise CatalogError("native semantic contract requires two requests")
    ids, digests = set(), set()
    for raw in requests:
        request = _exact(raw, {"id", "payload_sha256"}, "native semantic request")
        ids.add(_identity(request["id"], "semantic request ID"))
        digests.add(strong_sha256(request["payload_sha256"], "native semantic payload"))
    if len(ids) != 2 or len(digests) != 2:
        raise CatalogError("native semantic requests must be distinct")
    if item["assets"] != []:
        raise CatalogError("native request contract currently requires packaged fixtures without external assets")
    semantic = model["semantic_validator"]
    validator = {
        key: semantic[key]
        for key in (
            "contract",
            "source_path",
            "source_sha256",
            "fixture_path",
            "fixture_sha256",
        )
    }
    subject = {"model_id": record.model_id, "model_digest": record.digest, "validator": validator, **item}
    return SemanticRequestContract(
        model_id=record.model_id,
        state="qualified",
        digest=_digest(subject),
        asset_set_digest=_digest(
            {
                "fixture": {"path": semantic["fixture_path"], "sha256": semantic["fixture_sha256"]},
                "assets": [],
            }
        ),
        _value=MappingProxyType(copy.deepcopy(item)),
    )


def _scale(catalog: Catalog, record: ModelRecord) -> ScaleContract:
    # Reuse the existing generic HTTP Deployment policy/boundary, not a model's
    # historical hardware evidence. Target and all identity hashes are new.
    template = next(
        (
            contract.to_dict()
            for contract in catalog.scale_contracts.values()
            if contract.to_dict().get("policy_profile") == "http-deployment-zero-to-one-v1"
        ),
        None,
    )
    if template is None:
        raise CatalogError("native catalog requires the shared HTTP Deployment scale policy")
    value = record.to_dict()
    target_subject = {
        "api_version": "apps/v1",
        "kind": "Deployment",
        "namespace": "fs2-models",
        "name": record.model_id,
        "selector": {"fs2-serve.nebius.ai/model-id": record.model_id},
        "model_digest": record.digest,
        "execution_identity_sha256": execution_identity(value),
        "resource_placement_identity_sha256": resource_placement_identity(value),
    }
    target = {key: target_subject[key] for key in ("api_version", "kind", "namespace", "name", "selector")}
    target.update(uid_source="signed-serving-binding", template_identity_sha256=_digest(target_subject))
    subject = {
        "schema": template["schema"],
        "model_id": record.model_id,
        "activation_mode": "replica-scale",
        "model_digest": record.digest,
        "execution_identity_sha256": execution_identity(value),
        "resource_placement_identity_sha256": resource_placement_identity(value),
        "policy_profile": "http-deployment-zero-to-one-v1",
        "target": target,
        "readiness": value["interface"]["readiness"],
        "warmup": value["interface"]["warmup"],
        "controller_boundary": template["controller_boundary"],
        "policy": template["policy"],
    }
    return ScaleContract(record.model_id, _digest(subject), "replica-scale", MappingProxyType(subject))


def augment_native_catalog(catalog: Catalog, catalog_dir: Path, *, repo_root: Path | None = None) -> Catalog:
    """Add immutable native declarations after validating all archived inputs.

    This preserves the archive's digest/qualification cohort. Native candidates
    are independently digest-bound and cannot replace any archived identity.
    """
    paths = sorted((catalog_dir / "native").glob("*.json"))
    if not paths:
        return catalog
    schema = deployment_runtime_model_schema(catalog_dir)
    records, variants, fallbacks = (
        dict(catalog.records),
        dict(catalog.model_variants),
        dict(catalog.fallback_candidates),
    )
    semantics, scales, plans = (
        dict(catalog.semantic_requests),
        dict(catalog.scale_contracts),
        dict(catalog.acquisition_plans),
    )
    repository = repo_root or catalog_dir / "packaged-repository"
    for path in paths:
        declaration = _exact(
            _load_json(path),
            {
                "schema",
                "record",
                "variant_id",
                "runtime_architecture",
                "semantic_requests",
                "artifact_manifest",
            },
            "native catalog declaration",
        )
        if declaration["schema"] != NATIVE_SCHEMA:
            raise CatalogError("unsupported native catalog schema")
        raw = declaration["record"]
        if not isinstance(raw, dict) or not isinstance(raw.get("model"), dict):
            raise CatalogError("native declaration lacks model identity")
        model_id = _identity(raw["model"].get("id"), "model ID")
        variant_id = _identity(declaration["variant_id"], "variant ID")
        architecture = _identity(declaration["runtime_architecture"], "runtime architecture")
        candidate_id = f"native-{variant_id}"
        if path.stem != model_id or model_id in records or variant_id in variants or candidate_id in fallbacks:
            raise CatalogError("native declaration cannot alias or replace an existing model/variant")
        if architecture not in {"cpu", "cuda"}:
            raise CatalogError("native runtime architecture must be cpu or cuda")
        # The exact-source variant exists only for graph validation here; the
        # shared selected-runtime validator checks the entire record next.
        variant_value = {
            "variant_id": variant_id,
            "base_model_id": model_id,
            "exposed_model_id": model_id,
            "variant_kind": "independent-runtime",
            "runtime_architecture": architecture,
            "source": copy.deepcopy(raw["model"].get("source")),
            "relationship": {
                "kind": "exact-model",
                "reference_model_id": model_id,
                "subject_model_id": model_id,
                "nim_artifact_parity": "not-applicable",
                "distinct_base_record_required": False,
            },
            "promotion": {"state": "candidate-unqualified", "route_exposed": False},
        }
        variant = ModelVariant(
            variant_id,
            model_id,
            model_id,
            "exact-model",
            architecture,
            _digest(variant_value),
            MappingProxyType(variant_value),
        )
        provisional = ModelRecord(model_id, path, _digest(raw), copy.deepcopy(raw))
        graph = replace(
            catalog, records={**records, model_id: provisional}, model_variants={**variants, variant_id: variant}
        )
        value = _record(raw, model_id, variant_id, graph, schema)
        if (architecture == "cpu") != (value["resources"]["gpu"]["class"] == "CPU"):
            raise CatalogError("native architecture differs from its declared compute resources")
        _validate_semantic(value["semantic_validator"], repository, catalog_dir)
        artifact = _artifact(path, catalog_dir, declaration["artifact_manifest"], value)
        record = ModelRecord(model_id, path, _digest(value), value)
        fallback_value = {
            "candidate_id": candidate_id,
            "lane_id": model_id,
            "state": "mapped-source-only",
            "relationship": "exact-model",
            "profile_variants": {architecture: variant_id},
            "secondary_non_alias_alternative": None,
        }
        acquisition = {
            "schema": "fs2-serve.nebius.ai/native-runtime-acquisition/v1",
            "model_id": model_id,
            "method": "runtime-image",
            "source": copy.deepcopy(value["model"]["source"]),
            "runtime_image": copy.deepcopy(value["runtime"]["image"]),
            "artifact_manifest_sha256": artifact.digest,
            "artifact_kind": artifact.kind,
            "expanded_bytes": artifact.expanded_bytes,
            "required_prerequisite_ids": [],
        }
        records[model_id], variants[variant_id] = record, variant
        fallbacks[candidate_id] = FallbackCandidate(
            candidate_id,
            model_id,
            "mapped-source-only",
            "exact-model",
            MappingProxyType({architecture: variant_id}),
            _digest(fallback_value),
            MappingProxyType(fallback_value),
        )
        plans[model_id] = AcquisitionPlan(model_id, "runtime-image", (), MappingProxyType(acquisition))
        semantics[model_id] = _semantic(record, declaration["semantic_requests"])
        scales[model_id] = _scale(catalog, record)
    return replace(
        catalog,
        records=MappingProxyType(records),
        model_variants=MappingProxyType(variants),
        fallback_candidates=MappingProxyType(fallbacks),
        acquisition_plans=MappingProxyType(plans),
        semantic_requests=MappingProxyType(semantics),
        scale_contracts=MappingProxyType(scales),
    )
